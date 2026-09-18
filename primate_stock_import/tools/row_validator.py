# -*- coding: utf-8 -*-
"""Validación de las filas del Excel y resolución a registros de Odoo.

El resultado es un ``dict`` serializable a JSON que se guarda en la sesión de
importación y que el ``PickingBuilder`` consume tal cual, sin volver a leer el
archivo ni repetir búsquedas.
"""
import datetime
import logging

import pytz

from odoo import _
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT, float_compare, float_is_zero

from .excel_parser import HEADER_COLUMNS

_logger = logging.getLogger(__name__)

DATE_FORMATS = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y %H:%M',
    '%d/%m/%Y',
    '%d-%m-%Y',
)


class RowValidator:
    """Valida filas parseadas y las agrupa en operaciones listas para crear.

    Estructura del resultado de ``validate``::

        {
            'total_rows': int,
            'picking_count': int,
            'move_count': int,
            'has_blocking_errors': bool,
            'errors': {row(str): [mensaje, ...]},
            'warnings': {row(str): [mensaje, ...]},
            'groups': [
                {
                    'operation_ref': str, 'row': int,
                    'picking_type_id': int, 'company_id': int,
                    'partner_id': int | False,
                    'location_id': int, 'location_dest_id': int,
                    'scheduled_date': 'YYYY-MM-DD HH:MM:SS' | False,
                    'origin': str | False,
                    'lines': [
                        {'row': int, 'product_id': int, 'product_uom_id': int,
                         'quantity': float, 'lot_name': str | False},
                    ],
                },
            ],
        }

    Las claves de ``errors``/``warnings`` son texto porque el resultado se
    persiste en un campo Json.
    """

    def __init__(self, env):
        self.env = env
        self.errors = {}
        self.warnings = {}
        self._cache = {}

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def validate(self, rows):
        """Valida todas las filas y arma los grupos por ``operation_ref``.

        Args:
            rows (list[dict]): salida de ``ExcelParser.parse``.

        Returns:
            dict: ver docstring de la clase.
        """
        self.errors = {}
        self.warnings = {}
        groups = self._group_rows(rows)
        validated_groups = []
        for operation_ref, group_rows in groups.items():
            header = self._validate_header(operation_ref, group_rows)
            lines = [self._validate_line(row) for row in group_rows]
            lines = [line for line in lines if line]
            if header and lines:
                header['lines'] = lines
                validated_groups.append(header)
        return {
            'total_rows': len(rows),
            'picking_count': len(groups),
            'move_count': sum(len(group['lines']) for group in validated_groups),
            'has_blocking_errors': bool(self.errors),
            'errors': self.errors,
            'warnings': self.warnings,
            'groups': validated_groups,
        }

    # ------------------------------------------------------------------
    # Agrupación y cabezal
    # ------------------------------------------------------------------
    def _group_rows(self, rows):
        """Agrupa filas por ``operation_ref`` conservando el orden del archivo."""
        groups = {}
        for row in rows:
            operation_ref = row.get('operation_ref')
            if not operation_ref:
                self._add_error(row, _("'operation_ref' es obligatorio."))
                continue
            groups.setdefault(operation_ref, []).append(row)
        return groups

    def _validate_header(self, operation_ref, group_rows):
        """Resuelve el cabezal de la operación a partir de la primera fila.

        Verifica además que las columnas de cabezal sean idénticas en todas
        las filas del grupo; la primera fila manda.

        Returns:
            dict | None: cabezal resuelto, o None si hubo errores bloqueantes.
        """
        first = group_rows[0]
        for row in group_rows[1:]:
            for column in HEADER_COLUMNS:
                if (row.get(column) or '') != (first.get(column) or ''):
                    self._add_error(row, _(
                        "'%(column)s' inconsistente dentro de la operación %(ref)s: "
                        "se esperaba '%(expected)s' (fila %(first_row)s).",
                        column=column, ref=operation_ref,
                        expected=first.get(column) or '', first_row=first['row'],
                    ))

        picking_type = self._resolve_picking_type(first)
        if not picking_type:
            return None
        company = picking_type.company_id
        partner = self._resolve_partner(first)
        default_src, default_dest = self._default_locations(picking_type, partner)
        location = self._resolve_location(first, 'location_origin', default_src, company)
        location_dest = self._resolve_location(first, 'location_dest', default_dest, company)
        scheduled_date = self._parse_date(first)
        if not location or not location_dest:
            return None
        if location == location_dest:
            self._add_error(first, _("La ubicación de origen y de destino son la misma."))
            return None
        return {
            'operation_ref': operation_ref,
            'row': first['row'],
            'picking_type_id': picking_type.id,
            'company_id': company.id,
            'partner_id': partner.id if partner else False,
            'location_id': location.id,
            'location_dest_id': location_dest.id,
            'scheduled_date': scheduled_date or False,
            'origin': first.get('origin') or False,
        }

    def _default_locations(self, picking_type, partner):
        """Ubicaciones por defecto con la misma cascada que ``stock.picking``.

        Default del tipo de operación → ubicación de proveedor/cliente del
        contacto → ubicaciones genéricas de proveedores/clientes.

        Returns:
            tuple: (ubicación origen, ubicación destino), cada una un recordset
                de ``stock.location`` (puede estar vacío).
        """
        picking_type = picking_type.with_company(picking_type.company_id)
        customer_location, supplier_location = self.env['stock.warehouse'].with_company(
            picking_type.company_id
        )._get_partner_locations()
        default_src = picking_type.default_location_src_id
        if not default_src:
            default_src = partner.property_stock_supplier if partner else supplier_location
        default_dest = picking_type.default_location_dest_id
        if not default_dest:
            default_dest = partner.property_stock_customer if partner else customer_location
        return default_src, default_dest

    def _resolve_picking_type(self, row):
        """Busca el tipo de operación por 'Almacén: Nombre', nombre o código.

        Acepta el ``display_name`` de Odoo ("WH: Recepciones"), el nombre solo
        (desambiguado con la columna ``warehouse`` si hay varios almacenes) o
        el ``sequence_code`` (p. ej. "IN").
        """
        value = row.get('picking_type')
        if not value:
            self._add_error(row, _("'picking_type' es obligatorio."))
            return None
        warehouse_name = row.get('warehouse') or ''
        if ':' in value and not warehouse_name:
            warehouse_name, value = (part.strip() for part in value.split(':', 1))
        key = ('picking_type', warehouse_name.lower(), value.lower())
        if key in self._cache:
            picking_type = self._cache[key]
        else:
            picking_type = self._search_picking_type(value, warehouse_name)
            self._cache[key] = picking_type
        if picking_type is None:
            self._add_error(row, _(
                "Tipo de operación '%s' no encontrado. Usá el nombre exacto, el formato "
                "'Almacén: Nombre' o el código de secuencia (IN, OUT, INT).", value
            ))
        elif len(picking_type) > 1:
            self._add_error(row, _(
                "Tipo de operación '%(value)s' ambiguo (%(names)s). Indicá el almacén en la "
                "columna 'warehouse' o usá el formato 'Almacén: Nombre'.",
                value=value, names=", ".join(picking_type.mapped('display_name')),
            ))
            return None
        return picking_type or None

    def _search_picking_type(self, value, warehouse_name):
        picking_type_model = self.env['stock.picking.type']
        domain = [('company_id', 'in', self.env.companies.ids), ('active', '=', True)]
        if warehouse_name:
            domain.append(('warehouse_id.name', '=ilike', warehouse_name))
        picking_type = self._search_translated(picking_type_model, 'name', value, domain)
        if not picking_type:
            picking_type = picking_type_model.search(domain + [('sequence_code', '=ilike', value)])
        return picking_type or None

    def _search_translated(self, model, field_name, value, domain, limit=None):
        """Busca por un campo traducible probando todos los idiomas activos.

        Odoo solo compara contra el idioma del usuario; quien arma el Excel
        puede escribir "Unidades" mientras el usuario que importa está en
        inglés ("Units"). Se prueba primero el idioma del usuario y después el
        resto, devolviendo el primer idioma con coincidencias.
        """
        user_lang = self.env.user.lang or 'en_US'
        langs = [user_lang] + [
            code for code, _name in self.env['res.lang'].get_installed() if code != user_lang
        ]
        for lang in langs:
            records = model.with_context(lang=lang).search(
                domain + [(field_name, '=ilike', value)], limit=limit
            )
            if records:
                return records.with_context(lang=user_lang)
        return model.browse()

    def _resolve_partner(self, row):
        """Busca el contacto por RUT/NIF exacto o por nombre exacto (opcional)."""
        value = row.get('partner')
        if not value:
            return None
        key = ('partner', value.lower())
        if key not in self._cache:
            partner_model = self.env['res.partner']
            partner = partner_model.search([('vat', '=ilike', value)], limit=2)
            if not partner:
                partner = partner_model.search([('name', '=ilike', value)], limit=2)
            self._cache[key] = partner
        partner = self._cache[key]
        if not partner:
            self._add_error(row, _(
                "Contacto '%s' no encontrado (se busca por RUT o nombre exacto).", value
            ))
            return None
        if len(partner) > 1:
            self._add_error(row, _("Contacto '%s' ambiguo: hay más de uno con ese nombre.", value))
            return None
        return partner

    def _resolve_location(self, row, column, default_location, company):
        """Busca la ubicación por nombre completo, nombre corto o código de barras.

        Si la columna viene vacía se usa la ubicación por defecto del tipo de
        operación; si tampoco hay, es error.
        """
        value = row.get(column)
        if not value:
            if default_location:
                return default_location
            self._add_error(row, _(
                "'%s' es obligatorio porque el tipo de operación no tiene ubicación por defecto.",
                column,
            ))
            return None
        key = ('location', company.id, value.lower())
        if key not in self._cache:
            location_model = self.env['stock.location']
            domain = [('company_id', 'in', [company.id, False]), ('active', '=', True)]
            location = location_model.search(domain + [('complete_name', '=ilike', value)], limit=2)
            if not location:
                location = location_model.search(domain + [('name', '=ilike', value)], limit=2)
            if not location:
                location = location_model.search(domain + [('barcode', '=ilike', value)], limit=2)
            self._cache[key] = location
        location = self._cache[key]
        if not location:
            self._add_error(row, _("Ubicación '%s' no encontrada.", value))
            return None
        if len(location) > 1:
            self._add_error(row, _(
                "Ubicación '%(value)s' ambigua (%(names)s). Usá el nombre completo, "
                "por ejemplo 'WH/Stock/Estantería 1'.",
                value=value, names=", ".join(location.mapped('complete_name')),
            ))
            return None
        return location

    def _parse_date(self, row):
        """Convierte ``scheduled_date`` a datetime UTC en formato de servidor.

        La fecha del archivo se interpreta en la zona horaria del usuario, para
        que "2026-09-15" quede como el 15 a las 00:00 hora local y no el 14.

        Returns:
            str | None: fecha en ``DEFAULT_SERVER_DATETIME_FORMAT`` o None si
                la columna está vacía.
        """
        value = row.get('scheduled_date')
        if not value:
            return None
        parsed = None
        for date_format in DATE_FORMATS:
            try:
                parsed = datetime.datetime.strptime(value, date_format)
                break
            except ValueError:
                continue
        if parsed is None:
            self._add_error(row, _(
                "'scheduled_date' inválida: '%s'. Formatos aceptados: AAAA-MM-DD, "
                "AAAA-MM-DD HH:MM, DD/MM/AAAA.", value
            ))
            return None
        user_tz = pytz.timezone(self.env.user.tz or 'UTC')
        utc_date = user_tz.localize(parsed).astimezone(pytz.utc).replace(tzinfo=None)
        return utc_date.strftime(DEFAULT_SERVER_DATETIME_FORMAT)

    # ------------------------------------------------------------------
    # Líneas
    # ------------------------------------------------------------------
    def _validate_line(self, row):
        """Resuelve producto, cantidad, UdM y lote de una fila.

        Returns:
            dict | None: línea resuelta, o None si hubo errores bloqueantes.
        """
        product = self._resolve_product(row)
        quantity = self._parse_quantity(row)
        if not product or quantity is None:
            return None
        uom = self._resolve_uom(row, product)
        if not uom:
            return None
        lot_name = self._validate_lot(row, product, quantity)
        return {
            'row': row['row'],
            'product_id': product.id,
            'product_uom_id': uom.id,
            'quantity': quantity,
            'lot_name': lot_name,
        }

    def _resolve_product(self, row):
        """Busca la variante por referencia interna exacta o código de barras."""
        value = row.get('product')
        if not value:
            self._add_error(row, _("'product' es obligatorio."))
            return None
        key = ('product', value.lower())
        if key not in self._cache:
            product_model = self.env['product.product']
            product = product_model.search([('default_code', '=ilike', value)], limit=2)
            if not product:
                product = product_model.search([('barcode', '=', value)], limit=2)
            self._cache[key] = product
        product = self._cache[key]
        if not product:
            self._add_error(row, _(
                "Producto '%s' no encontrado o archivado (se busca por referencia interna "
                "o código de barras).", value
            ))
            return None
        if len(product) > 1:
            self._add_error(row, _(
                "Producto '%s' ambiguo: más de una variante con ese código.", value
            ))
            return None
        if product.detailed_type not in ('product', 'consu'):
            self._add_error(row, _(
                "Producto '%s' es un servicio: no genera movimientos de stock.", value
            ))
            return None
        return product

    def _parse_quantity(self, row):
        """Convierte ``quantity`` a float positivo; acepta coma o punto decimal."""
        value = (row.get('quantity') or '').replace(',', '.')
        if not value:
            self._add_error(row, _("'quantity' es obligatorio."))
            return None
        try:
            quantity = float(value)
        except ValueError:
            self._add_error(row, _("'quantity' debe ser numérico: '%s'.", row.get('quantity')))
            return None
        if float_compare(quantity, 0.0, precision_digits=6) <= 0:
            self._add_error(row, _("'quantity' debe ser mayor que cero."))
            return None
        return quantity

    def _resolve_uom(self, row, product):
        """Devuelve la UdM de la fila (misma categoría que el producto) o la del producto."""
        value = row.get('uom')
        if not value:
            return product.uom_id
        key = ('uom', value.lower())
        if key not in self._cache:
            self._cache[key] = self._search_translated(
                self.env['uom.uom'], 'name', value, [], limit=2
            )
        uom = self._cache[key]
        if not uom:
            self._add_error(row, _("Unidad de medida '%s' no encontrada.", value))
            return None
        if len(uom) > 1:
            self._add_error(row, _("Unidad de medida '%s' ambigua.", value))
            return None
        if uom.category_id != product.uom_id.category_id:
            self._add_error(row, _(
                "La UdM '%(uom)s' no es de la misma categoría que la del producto "
                "('%(product_uom)s').", uom=uom.name, product_uom=product.uom_id.name,
            ))
            return None
        return uom

    def _validate_lot(self, row, product, quantity):
        """Controla la coherencia del lote con el seguimiento del producto.

        En esta versión el lote no se asigna al crear la operación (la reserva
        por lote se hace al comprobar disponibilidad o a mano); solo se valida y
        se conserva el nombre en el resultado para trazabilidad.
        """
        lot_name = row.get('lot') or False
        if product.tracking == 'none':
            if lot_name:
                self._add_warning(row, _(
                    "El producto no tiene seguimiento por lote/serie: se ignora el lote '%s'.",
                    lot_name,
                ))
            return False
        if not lot_name:
            self._add_warning(row, _(
                "El producto requiere lote/serie y no se indicó: deberá asignarse "
                "manualmente antes de validar la operación."
            ))
            return False
        if product.tracking == 'serial' and not float_is_zero(quantity - 1.0, precision_digits=6):
            self._add_error(row, _(
                "El producto tiene seguimiento por número de serie: la cantidad debe ser 1 "
                "por fila."
            ))
        lot = self.env['stock.lot'].search([
            ('name', '=', lot_name), ('product_id', '=', product.id),
        ], limit=1)
        if not lot:
            self._add_warning(row, _(
                "El lote '%s' no existe para este producto; se deberá crear al asignarlo.",
                lot_name,
            ))
        return lot_name

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def _add_error(self, row, message):
        self.errors.setdefault(str(row['row']), []).append(message)

    def _add_warning(self, row, message):
        self.warnings.setdefault(str(row['row']), []).append(message)

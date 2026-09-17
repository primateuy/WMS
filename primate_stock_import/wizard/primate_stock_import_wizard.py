# -*- coding: utf-8 -*-
import base64
import io
import logging

import xlsxwriter

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..tools.excel_parser import COLUMNS, REQUIRED_COLUMNS, ExcelParser
from ..tools.picking_builder import PickingBuilder
from ..tools.row_validator import RowValidator

_logger = logging.getLogger(__name__)

# Texto de ayuda de cada columna para la hoja "Instrucciones" de la plantilla.
COLUMN_HELP = {
    'operation_ref': (
        "Obligatorio. Identificador libre de la operación. Todas las filas con la misma "
        "referencia forman una sola operación (un picking con varias líneas)."
    ),
    'picking_type': (
        "Obligatorio. Tipo de operación: nombre exacto ('Recepciones'), formato "
        "'Almacén: Nombre' ('WH: Recepciones') o código de secuencia (IN, OUT, INT)."
    ),
    'warehouse': (
        "Opcional. Nombre del almacén, para desambiguar cuando el mismo tipo de operación "
        "existe en varios almacenes."
    ),
    'partner': "Opcional. Contacto (proveedor/cliente) por RUT o nombre exacto.",
    'location_origin': (
        "Opcional. Ubicación de origen por nombre completo ('WH/Stock'), nombre corto o "
        "código de barras. Si se omite se usa la del tipo de operación."
    ),
    'location_dest': "Opcional. Ubicación de destino. Igual criterio que location_origin.",
    'scheduled_date': (
        "Opcional. Fecha programada: AAAA-MM-DD, AAAA-MM-DD HH:MM o DD/MM/AAAA. "
        "Se interpreta en la zona horaria del usuario."
    ),
    'origin': "Opcional. Documento origen (OC, pedido, etc.). Si se omite se usa operation_ref.",
    'product': "Obligatorio. Referencia interna o código de barras de la variante.",
    'lot': (
        "Opcional. Nombre del lote/serie. En esta versión solo se valida y se anota en la "
        "descripción del movimiento; la asignación se hace al reservar."
    ),
    'quantity': "Obligatorio. Cantidad mayor que cero. Acepta coma o punto decimal.",
    'uom': (
        "Opcional. Unidad de medida (misma categoría que la del producto). "
        "Si se omite se usa la del producto."
    ),
}
EXAMPLE_ROWS = (
    ('IMP-001', 'Recepciones', 'WH', 'Proveedor SA', '', '', '2026-09-20', 'OC-123',
     'PROD-001', '', '10', ''),
    ('IMP-001', 'Recepciones', 'WH', 'Proveedor SA', '', '', '2026-09-20', 'OC-123',
     'PROD-002', 'L-2026-01', '5,5', ''),
    ('IMP-002', 'Transferencias internas', 'WH', '', 'WH/Stock', 'WH/Stock/Tienda', '', '',
     'PROD-001', '', '3', 'Unidades'),
)


class PrimateStockImportWizard(models.TransientModel):
    """Asistente en tres pasos: cargar archivo, validar, importar."""

    _name = 'primate_stock_import.wizard'
    _description = 'Asistente de importación de operaciones de inventario'

    state = fields.Selection(
        [
            ('upload', "Cargar archivo"),
            ('validated', "Validado"),
            ('done', "Importado"),
        ],
        string="Paso",
        default='upload',
        readonly=True,
    )
    file = fields.Binary(string="Archivo Excel")
    file_name = fields.Char(string="Nombre del archivo")
    session_id = fields.Many2one(
        'primate_stock_import.session',
        string="Sesión de importación",
        readonly=True,
    )
    has_errors = fields.Boolean(related='session_id.has_errors')
    error_count = fields.Integer(related='session_id.error_count')
    warning_count = fields.Integer(related='session_id.warning_count')
    picking_count = fields.Integer(related='session_id.picking_count')
    log_html = fields.Html(related='session_id.log_html')
    template_file = fields.Binary(string="Plantilla", readonly=True)
    template_file_name = fields.Char(default='plantilla_importacion_operaciones.xlsx')

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def action_validate(self):
        """Lee y valida el archivo sin crear operaciones.

        Crea (o reutiliza) la sesión de importación con el archivo original,
        el resultado de la validación y el log por fila.

        Returns:
            dict: action que reabre el asistente en el paso 'validated'.

        Raises:
            UserError: si no se cargó archivo o el archivo no se puede leer.
        """
        self.ensure_one()
        if not self.file:
            raise UserError(_("Seleccioná un archivo Excel para validar."))
        file_bytes = base64.b64decode(self.file)
        rows = ExcelParser(self.env).parse(file_bytes)
        result = RowValidator(self.env).validate(rows)

        session_vals = {
            'file_name': self.file_name,
            'file': self.file,
            'total_rows': result['total_rows'],
            'validation_json': result,
            'error_log': result['errors'],
            'warning_log': result['warnings'],
            'state': 'failed' if result['has_blocking_errors'] else 'validated',
        }
        if self.session_id and self.session_id.state != 'imported':
            self.session_id.write(session_vals)
        else:
            self.session_id = self.env['primate_stock_import.session'].create(session_vals)
        _logger.info(
            "Sesión %s validada: %s filas, %s errores, %s advertencias",
            self.session_id.name, result['total_rows'],
            len(result['errors']), len(result['warnings']),
        )
        self.state = 'validated'
        return self._reopen()

    def action_import(self):
        """Crea las operaciones en borrador a partir de la sesión validada.

        Returns:
            dict: action que reabre el asistente en el paso 'done'.

        Raises:
            UserError: si no hay sesión validada o tiene errores bloqueantes.
        """
        self.ensure_one()
        session = self.session_id
        if not session or session.state not in ('validated', 'failed'):
            raise UserError(_("Primero validá el archivo."))
        if session.has_errors:
            raise UserError(_(
                "No se puede importar con errores bloqueantes. Corregí el archivo y volvé "
                "a validar."
            ))
        groups = (session.validation_json or {}).get('groups') or []
        if not groups:
            raise UserError(_("La validación no produjo operaciones para crear."))

        pickings = PickingBuilder(self.env).build(groups, session)
        session.write({
            'state': 'imported',
            'move_count': len(pickings.move_ids),
        })
        _logger.info(
            "Sesión %s importada: %s operaciones, %s movimientos",
            session.name, len(pickings), len(pickings.move_ids),
        )
        self.state = 'done'
        return self._reopen()

    def action_back_to_upload(self):
        """Vuelve al paso de carga conservando la sesión para reutilizarla."""
        self.ensure_one()
        self.state = 'upload'
        return self._reopen()

    def action_view_pickings(self):
        """Abre las operaciones creadas por la sesión."""
        self.ensure_one()
        return self.session_id.action_view_pickings()

    def action_view_session(self):
        """Abre el formulario de la sesión de importación."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'primate_stock_import.session',
            'res_id': self.session_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_download_template(self):
        """Genera la plantilla Excel y la descarga.

        La plantilla tiene una hoja "Operaciones" con encabezados y filas de
        ejemplo, y una hoja "Instrucciones" con la descripción de cada columna.

        Returns:
            dict: act_url que descarga el binario desde el propio asistente.
        """
        self.ensure_one()
        self.template_file = base64.b64encode(self._build_template())
        return {
            'type': 'ir.actions.act_url',
            'url': (
                f'/web/content/?model={self._name}&id={self.id}&field=template_file'
                '&filename_field=template_file_name&download=true'
            ),
            'target': 'self',
        }

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------
    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _build_template(self):
        """Construye el .xlsx de la plantilla en memoria.

        Returns:
            bytes: contenido del archivo.
        """
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        header_format = workbook.add_format({
            'bold': True, 'font_color': '#FFFFFF', 'bg_color': '#4472C4',
            'align': 'center', 'valign': 'vcenter', 'border': 1,
        })
        required_format = workbook.add_format({
            'bold': True, 'font_color': '#FFFFFF', 'bg_color': '#C00000',
            'align': 'center', 'valign': 'vcenter', 'border': 1,
        })
        example_format = workbook.add_format({'font_color': '#808080', 'italic': True})
        wrap_format = workbook.add_format({'text_wrap': True, 'valign': 'top'})

        sheet = workbook.add_worksheet("Operaciones")
        for col, column in enumerate(COLUMNS):
            cell_format = required_format if column in REQUIRED_COLUMNS else header_format
            sheet.write(0, col, column, cell_format)
            sheet.set_column(col, col, max(14, len(column) + 4))
        for row_index, example in enumerate(EXAMPLE_ROWS, start=1):
            for col, value in enumerate(example):
                sheet.write(row_index, col, value, example_format)
        sheet.freeze_panes(1, 0)

        help_sheet = workbook.add_worksheet("Instrucciones")
        help_sheet.set_column(0, 0, 18)
        help_sheet.set_column(1, 1, 100)
        help_sheet.write(0, 0, "Columna", header_format)
        help_sheet.write(0, 1, "Descripción", header_format)
        for row_index, column in enumerate(COLUMNS, start=1):
            help_sheet.write(row_index, 0, column)
            help_sheet.write(row_index, 1, COLUMN_HELP[column], wrap_format)
        notes_row = len(COLUMNS) + 2
        help_sheet.write(notes_row, 0, "Notas", header_format)
        help_sheet.write(notes_row, 1, (
            "Las filas de ejemplo (en gris) deben borrarse antes de importar. "
            "Las columnas en rojo son obligatorias. La primera fila de cada operation_ref "
            "define el cabezal; el resto debe repetir los mismos valores de cabezal."
        ), wrap_format)
        workbook.close()
        return output.getvalue()

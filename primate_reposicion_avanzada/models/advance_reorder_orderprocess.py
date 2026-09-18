# -*- coding: utf-8 -*-
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)


class AdvanceReorderOrderProcess(models.Model):
    _inherit = 'advance.reorder.orderprocess'

    product_category_ids = fields.Many2many(
        'product.category',
        'primate_reorder_orderprocess_categ_rel',
        'orderprocess_id',
        'categ_id',
        string='Categorías de Producto',
        help='Carga asistida: agrega a Productos los de estas categorías y sus hijas.',
    )
    warehouse_group_ids = fields.Many2many(
        'stock.warehouse.group',
        'primate_reorder_orderprocess_wh_group_rel',
        'orderprocess_id',
        'warehouse_group_id',
        string='Grupos de Almacenes',
        help='Opcional. Si se completa, solo se cargan productos con alguno de estos grupos '
             'asignado, igual que en el proceso de reposición.',
    )
    multiplo_rounding_method = fields.Selection(
        [('ceil', 'Hacia arriba'),
         ('floor', 'Hacia abajo')],
        string='Redondeo al Múltiplo',
        default='ceil',
        help='Dirección de redondeo al ajustar la demanda y la cantidad de compra al '
             'múltiplo de distribución del producto.',
    )

    def action_load_products_by_category_and_group(self):
        """Carga masiva de productos por categoría y, opcionalmente, grupo de almacenes.

        Se intersecta con computed_product_ids, que ya encierra los filtros del propio Setu
        (purchase_ok, compañía y, si hay proveedor elegido, que tenga tarifa de ese
        proveedor) y es además el dominio de la vista.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_('Solo se pueden agregar productos mientras el proceso está en borrador.'))
        if not self.product_category_ids and not self.warehouse_group_ids:
            raise UserError(_('Elegí al menos una categoría o un grupo de almacenes.'))

        candidatos = self.env['product.product'].primate_products_from_categories_and_groups(
            self.product_category_ids, self.warehouse_group_ids)
        permitidos = candidatos & self.sudo().computed_product_ids
        nuevos = permitidos - self.product_ids
        if not nuevos:
            raise UserError(_(
                'La combinación elegida no aporta productos nuevos habilitados para este '
                'proceso. Si cargaste un grupo de almacenes, recordá que el producto tiene '
                'que tenerlo asignado.'))
        self.product_ids = [(4, product.id) for product in nuevos]
        self.message_post(body=_('Carga asistida: se agregaron %s productos.') % len(nuevos))
        # Sin acción de retorno para que el cliente web recargue el registro y los productos
        # aparezcan en el momento. Devolver una notificación no refresca la vista.
        return None

    def get_sales_data(self, config):
        """Garantiza una fila por producto, aunque no tenga ventas en el período.

        Mismo problema que en el proceso de reposición: el motor de Setu arma las líneas
        desde el resultado de la consulta de ventas, así que los productos sin rotación en la
        ventana quedaban sin línea, y si ninguno tenía ventas el proceso terminaba en el
        estado "Sin datos" sin explicar nada.
        """
        sales_data = super().get_sales_data(config) or []
        con_datos = {fila.get('product_id') for fila in sales_data}
        faltantes = self.product_ids.filtered(lambda p: p.id not in con_datos)
        if not faltantes:
            return sales_data

        for product in faltantes:
            fila = {'product_id': product.id, 'product_name': product.display_name, 'ads': 0.0}
            if self.generate_demand_with != 'history_sales':
                fila.update({'lead_days_demand_stock': 0.0, 'expected_sales_stock': 0.0})
            sales_data.append(fila)

        _logger.info(
            "Reorden %s / grupo %s: %s producto(s) sin ventas en el período, se les arma "
            "línea con demanda cero para no cortar el proceso.",
            self.name or self.id, config.warehouse_group_id.display_name, len(faltantes))
        return sales_data

    def action_reorder_confirm(self):
        """Rescata el proceso del estado "Sin datos" cuando sí hay líneas para revisar."""
        res = super().action_reorder_confirm()
        for proceso in self:
            if proceso.state == 'no_data' and proceso.line_ids:
                proceso.state = 'inprogress'
                sin_demanda = len(proceso.line_ids.filtered(lambda l: l.demand_adjustment_qty <= 0))
                proceso.message_post(body=_(
                    'El cálculo se completó con %(total)s línea(s), de las cuales %(cero)s '
                    'quedaron sin demanda. El proceso sigue en curso para que se pueda '
                    'revisar el detalle.',
                    total=len(proceso.line_ids), cero=sin_demanda))
        return res

    def prepare_reorder_line_vals(self, config, sales_data, generate_demand_with):
        """Ajusta la demanda calculada al múltiplo de distribución del producto.

        Setu escribe directo sobre las líneas que ya existen (line_id.write) y solo devuelve
        tuplas (0, 0, {...}) para las nuevas, así que hay que corregir los dos caminos.
        """
        vals = super().prepare_reorder_line_vals(config, sales_data, generate_demand_with)
        metodo = self.multiplo_rounding_method or 'ceil'
        product_obj = self.env['product.product']

        for command in vals:
            if command[0] != 0 or not command[2]:
                continue
            line_vals = command[2]
            demanda = line_vals.get('demand_adjustment_qty') or 0.0
            if demanda <= 0 or not line_vals.get('product_id'):
                continue
            product = product_obj.browse(line_vals['product_id'])
            line_vals['demand_adjustment_qty'] = product.primate_round_to_multiple(
                demanda, rounding_method=metodo)

        existentes = self.line_ids.filtered(
            lambda l: l.warehouse_group_id == config.warehouse_group_id and l.demand_adjustment_qty > 0)
        for linea in existentes:
            ajustada = linea.product_id.primate_round_to_multiple(
                linea.demand_adjustment_qty, rounding_method=metodo)
            rounding = linea.product_id.uom_id.rounding or 0.01
            if float_compare(ajustada, linea.demand_adjustment_qty, precision_rounding=rounding) != 0:
                linea.demand_adjustment_qty = ajustada

        return vals

    def _prepare_purchase_order_line_vals(self, fpos, warehouse_group_id):
        """Ajusta la cantidad de la línea de orden de compra al múltiplo del producto.

        El múltiplo se aplica sobre product_qty tal cual, sin convertir a la unidad de
        compra, incluso cuando el base setea product_uom = uom_po_id. Es el mismo criterio
        que ya usa automatic_crossdocking sobre purchase.order.line.product_qty, y mantener
        los dos flujos alineados pesa más que la pureza de unidades.
        """
        po_line_vals = super()._prepare_purchase_order_line_vals(fpos, warehouse_group_id)
        metodo = self.multiplo_rounding_method or 'ceil'
        product_obj = self.env['product.product']

        for command in po_line_vals:
            if command[0] != 0 or not command[2]:
                continue
            line_vals = command[2]
            cantidad = line_vals.get('product_qty') or 0.0
            if cantidad <= 0 or not line_vals.get('product_id'):
                continue
            product = product_obj.browse(line_vals['product_id'])
            line_vals['product_qty'] = product.primate_round_to_multiple(
                cantidad, rounding_method=metodo)

        return po_line_vals

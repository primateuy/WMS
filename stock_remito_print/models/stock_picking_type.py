# -*- coding: utf-8 -*-
from odoo import fields, models


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    remito_report_id = fields.Many2one(
        comodel_name='ir.actions.report',
        string='Reporte de remito',
        domain="[('model', '=', 'stock.picking')]",
        ondelete='set null',
        help='Reporte que se usa para imprimir el remito de este tipo de '
             'operación cuando no corresponde el PDF del CFE. Si queda '
             'vacío se usa el albarán de entrega estándar de Odoo.',
    )

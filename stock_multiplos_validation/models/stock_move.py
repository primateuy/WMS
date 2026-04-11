# -*- coding: utf-8 -*-
from odoo import models, fields


class StockMove(models.Model):
    _inherit = 'stock.move'

    multiplo_incumplido = fields.Boolean(
        string='No respeta múltiplo',
        default=False,
        help='Indica que la cantidad de esta línea no respeta el múltiplo de distribución del producto.',
    )

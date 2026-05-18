from odoo import models, api, fields


class StockQuantPackage(models.Model):
    _inherit = 'stock.quant.package'

    wis_id_externo = fields.Char(
        string='ID externo WIS',
        copy=False,
        help='IdExternoContenedor recibido en el webhook confirmacionPedido. '
             'Campo informativo para trazabilidad con WIS.',
    )

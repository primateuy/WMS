# -*- coding: utf-8 -*-
from odoo import models

from odoo.addons.reorden_rendimiento.models.stock_move import CTX_MASIVO


class StockMove(models.Model):
    _inherit = 'stock.move'

    def action_explode(self):
        """En procesos masivos, no buscar kits move por move si no existe ninguno."""
        if self and self.env.context.get(CTX_MASIVO):
            hay_kits = self.env['mrp.bom'].sudo().search_count(
                [('type', '=', 'phantom')], limit=1)
            if not hay_kits:
                return self
        return super().action_explode()

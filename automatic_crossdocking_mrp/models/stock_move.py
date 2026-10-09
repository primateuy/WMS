# -*- coding: utf-8 -*-
from odoo import models


class StockMove(models.Model):
    _inherit = 'stock.move'

    def action_explode(self):
        """En el armado del crossdock, no buscar kits move por move si no existe ninguno.

        Si mañana se define un kit, la consulta previa lo encuentra y se vuelve al
        comportamiento estándar de MRP sin tocar nada.
        """
        if self and self.env.context.get('crossdock_armado'):
            hay_kits = self.env['mrp.bom'].sudo().search_count(
                [('type', '=', 'phantom')], limit=1)
            if not hay_kits:
                return self
        return super().action_explode()

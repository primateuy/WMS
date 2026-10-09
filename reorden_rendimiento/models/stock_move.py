# -*- coding: utf-8 -*-
"""Atajos para procesos masivos de stock (armado de crossdock, reposición de muchas reglas).

Con `stock_proceso_masivo` en el contexto, dos búsquedas que el core hace movimiento por
movimiento se saltean si una sola consulta previa confirma que no hay nada que encontrar.
Si mañana se crea una regla push o un punto de reorden automático, la consulta lo
encuentra y se vuelve al comportamiento estándar sin tocar nada.
"""
from odoo import models

CTX_MASIVO = 'stock_proceso_masivo'


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _push_apply(self):
        """Sin reglas push que salgan de esas ubicaciones, el resultado es vacío.

        Los moves encadenados no se empujan (el core los saltea por tener destino), pero los
        del último eslabón pasan por una búsqueda de regla cada uno."""
        if self and self.env.context.get(CTX_MASIVO):
            hay_reglas = self.env['stock.rule'].sudo().search_count([
                ('location_src_id', 'in', self.location_dest_id.ids),
                ('action', 'in', ('push', 'pull_push')),
            ], limit=1)
            if not hay_reglas:
                return self.env['stock.move']
        return super()._push_apply()

    def _trigger_scheduler(self):
        """Sin puntos de reorden automáticos para estos productos, no hay nada que disparar.

        El core busca uno por cada move confirmado."""
        if self and self.env.context.get(CTX_MASIVO):
            hay_automaticos = self.env['stock.warehouse.orderpoint'].sudo().search_count([
                ('trigger', '=', 'auto'),
                ('product_id', 'in', self.product_id.ids),
            ], limit=1)
            if not hay_automaticos:
                return
        return super()._trigger_scheduler()

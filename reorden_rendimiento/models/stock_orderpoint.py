# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import models
from odoo.tools import float_compare, frozendict


class StockWarehouseOrderpoint(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    def _compute_qty_to_order(self):
        """Mismo cálculo que el core (`stock/models/stock_orderpoint.py`), en lote.

        El core, para cada regla por debajo del mínimo, lee el pronóstico con la ventana
        de visibilidad y llama a `_quantity_in_progress()` de esa regla sola: en Forum son
        ≈ 6-7 ms por regla, porque «en proceso» consulta compras, requisiciones y
        fabricación (que además busca listas de materiales). Acá se agrupan las reglas por
        el mismo contexto de producto y se consulta una vez por grupo. Todos los
        `_quantity_in_progress` del core trabajan sobre conjuntos y devuelven el valor de
        cada regla, así que el resultado es el mismo.

        No llama a `super()`: reemplaza el cálculo. Las dependencias (`@api.depends`) del
        core y de `purchase_stock` se mantienen, porque Odoo las junta de toda la cadena.
        """
        por_contexto = defaultdict(list)
        for orderpoint in self:
            if not orderpoint.product_id or not orderpoint.location_id:
                orderpoint.qty_to_order = False
                continue
            rounding = orderpoint.product_uom.rounding
            # Igual que el core: la visibilidad sólo cuenta si el pronóstico ya quedó por
            # debajo del mínimo.
            if float_compare(orderpoint.qty_forecast, orderpoint.product_min_qty,
                             precision_rounding=rounding) < 0:
                contexto = frozendict(orderpoint._get_product_context(
                    visibility_days=orderpoint.visibility_days))
                por_contexto[contexto].append(orderpoint.id)
            else:
                orderpoint.qty_to_order = 0.0

        for contexto, ids in por_contexto.items():
            grupo = self.browse(ids)
            disponible = {
                p['id']: p['virtual_available']
                for p in grupo.product_id.with_context(contexto).read(['virtual_available'])
            }
            en_proceso = grupo._quantity_in_progress()
            for orderpoint in grupo:
                rounding = orderpoint.product_uom.rounding
                pronostico = disponible[orderpoint.product_id.id] + en_proceso[orderpoint.id]
                qty_to_order = max(orderpoint.product_min_qty, orderpoint.product_max_qty) - pronostico
                remainder = orderpoint.qty_multiple > 0.0 and qty_to_order % orderpoint.qty_multiple or 0.0
                if (float_compare(remainder, 0.0, precision_rounding=rounding) > 0
                        and float_compare(orderpoint.qty_multiple - remainder, 0.0,
                                          precision_rounding=rounding) > 0):
                    qty_to_order += orderpoint.qty_multiple - remainder
                orderpoint.qty_to_order = qty_to_order

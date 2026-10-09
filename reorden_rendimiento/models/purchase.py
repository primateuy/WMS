# -*- coding: utf-8 -*-
"""Las operaciones de compra que disparan el recálculo de la cantidad a pedir.

`purchase_stock` hace depender `qty_to_order` de `product_id.purchase_order_line_ids`
(cantidad y estado): crear, editar, confirmar o cancelar líneas recalcula las reglas de
esas variantes. Y al confirmar, si el proveedor es nuevo para el producto, Odoo lo agrega
a la PLANTILLA y se recalculan las reglas de todas sus variantes.
"""
from odoo import api, models

CAMPOS_LINEA = {'product_qty', 'product_uom_qty', 'product_id', 'state', 'product_uom'}


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    @api.model_create_multi
    def create(self, vals_list):
        ids = {v['product_id'] for v in vals_list if v.get('product_id')}
        productos = self.env['product.product'].browse(ids)
        with self.env['reorden.recalculo.pendiente'].diferir(
                productos=productos, origen="Líneas de compra"):
            return super().create(vals_list)

    def write(self, vals):
        if not CAMPOS_LINEA & set(vals):
            return super().write(vals)
        productos = self.product_id
        if vals.get('product_id'):
            productos |= self.env['product.product'].browse(vals['product_id'])
        with self.env['reorden.recalculo.pendiente'].diferir(
                productos=productos, origen="Líneas de compra"):
            return super().write(vals)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def _reorden_objetivos(self):
        """(variantes, plantillas) cuyas reglas va a recalcular la confirmación.

        Las variantes de las líneas siempre (cambia el estado de la línea). La plantilla
        entera sólo cuando Odoo le va a agregar el proveedor, con la misma condición que
        `purchase.order._add_supplier_to_product`.
        """
        productos = self.order_line.product_id
        plantillas = self.env['product.template']
        for orden in self:
            partner = orden.partner_id if not orden.partner_id.parent_id else orden.partner_id.parent_id
            for linea in orden.order_line.filtered('product_id'):
                vendedores = linea.product_id.seller_ids
                ya_es = (partner | orden.partner_id) & vendedores.partner_id
                if not ya_es and len(vendedores) <= 10:
                    plantillas |= linea.product_id.product_tmpl_id
        return productos, plantillas

    def _reorden_ordenes_a_encolar(self):
        """Órdenes cuyo recálculo diferido se encola al terminar la confirmación.

        Gancho para módulos que lo encolan más tarde (el armado del crossdock lo hace al
        terminar de armar, para no recalcular dos veces)."""
        return self

    def button_confirm(self):
        Recalculo = self.env['reorden.recalculo.pendiente']
        productos, plantillas = self._reorden_objetivos()
        with Recalculo.diferir(productos=productos, plantillas=plantillas,
                               encolar=False) as diferido:
            res = super().button_confirm()
        if diferido:
            ordenes = self._reorden_ordenes_a_encolar()
            if ordenes:
                productos = ordenes.order_line.product_id
                Recalculo._encolar(productos=productos,
                                   plantillas=plantillas & productos.product_tmpl_id,
                                   origen="Confirmación de compra")
        return res

    def button_cancel(self):
        with self.env['reorden.recalculo.pendiente'].diferir(
                productos=self.order_line.product_id, origen="Cancelación de compra"):
            return super().button_cancel()

    def button_draft(self):
        with self.env['reorden.recalculo.pendiente'].diferir(
                productos=self.order_line.product_id, origen="Compra a borrador"):
            return super().button_draft()

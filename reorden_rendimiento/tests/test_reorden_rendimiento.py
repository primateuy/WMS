# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.addons.stock.models.stock_orderpoint import StockWarehouseOrderpoint as OrderpointCore
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'reorden_rendimiento')
class TestReordenRendimiento(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.almacen = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        cls.proveedor = cls.env['res.partner'].create({'name': 'Proveedor reorden test'})
        atributo = cls.env['product.attribute'].create({
            'name': 'Talle reorden test', 'create_variant': 'always',
            'value_ids': [(0, 0, {'name': n}) for n in ('S', 'M', 'L', 'XL')]})
        cls.plantilla = cls.env['product.template'].create({
            'name': 'Remera reorden test', 'type': 'product',
            'attribute_line_ids': [(0, 0, {'attribute_id': atributo.id,
                                           'value_ids': [(6, 0, atributo.value_ids.ids)]})]})
        cls.variantes = cls.plantilla.product_variant_ids
        Ubicacion = cls.env['stock.location']
        cls.ubicaciones = Ubicacion.create([
            {'name': 'Reorden test %s' % i, 'usage': 'internal',
             'location_id': cls.almacen.lot_stock_id.id} for i in range(3)])
        Orderpoint = cls.env['stock.warehouse.orderpoint']
        vals = []
        # Casos variados: por encima y por debajo del mínimo, múltiplos, visibilidad.
        for i, variante in enumerate(cls.variantes):
            for j, ubicacion in enumerate(cls.ubicaciones):
                vals.append({
                    'product_id': variante.id, 'location_id': ubicacion.id,
                    'warehouse_id': cls.almacen.id, 'trigger': 'manual',
                    'product_min_qty': (i + j) % 3 * 5, 'product_max_qty': 20 + i,
                    'qty_multiple': (1, 4, 6)[j], 'visibility_days': j,
                })
        cls.puntos = Orderpoint.create(vals)
        # Stock en algunas ubicaciones, para que no todas queden por debajo del mínimo.
        Quant = cls.env['stock.quant']
        for k, punto in enumerate(cls.puntos[::2]):
            Quant._update_available_quantity(punto.product_id, punto.location_id, 3 + k)
        cls.env['ir.config_parameter'].sudo().set_param('reorden_rendimiento.umbral', '0')

    def _core(self, puntos):
        """El valor que daría el cálculo del core, punto por punto."""
        puntos.invalidate_recordset(['qty_to_order', 'qty_forecast'])
        resultado = {}
        for punto in puntos:
            copia = punto.with_context(prueba_core=True)
            OrderpointCore._compute_qty_to_order(copia)
            resultado[punto.id] = copia.qty_to_order
        return resultado

    def _lote(self, puntos):
        puntos.invalidate_recordset(['qty_to_order', 'qty_forecast'])
        self.env.add_to_compute(puntos._fields['qty_to_order'], puntos)
        puntos.flush_recordset(['qty_to_order'])
        puntos.invalidate_recordset(['qty_to_order'])
        return {p.id: p.qty_to_order for p in puntos}

    # ------------------------------------------------------------------
    def test_calculo_en_lote_igual_al_del_core(self):
        compra = self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'order_line': [(0, 0, {'product_id': v.id, 'product_qty': 7, 'price_unit': 1})
                           for v in self.variantes[:2]]})
        self.env.flush_all()
        lote = self._lote(self.puntos)
        core = self._core(self.puntos)
        self.assertEqual(lote, core)
        self.assertTrue(any(lote.values()), "la prueba tiene que tener reglas con algo para pedir")
        compra.button_cancel()

    def test_lineas_de_compra_difieren_el_recalculo(self):
        espiados = []
        original = type(self.puntos)._compute_qty_to_order

        def espia(registros):
            espiados.extend(registros.ids)
            return original(registros)

        with patch.object(type(self.puntos), '_compute_qty_to_order', espia):
            self.env['purchase.order'].create({
                'partner_id': self.proveedor.id,
                'order_line': [(0, 0, {'product_id': self.variantes[0].id, 'product_qty': 5,
                                       'price_unit': 1})]})
            self.env.flush_all()
            self.assertFalse(set(espiados) & set(self.puntos.ids))
            pendiente = self.env['reorden.recalculo.pendiente'].search(
                [('product_id', '=', self.variantes[0].id), ('estado', '=', 'pendiente')])
            self.assertTrue(pendiente)
            self.env['reorden.recalculo.pendiente']._cron_recalcular()
            recalculados = self.puntos.filtered(lambda p: p.product_id == self.variantes[0])
            self.assertTrue(set(recalculados.ids) <= set(espiados))
        self.assertEqual(pendiente.estado, 'hecho')

    def test_debajo_del_umbral_recalcula_en_el_momento(self):
        self.env['ir.config_parameter'].sudo().set_param('reorden_rendimiento.umbral', '100000')
        self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'order_line': [(0, 0, {'product_id': self.variantes[1].id, 'product_qty': 5,
                                   'price_unit': 1})]})
        self.env.flush_all()
        self.assertFalse(self.env['reorden.recalculo.pendiente'].search(
            [('product_id', '=', self.variantes[1].id), ('estado', '=', 'pendiente')]))

    def test_confirmar_con_proveedor_nuevo_encola_la_plantilla(self):
        compra = self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'order_line': [(0, 0, {'product_id': self.variantes[0].id, 'product_qty': 5,
                                   'price_unit': 1})]})
        self.env['reorden.recalculo.pendiente'].search([]).unlink()
        compra.button_confirm()
        Pendiente = self.env['reorden.recalculo.pendiente']
        self.assertTrue(Pendiente.search([('product_tmpl_id', '=', self.plantilla.id)]),
                        "proveedor nuevo: se recalcula la plantilla entera")
        # Segunda compra al mismo proveedor: ya figura en la ficha, sólo la variante.
        Pendiente.search([]).unlink()
        compra2 = self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'order_line': [(0, 0, {'product_id': self.variantes[0].id, 'product_qty': 5,
                                   'price_unit': 1})]})
        Pendiente.search([]).unlink()
        compra2.button_confirm()
        self.assertFalse(Pendiente.search([('product_tmpl_id', '=', self.plantilla.id)]))
        self.assertTrue(Pendiente.search([('product_id', '=', self.variantes[0].id)]))

    def test_atajos_de_proceso_masivo(self):
        Move = self.env['stock.move']
        move = Move.create({
            'name': 'atajo', 'product_id': self.variantes[0].id, 'product_uom_qty': 1,
            'product_uom': self.variantes[0].uom_id.id,
            'location_id': self.ubicaciones[0].id, 'location_dest_id': self.ubicaciones[1].id})
        with patch.object(type(Move.env['stock.rule']), 'search_count', autospec=True,
                          return_value=0), \
                patch('odoo.addons.stock.models.stock_move.StockMove._push_apply',
                      autospec=True) as push_core:
            res = move.with_context(stock_proceso_masivo=True)._push_apply()
        push_core.assert_not_called()
        self.assertFalse(res)
        self.assertTrue(move._push_apply() is not None)

    def test_la_plantilla_reemplaza_a_sus_variantes_en_la_cola(self):
        Pendiente = self.env['reorden.recalculo.pendiente']
        Pendiente.search([]).unlink()
        Pendiente._encolar(productos=self.variantes[:2])
        Pendiente._encolar(plantillas=self.plantilla)
        pendientes = Pendiente.search([('estado', '=', 'pendiente')])
        self.assertEqual(pendientes.product_tmpl_id, self.plantilla)
        self.assertFalse(pendientes.product_id, "las variantes ya las cubre la plantilla")
        Pendiente._encolar(productos=self.variantes[2:])
        self.assertEqual(Pendiente.search([('estado', '=', 'pendiente')]), pendientes)

    def test_un_lote_junta_varias_entradas(self):
        Pendiente = self.env['reorden.recalculo.pendiente']
        Pendiente.search([]).unlink()
        Pendiente._encolar(productos=self.variantes)
        pasadas = []
        original = type(self.puntos)._compute_qty_to_order

        def espia(registros):
            pasadas.append(len(registros))
            return original(registros)

        with patch.object(type(self.puntos), '_compute_qty_to_order', espia):
            Pendiente._cron_recalcular()
        self.assertEqual(len(pasadas), 1, "una sola pasada para las cuatro variantes")
        self.assertEqual(pasadas[0], len(self.puntos))
        self.assertFalse(Pendiente.search([('estado', '=', 'pendiente')]))

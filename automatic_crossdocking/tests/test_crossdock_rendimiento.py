# -*- coding: utf-8 -*-
"""Armado del crossdock: cadena de moves, segundo plano y puntos de reorden diferidos."""
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'crossdock_rendimiento')
class TestCrossdockRendimiento(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.almacen = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        cls.proveedor = cls.env['res.partner'].create({'name': 'Proveedor crossdock test'})
        cls.productos = cls.env['product.product'].create([
            {'name': 'Crossdock test A', 'type': 'product'},
            {'name': 'Crossdock test B', 'type': 'product'},
        ])
        Location = cls.env['stock.location']
        padre = cls.almacen.view_location_id
        cls.entrada, cls.intermedia1, cls.cross1, cls.cross2, cls.tienda1, cls.tienda2 = [
            Location.create({'name': n, 'usage': 'internal', 'location_id': padre.id})
            for n in ('Entrada', 'Intermedia 1', 'Crossdock 1', 'Crossdock 2', 'Tienda 1', 'Tienda 2')
        ]
        cls.tipo = cls.env['stock.picking.type'].create({
            'name': 'Crossdock test', 'code': 'internal', 'sequence_code': 'XDT',
            'warehouse_id': cls.almacen.id, 'reservation_method': 'manual',
        })

    def _orden(self, crossdock=True, qty=10):
        return self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'crossdock_enabled': crossdock,
            'order_line': [(0, 0, {
                'product_id': p.id, 'product_qty': qty, 'price_unit': 1,
                'use_crossdock': crossdock,
            }) for p in self.productos],
        })

    def _picking(self, orden, origen, destino, cadena=None):
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.tipo.id,
            'location_id': origen.id, 'location_dest_id': destino.id,
        })
        vals = [{
            'name': 'xd', 'product_id': linea.product_id.id, 'product_uom_qty': 5,
            'product_uom': linea.product_uom.id, 'location_id': origen.id,
            'location_dest_id': destino.id, 'picking_id': picking.id,
            'purchase_line_id': linea.id, 'date': fields.Datetime.now(),
        } for linea in orden.order_line]
        return orden._crossdock_crear_moves(vals, cadena)

    def _enlaces(self, moves):
        return {(o, m) for m in moves for o in m.move_orig_ids}

    # ------------------------------------------------------------------
    # Cadena de moves
    # ------------------------------------------------------------------
    def test_moves_nacen_encadenados_dentro_de_su_cadena(self):
        orden = self._orden()
        proveedor = self.proveedor.property_stock_supplier
        recepcion = self._picking(orden, proveedor, self.entrada)
        cadena = orden._crossdock_cadena_inicial()  # vacía: la recepción no es "Recepción Crossdock"
        for m in recepcion:
            cadena[(m.location_dest_id.id, m.product_id.id)].append(m.id)

        inter = self._picking(orden, self.entrada, self.intermedia1, cadena)
        cross1 = self._picking(orden, self.intermedia1, self.cross1, cadena)
        tienda1 = self._picking(orden, self.cross1, self.tienda1, cadena)
        cross2 = self._picking(orden, self.entrada, self.cross2, cadena)
        tienda2 = self._picking(orden, self.cross2, self.tienda2, cadena)

        for anterior, siguiente in ((recepcion, inter), (inter, cross1), (cross1, tienda1),
                                    (recepcion, cross2), (cross2, tienda2)):
            for move in siguiente:
                esperado = anterior.filtered(lambda m: m.product_id == move.product_id)
                self.assertEqual(move.move_orig_ids, esperado)
        # Nada cruza entre sucursales.
        self.assertFalse(tienda1.move_orig_ids & (cross2 | tienda2))
        self.assertFalse(tienda2.move_orig_ids & (cross1 | tienda1 | inter))
        self.assertEqual(sorted(inter.mapped('sequence')), [5, 10])

    def test_setup_dependencies_enlaza_contiguos_y_es_idempotente(self):
        orden = self._orden()
        proveedor = self.proveedor.property_stock_supplier
        recepcion = self._picking(orden, proveedor, self.entrada)
        cross1 = self._picking(orden, self.entrada, self.cross1)
        tienda1 = self._picking(orden, self.cross1, self.tienda1)
        cross2 = self._picking(orden, self.entrada, self.cross2)
        tienda2 = self._picking(orden, self.cross2, self.tienda2)
        todos = recepcion | cross1 | tienda1 | cross2 | tienda2
        todos._action_confirm()

        orden._setup_picking_dependencies()
        enlaces = self._enlaces(todos)
        # 4 eslabones contiguos × 2 productos, ninguno al revés ni entre sucursales.
        self.assertEqual(len(enlaces), 8)
        for origen, destino in enlaces:
            self.assertEqual(origen.location_dest_id, destino.location_id)
            self.assertEqual(origen.product_id, destino.product_id)
        self.assertTrue(all(m.state == 'waiting' for m in (cross1 | tienda1 | cross2 | tienda2)))

        orden._setup_picking_dependencies()
        todos.invalidate_recordset()
        self.assertEqual(self._enlaces(todos), enlaces)

    # ------------------------------------------------------------------
    # Armado en segundo plano
    # ------------------------------------------------------------------
    def test_confirmar_encola_el_armado(self):
        orden = self._orden()
        with patch.object(type(orden), '_crossdock_armar', autospec=True) as armar:
            orden.button_confirm()
        armar.assert_not_called()
        self.assertEqual(orden.state, 'purchase')
        self.assertEqual(orden.crossdock_armado_estado, 'pendiente')
        self.assertFalse(orden.picking_ids)
        cron = self.env.ref('automatic_crossdocking.ir_cron_armar_crossdock')
        self.assertTrue(self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)]))

    def test_cron_arma_y_deja_listo(self):
        orden = self._orden()
        orden.button_confirm()
        with patch.object(type(orden), '_crossdock_armar', autospec=True) as armar:
            self.env['purchase.order']._cron_armar_crossdock()
        armar.assert_called_once()
        self.assertEqual(orden.crossdock_armado_estado, 'listo')
        self.assertEqual(orden.crossdock_armado_intentos, 1)

    def test_armado_fallido_igual_encola_el_reorden(self):
        """La confirmación ya cambió las líneas: el recálculo diferido se debe hacer igual."""
        self.env['stock.warehouse.orderpoint'].create({
            'product_id': self.productos[0].id,
            'location_id': self.almacen.lot_stock_id.id,
            'warehouse_id': self.almacen.id,
            'product_min_qty': 1, 'product_max_qty': 2, 'trigger': 'manual',
        })
        orden = self._orden()
        orden.button_confirm()
        with patch.object(type(orden), '_crossdock_armar', autospec=True,
                          side_effect=Exception("falta configuración")):
            self.env['purchase.order']._cron_armar_crossdock()
        self.assertEqual(orden.crossdock_armado_estado, 'error')
        self.assertTrue(self.env['crossdock.reorden.pendiente'].search([
            ('product_tmpl_id', '=', self.productos[0].product_tmpl_id.id),
            ('estado', '=', 'pendiente')]))

    def test_cron_registra_el_error_y_permite_reintentar(self):
        orden = self._orden()
        orden.button_confirm()
        with patch.object(type(orden), '_crossdock_armar', autospec=True,
                          side_effect=Exception("falta configuración")):
            self.env['purchase.order']._cron_armar_crossdock()
        self.assertEqual(orden.crossdock_armado_estado, 'error')
        self.assertIn("falta configuración", orden.crossdock_armado_error)

        orden.action_crossdock_reintentar_armado()
        self.assertEqual(orden.crossdock_armado_estado, 'pendiente')
        self.assertEqual(orden.crossdock_armado_intentos, 0)

    def test_cron_no_reintenta_para_siempre_un_armado_que_se_corta(self):
        orden = self._orden()
        orden.button_confirm()
        orden.crossdock_armado_intentos = 3  # tres corridas cortadas por tiempo
        with patch.object(type(orden), '_crossdock_armar', autospec=True) as armar:
            self.env['purchase.order']._cron_armar_crossdock()
        armar.assert_not_called()
        self.assertEqual(orden.crossdock_armado_estado, 'error')

    def test_usuario_de_compras_puede_confirmar(self):
        """La cola de puntos de reorden y el cron son internos: un comprador sin permisos
        sobre ellos tiene que poder confirmar igual."""
        comprador = self.env['res.users'].create({
            'name': 'Comprador crossdock', 'login': 'comprador_crossdock_test',
            'groups_id': [(6, 0, [self.env.ref('purchase.group_purchase_user').id,
                                  self.env.ref('stock.group_stock_user').id])],
        })
        self.env['stock.warehouse.orderpoint'].create({
            'product_id': self.productos[0].id,
            'location_id': self.almacen.lot_stock_id.id,
            'warehouse_id': self.almacen.id,
            'product_min_qty': 1, 'product_max_qty': 2, 'trigger': 'manual',
        })
        orden = self._orden()
        orden.with_user(comprador).button_confirm()
        self.assertEqual(orden.state, 'purchase')
        self.assertEqual(orden.crossdock_armado_estado, 'pendiente')
        with patch.object(type(orden), '_crossdock_armar', autospec=True):
            self.env['purchase.order']._cron_armar_crossdock()
        self.assertTrue(self.env['crossdock.reorden.pendiente'].search([
            ('product_tmpl_id', '=', self.productos[0].product_tmpl_id.id)]))

    def test_orden_sin_crossdock_no_cambia(self):
        orden = self._orden(crossdock=False)
        orden.button_confirm()
        self.assertFalse(orden.crossdock_armado_estado)
        self.assertTrue(orden.picking_ids)

    # ------------------------------------------------------------------
    # Puntos de reorden diferidos
    # ------------------------------------------------------------------
    def test_puntos_de_reorden_se_recalculan_en_diferido(self):
        Orderpoint = self.env['stock.warehouse.orderpoint']
        punto = Orderpoint.create({
            'product_id': self.productos[0].id,
            'location_id': self.almacen.lot_stock_id.id,
            'warehouse_id': self.almacen.id,
            'product_min_qty': 5, 'product_max_qty': 10, 'trigger': 'manual',
        })
        # El core también recalcula los archivados: hay que protegerlos igual.
        archivado = Orderpoint.create({
            'product_id': self.productos[1].id,
            'location_id': self.almacen.lot_stock_id.id,
            'warehouse_id': self.almacen.id,
            'product_min_qty': 5, 'product_max_qty': 10, 'trigger': 'manual',
        })
        archivado.active = False
        orden = self._orden(qty=100)
        self.env.flush_all()

        clase = type(Orderpoint)
        original = clase._compute_qty_to_order
        recalculados = []

        def espia(registros):
            recalculados.extend(registros.ids)
            return original(registros)

        with patch.object(clase, '_compute_qty_to_order', espia):
            orden.button_confirm()
            self.env.flush_all()
            self.assertNotIn(punto.id, recalculados,
                             "la confirmación no recalcula: lo deja para el cron")
            self.assertNotIn(archivado.id, recalculados)
            Pendiente = self.env['crossdock.reorden.pendiente']
            dominio = [('product_tmpl_id', '=', self.productos[0].product_tmpl_id.id),
                       ('estado', '=', 'pendiente')]
            self.assertFalse(Pendiente.search(dominio),
                             "con armado en segundo plano se encola al terminar el armado")

            with patch.object(type(orden), '_crossdock_armar', autospec=True):
                self.env['purchase.order']._cron_armar_crossdock()
            self.assertNotIn(punto.id, recalculados)
            pendiente = Pendiente.search(dominio)
            self.assertTrue(pendiente)

            self.env['crossdock.reorden.pendiente']._cron_recalcular()
            self.assertIn(punto.id, recalculados)
            self.assertIn(archivado.id, recalculados)
        self.assertEqual(pendiente.estado, 'hecho')
        punto.invalidate_recordset(['qty_to_order'])
        diferido = punto.qty_to_order

        self.env.add_to_compute(Orderpoint._fields['qty_to_order'], punto)
        punto.flush_recordset(['qty_to_order'])
        punto.invalidate_recordset(['qty_to_order'])
        self.assertEqual(diferido, punto.qty_to_order, "el cron deja el mismo valor que el core")

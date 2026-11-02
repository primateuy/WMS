# -*- coding: utf-8 -*-
"""Individualización de operaciones con WIS (Forum, documento del 21-10-2026).

Un movimiento nuevo no se suma a un parcial pendiente de un tipo de WIS, ni a un envío
cuya preparación ya está en WIS. Los tipos sin WIS se siguen agrupando como siempre.
Nada sale a WIS: todo corre con `skip_wms_integration`.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'wis_individualizar')
class TestNoAgruparParciales(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_wms_integration=True,
                                       _avoid_wms=True, tracking_disable=True))
        cls.almacen = cls.env['stock.warehouse'].create({'name': 'Central indiv', 'code': 'CIN'})
        Location = cls.env['stock.location']
        cls.salida = Location.create({'name': 'Salida indiv', 'usage': 'internal',
                                      'location_id': cls.almacen.view_location_id.id})
        cls.tienda = Location.create({'name': 'Tienda indiv', 'usage': 'internal',
                                      'location_id': cls.almacen.view_location_id.id})
        Tipo = cls.env['stock.picking.type']
        base = {'code': 'internal', 'warehouse_id': cls.almacen.id}
        cls.tipo_prep = Tipo.create(dict(base, name='Preparo indiv', sequence_code='PIN',
                                         integracion_wms=True, tipo_pedido_wis='REP'))
        cls.tipo_envio = Tipo.create(dict(base, name='Envíos indiv', sequence_code='EIN',
                                          adquiere_codigo_unico_wms=True))
        cls.tipo_comun = Tipo.create(dict(base, name='Traslado indiv', sequence_code='TIN'))
        cls.grupo = cls.env['procurement.group'].create({'name': 'Solicitud indiv'})
        cls.productos = cls.env['product.product'].create([
            {'name': 'Remera indiv %s' % i, 'type': 'product'} for i in range(6)])

    def _move(self, tipo, origen, destino, producto, **extra):
        move = self.env['stock.move'].create(dict({
            'name': producto.name, 'product_id': producto.id, 'product_uom_qty': 4,
            'product_uom': producto.uom_id.id, 'location_id': origen.id,
            'location_dest_id': destino.id, 'picking_type_id': tipo.id,
            'group_id': self.grupo.id, 'procure_method': 'make_to_stock',
        }, **extra))
        move._action_confirm(merge=False)
        return move

    def _prep(self, producto):
        return self._move(self.tipo_prep, self.almacen.lot_stock_id, self.salida, producto)

    def _envio(self, producto, prep_move):
        return self._move(self.tipo_envio, self.salida, self.tienda, producto,
                          procure_method='make_to_order',
                          move_orig_ids=[(6, 0, prep_move.ids)])

    # --- parciales -------------------------------------------------------
    def test_01_un_parcial_de_wis_no_recibe_movimientos(self):
        primero = self._prep(self.productos[0])
        parcial = primero.picking_id
        parcial.backorder_id = self.env['stock.picking'].create({
            'picking_type_id': self.tipo_prep.id,
            'location_id': self.almacen.lot_stock_id.id, 'location_dest_id': self.salida.id})
        nuevo = self._prep(self.productos[1])
        self.assertTrue(nuevo.picking_id)
        self.assertNotEqual(nuevo.picking_id, parcial)

    def test_02_sin_parcial_se_agrupa_como_antes(self):
        primero = self._prep(self.productos[0])
        nuevo = self._prep(self.productos[1])
        self.assertEqual(nuevo.picking_id, primero.picking_id)

    def test_03_un_tipo_sin_wis_se_sigue_agrupando(self):
        primero = self._move(self.tipo_comun, self.salida, self.tienda, self.productos[0])
        primero.picking_id.backorder_id = self.env['stock.picking'].create({
            'picking_type_id': self.tipo_comun.id,
            'location_id': self.salida.id, 'location_dest_id': self.tienda.id})
        nuevo = self._move(self.tipo_comun, self.salida, self.tienda, self.productos[1])
        self.assertEqual(nuevo.picking_id, primero.picking_id)

    # --- envíos ----------------------------------------------------------
    def test_04_el_envio_no_recibe_lo_de_otra_preparacion(self):
        """La preparación 1 ya está en WIS: la línea nueva sale de la preparación 2 y su
        envío tiene que ser otro, o el envío 1 llevaría lo que WIS nunca preparó."""
        prep1 = self._prep(self.productos[0])
        envio1 = self._envio(self.productos[0], prep1)
        prep1.picking_id.wms_estado = 'enviado'
        prep2 = self._prep(self.productos[1])
        self.assertNotEqual(prep2.picking_id, prep1.picking_id)
        envio2 = self._envio(self.productos[1], prep2)
        self.assertNotEqual(envio2.picking_id, envio1.picking_id)

    def test_05_mientras_la_preparacion_recibe_lineas_el_envio_tambien(self):
        prep1 = self._prep(self.productos[0])
        envio1 = self._envio(self.productos[0], prep1)
        prep1.picking_id.wms_estado = 'en_cola'
        prep2 = self._prep(self.productos[1])
        self.assertEqual(prep2.picking_id, prep1.picking_id)
        envio2 = self._envio(self.productos[1], prep2)
        self.assertEqual(envio2.picking_id, envio1.picking_id)

    def test_06_el_parcial_de_un_envio_no_recibe_movimientos(self):
        """Entrega a Tiendas – Crossdocking: el saldo de una entrega parcial."""
        prep1 = self._prep(self.productos[0])
        envio1 = self._envio(self.productos[0], prep1)
        envio1.picking_id.backorder_id = self.env['stock.picking'].create({
            'picking_type_id': self.tipo_envio.id,
            'location_id': self.salida.id, 'location_dest_id': self.tienda.id})
        prep2 = self._prep(self.productos[1])
        envio2 = self._envio(self.productos[1], prep2)
        self.assertNotEqual(envio2.picking_id, envio1.picking_id)

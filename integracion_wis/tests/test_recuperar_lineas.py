# -*- coding: utf-8 -*-
"""Recuperar las líneas que WIS nunca recibió (producción, 05 al 24-11-2026: 28.177 líneas).

La reposición en tandas dejó líneas en pedidos que WIS ya tenía, y cuando WIS preparaba el
pedido esas líneas pasaban a un saldo con el mismo código. Se recuperan moviéndolas a un
pedido nuevo por sucursal, con código propio, sin duplicar y sin cortar la entrega a la tienda.
"""
import json

from odoo.tests.common import tagged

from .test_webhook_multipedido import WisMultipedidoCase


@tagged('post_install', '-at_install', 'wis_recuperar_lineas')
class TestRecuperarLineas(WisMultipedidoCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.producto_c = cls._producto('Campera multipedido', 'PRD-TMP-C')
        cls.Picking = cls.env['stock.picking']
        cls.Cola = cls.env['wis.picking.cola']

    def setUp(self):
        super().setUp()
        self.env['stock.quant']._update_available_quantity(self.producto_c, self.origen, 100)

    # ------------------------------------------------------------------
    # Ayudas
    # ------------------------------------------------------------------
    def _enviado(self, picking, productos, cajas=False):
        """Registra el /Pedido/Create que salió para `picking` con sólo `productos`."""
        pedido = {'nroPedido': picking.codigo_unico, 'detalles': [
            {'codigoProducto': p.codigo_unico, 'cantidad': 3} for p in productos]}
        if cajas:
            pedido['lpns'] = [{'idExterno': 'CAJA-1', 'tipo': 'FINTEMP'}]
        self.env['wis.envio.log'].create({
            'metodo': 'POST', 'endpoint': '/api/Pedido/Create', 'resultado': 'exito',
            'codigo_unico': picking.codigo_unico, 'picking_ref': picking.id,
            'request': json.dumps({'pedidos': [pedido]}),
        })

    def _pedido(self, tienda, productos, enviados):
        """Pedido de la tienda con `productos`, del que a WIS sólo le llegaron `enviados`."""
        picking = self._preparo(tienda, [(p, 3) for p in productos])
        self._enviado(picking, enviados)
        return picking

    def _moves(self, picking, producto):
        return picking.move_ids.filtered(lambda m: m.product_id == producto)

    # ------------------------------------------------------------------
    # Detección
    # ------------------------------------------------------------------
    def test_detecta_solo_lo_que_no_viajo(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        no_enviadas, sin_revisar = pedido._wis_lineas_no_enviadas()
        self.assertEqual(no_enviadas, self._moves(pedido, self.producto_b))
        self.assertFalse(sin_revisar)

    def test_pedido_completo_no_tiene_nada_para_recuperar(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b],
                              [self.producto_a, self.producto_b])
        no_enviadas, _sin_revisar = pedido._wis_lineas_no_enviadas()
        self.assertFalse(no_enviadas)

    def test_saldo_se_compara_con_el_envio_del_pedido(self):
        """El saldo conserva el código del pedido: se compara con lo que se mandó con ese
        código, aunque el saldo nunca se haya enviado como operación propia."""
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        saldo = self._preparo(self.tiendas[0], [(self.producto_b, 3)])
        saldo.with_context(skip_wms_integration=True).write({
            'codigo_unico': pedido.codigo_unico, 'backorder_id': pedido.id})
        no_enviadas, _sin_revisar = saldo._wis_lineas_no_enviadas()
        self.assertEqual(no_enviadas, saldo.move_ids)

    def test_sin_envio_registrado_no_se_toca(self):
        """Anterior al registro de envíos (05-11): no se puede saber qué recibió WIS."""
        pedido = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        no_enviadas, sin_revisar = pedido._wis_lineas_no_enviadas()
        self.assertFalse(no_enviadas)
        self.assertEqual(sin_revisar, pedido)

    def test_pedido_con_cajas_no_se_toca(self):
        """Con cajas, el contenido no viaja en `detalles`: no se puede saber qué recibió WIS."""
        pedido = self._preparo(self.tiendas[0], [(self.producto_a, 3), (self.producto_b, 3)])
        self._enviado(pedido, [self.producto_a], cajas=True)
        no_enviadas, sin_revisar = pedido._wis_lineas_no_enviadas()
        self.assertFalse(no_enviadas)
        self.assertEqual(sin_revisar, pedido)

    # ------------------------------------------------------------------
    # Recuperación
    # ------------------------------------------------------------------
    def test_las_lineas_pasan_a_un_pedido_nuevo_que_sale_por_la_cola(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        entrega = self._entrega(self.tiendas[0], pedido)
        linea = self._moves(pedido, self.producto_b)
        destino = linea.move_dest_ids

        nueva = self.Picking._wis_recuperar_lineas(pedido._wis_lineas_no_enviadas()[0])

        self.assertEqual(len(nueva), 1)
        self.assertEqual(linea.picking_id, nueva, "la línea se mueve, no se copia")
        self.assertEqual(pedido.move_ids.product_id, self.producto_a,
                         "el pedido original queda con lo que WIS tiene")
        self.assertEqual(nueva.codigo_unico, 'W-P-%s' % nueva.id, "código W-P propio")
        self.assertNotEqual(nueva.codigo_unico, pedido.codigo_unico)
        self.assertEqual(nueva.wms_estado, 'en_cola')
        self.assertEqual(nueva.picking_type_id, pedido.picking_type_id)
        self.assertEqual(nueva.location_dest_id, pedido.location_dest_id)
        self.assertEqual(linea.move_dest_ids, destino, "la entrega a la tienda sigue encadenada")
        self.assertEqual(entrega.move_ids.move_orig_ids, pedido.move_ids | linea)
        self.assertFalse(linea.move_line_ids, "sale sin reserva")
        entrada = self.Cola.search([('picking_id', '=', nueva.id)])
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertFalse(entrada.retenida, "sale en la próxima corrida de la cola")
        self.assertIn(nueva.name, pedido.message_ids[:1].body)

    def test_un_pedido_nuevo_por_sucursal(self):
        a1 = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        a2 = self._pedido(self.tiendas[0], [self.producto_a, self.producto_c], [self.producto_a])
        b1 = self._pedido(self.tiendas[1], [self.producto_a, self.producto_b], [self.producto_a])
        no_enviadas, _sin_revisar = (a1 | a2 | b1)._wis_lineas_no_enviadas()
        self.assertEqual(len(no_enviadas), 3)

        nuevas = self.Picking._wis_recuperar_lineas(no_enviadas)

        self.assertEqual(len(nuevas), 2)
        de_a = nuevas.filtered(lambda p: p.location_dest_id == self.tiendas[0]['salida'])
        self.assertEqual(de_a.move_ids.product_id, self.producto_b | self.producto_c,
                         "las dos operaciones de la sucursal van en un solo pedido")
        self.assertEqual(len(set(nuevas.mapped('codigo_unico'))), 2)

    def test_tope_de_lineas_por_pedido(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b, self.producto_c],
                              [])
        nuevas = self.Picking._wis_recuperar_lineas(pedido.move_ids, max_lineas=2)
        self.assertEqual(sorted(len(n.move_ids) for n in nuevas), [1, 2])

    def test_operacion_que_queda_vacia_no_se_borra(self):
        saldo = self._pedido(self.tiendas[0], [self.producto_b], [self.producto_a])
        self.Picking._wis_recuperar_lineas(saldo._wis_lineas_no_enviadas()[0])
        self.assertTrue(saldo.exists())
        self.assertFalse(saldo.move_ids)

    def test_al_despachar_el_pedido_nuevo_la_mercaderia_llega_a_la_tienda(self):
        tienda = self.tiendas[0]
        pedido = self._pedido(tienda, [self.producto_a, self.producto_b], [self.producto_a])
        entrega = self._entrega(tienda, pedido)
        nueva = self.Picking._wis_recuperar_lineas(pedido._wis_lineas_no_enviadas()[0])
        # La cola la manda (acá con la comunicación apagada, se da por enviada).
        nueva.with_context(skip_wms_integration=True).write({'wms_estado': 'enviado'})

        pickings = [pedido, nueva]
        contenedores = [
            self._contenedor('WISCO-R1', 32790001, pedido.codigo_unico, [(self.producto_a, 3)]),
            self._contenedor('WISCO-R2', 32790002, nueva.codigo_unico, [(self.producto_b, 3)]),
        ]
        self._preparada(pickings, contenedores)
        self.controller._handle_confirmacion_pedido({
            'fechaCierre': '24/11/2026',
            'pedidos': [{'pedido': p.codigo_unico, 'codigoOrigen': 'API'} for p in pickings],
            'contenedores': contenedores,
        })

        self.assertEqual(pedido.state, 'done')
        self.assertEqual(nueva.state, 'done')
        self.assertEqual(entrega.state, 'done', "la entrega a la tienda se completó")
        Quant = self.env['stock.quant']
        for producto in (self.producto_a, self.producto_b):
            self.assertEqual(Quant._get_available_quantity(producto, tienda['transito']), 3,
                             producto.name)

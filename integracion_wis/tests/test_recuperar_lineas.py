# -*- coding: utf-8 -*-
"""Recuperar las líneas que WIS nunca recibió (producción, 05 al 24-11-2026: 28.177 líneas).

La reposición en tandas dejó líneas en pedidos que WIS ya tenía, y cuando WIS preparaba el
pedido esas líneas pasaban a un saldo con el mismo código. Se mueven a un pedido nuevo por
sucursal, sin duplicar y sin cortar la entrega a la tienda, que queda SIN ENVIAR a WIS hasta
que Forum decida: mandarlo («Integrar con WMS»), cancelarlo (con la cadena) o borrarlo.
"""
import json
from unittest.mock import patch

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
    # Grupos del relevamiento (0.14.8)
    # ------------------------------------------------------------------
    def _saldo_de(self, pedido, cantidades):
        saldo = self._preparo(self.tiendas[0], cantidades)
        saldo.with_context(skip_wms_integration=True).write({
            'codigo_unico': pedido.codigo_unico, 'backorder_id': pedido.id})
        return saldo

    def test_clasifica_los_tres_grupos(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        saldo = self._saldo_de(pedido, [(self.producto_a, 3), (self.producto_c, 3)])
        viejo = self._preparo(self.tiendas[1], [(self.producto_a, 3)])  # sin envío registrado
        grupos, sin_revisar = (pedido | saldo | viejo)._wis_clasificar_lineas()
        self.assertEqual(grupos['no_enviadas'],
                         self._moves(pedido, self.producto_b) | self._moves(saldo, self.producto_c))
        self.assertEqual(grupos['no_preparadas'], self._moves(saldo, self.producto_a),
                         "viajó con el código y quedó en el saldo: WIS no la preparó")
        self.assertEqual(grupos['sin_registro'], viejo.move_ids)
        self.assertFalse(sin_revisar)

    def test_pedido_abierto_enviado_no_es_de_ningun_grupo(self):
        """Lo que viajó y sigue en el pedido abierto es lo que WIS tiene (grupo 1)."""
        pedido = self._pedido(self.tiendas[0], [self.producto_a], [self.producto_a])
        grupos, _sin_revisar = pedido._wis_clasificar_lineas()
        self.assertFalse(grupos['no_enviadas'] | grupos['no_preparadas'] | grupos['sin_registro'])

    def test_preparado_o_despachado_no_se_toca(self):
        """Ésas emiten e-Remito al validarse: van con la regularización, no acá."""
        viejo = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        for estado in ('preparado', 'despachado'):
            viejo.with_context(skip_wms_integration=True).write({'wms_estado': estado})
            grupos, _sin_revisar = viejo._wis_clasificar_lineas()
            self.assertFalse(grupos['sin_registro'], estado)

    def test_cada_grupo_arma_sus_pedidos(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        saldo = self._saldo_de(pedido, [(self.producto_a, 3)])
        (pedido | saldo).with_context(skip_wms_integration=True).write({'origin': 'REP/00099'})
        grupos, _sin_revisar = (pedido | saldo)._wis_clasificar_lineas()
        nueva_3 = self.Picking._wis_recuperar_lineas(grupos['no_enviadas'])
        nueva_2 = self.Picking._wis_recuperar_lineas(grupos['no_preparadas'], grupo='no_preparadas')
        self.assertNotEqual(nueva_2, nueva_3)
        self.assertEqual(nueva_3.origin, 'REP/00099 (recuperación WIS)')
        self.assertEqual(nueva_2.origin, 'REP/00099 (WIS no preparó)')
        self.assertEqual(nueva_2.wms_estado, 'sin_enviar')
        self.assertTrue(nueva_2.wis_envio_retenido)
        self.assertIn('WIS recibió y no preparó', saldo.message_ids[:1].body)

    # ------------------------------------------------------------------
    # Recuperación
    # ------------------------------------------------------------------
    def _con_comunicacion(self):
        """Comunicación con WIS prendida y el tipo disparando en «en espera», como el PICK de
        producción: sin el freno, el pedido nuevo saldría solo."""
        config = self.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', self.env.company.id)], limit=1)
        if not config:
            config = self.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test', 'client_secret': 'test', 'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api', 'company_id': self.env.company.id,
            })
        config.write({'comunicacion_activa': True, 'pausa_hasta': False, 'pausa_fallos': 0})
        self.tipo.estado_disparo_wis = 'waiting'

    def _enviar_wis(self):
        return patch.object(type(self.env['stock.picking']), 'enviarWS', autospec=True,
                            return_value={'numeroInterfaz': 1})

    @staticmethod
    def _como_produccion(*pickings):
        """En producción ninguna línea de la cadena propaga la cancelación (28.177 de 28.177):
        sin esto, Odoo cancelaría la cadena solo y el test no probaría nada."""
        for picking in pickings:
            picking.move_ids.write({'propagate_cancel': False})

    def _recuperado(self, tienda=None):
        tienda = tienda or self.tiendas[0]
        pedido = self._pedido(tienda, [self.producto_a, self.producto_b], [self.producto_a])
        entrega = self._entrega(tienda, pedido)
        self._como_produccion(pedido, entrega)
        linea = self._moves(pedido, self.producto_b)
        nueva = self.Picking._wis_recuperar_lineas(pedido._wis_lineas_no_enviadas()[0])
        return pedido, entrega, linea, nueva

    def test_las_lineas_pasan_a_un_pedido_nuevo_sin_enviar(self):
        pedido = self._pedido(self.tiendas[0], [self.producto_a, self.producto_b], [self.producto_a])
        entrega = self._entrega(self.tiendas[0], pedido)
        linea = self._moves(pedido, self.producto_b)
        destino = linea.move_dest_ids

        nueva = self.Picking._wis_recuperar_lineas(pedido._wis_lineas_no_enviadas()[0])

        self.assertEqual(len(nueva), 1)
        self.assertEqual(linea.picking_id, nueva, "la línea se mueve, no se copia")
        self.assertEqual(pedido.move_ids.product_id, self.producto_a,
                         "el pedido original queda con lo que WIS tiene")
        self.assertEqual(nueva.wms_estado, 'sin_enviar')
        self.assertFalse(nueva.codigo_unico, "el código lo toma recién cuando se manda")
        self.assertTrue(nueva.wis_envio_retenido)
        self.assertTrue(nueva.wis_integracion_manual, "muestra «Integrar con WMS»")
        self.assertFalse(self.Cola.search([('picking_id', '=', nueva.id)]), "no entra a la cola")
        self.assertEqual(nueva.picking_type_id, pedido.picking_type_id)
        self.assertEqual(nueva.location_dest_id, pedido.location_dest_id)
        self.assertEqual(linea.move_dest_ids, destino, "la entrega a la tienda sigue encadenada")
        self.assertEqual(entrega.move_ids.move_orig_ids, pedido.move_ids | linea)
        self.assertFalse(linea.move_line_ids, "sale sin reserva")
        self.assertIn(nueva.name, pedido.message_ids[:1].body)

    def test_no_sale_sola_a_wis(self):
        """Ningún disparo automático la manda: ni reservar, ni confirmar, ni la cola."""
        self._con_comunicacion()
        _pedido, _entrega, _linea, nueva = self._recuperado()
        with self._enviar_wis() as enviar:
            nueva.action_assign()
            nueva._enviar_wis_si_corresponde('write')
            nueva.with_context(wis_encolar_envios=True)._enviar_wis_si_corresponde('write')
            nueva.move_ids._action_confirm()
            self.Cola._cron_procesar()
        enviar.assert_not_called()
        self.assertEqual(nueva.wms_estado, 'sin_enviar')
        self.assertFalse(nueva.codigo_unico)
        self.assertFalse(self.Cola.search([('picking_id', '=', nueva.id)]))

    def test_no_suma_lineas_de_otra_reposicion(self):
        tienda = self.tiendas[0]
        _pedido, _entrega, _linea, nueva = self._recuperado(tienda)
        move = self.env['stock.move'].with_context(skip_wms_integration=True).create({
            'name': self.producto_c.name, 'product_id': self.producto_c.id,
            'product_uom_qty': 1, 'product_uom': self.producto_c.uom_id.id,
            'location_id': self.origen.id, 'location_dest_id': tienda['salida'].id,
            'group_id': tienda['grupo'].id, 'picking_type_id': self.tipo.id,
        })
        move._action_confirm()
        self.assertTrue(move.picking_id)
        self.assertNotEqual(move.picking_id, nueva, "espera la decisión de Forum: no suma líneas")

    def test_integrar_con_wms_la_manda_como_pedido_nuevo(self):
        self._con_comunicacion()
        pedido, _entrega, _linea, nueva = self._recuperado()
        nueva.action_integrar_wms_manual()
        self.assertEqual(nueva.wms_estado, 'en_cola')
        self.assertEqual(nueva.codigo_unico, 'W-P-%s' % nueva.id, "código W-P propio")
        self.assertNotEqual(nueva.codigo_unico, pedido.codigo_unico)
        entrada = self.Cola.search([('picking_id', '=', nueva.id)])
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertFalse(entrada.retenida)
        with self._enviar_wis() as enviar:
            self.Cola._cron_procesar()
        enviar.assert_called_once()
        self.assertEqual(entrada.estado, 'enviado')

    def test_cancelar_cancela_la_entrega_a_la_tienda(self):
        pedido, entrega, linea, nueva = self._recuperado()
        siguiente = linea.move_dest_ids
        nueva.action_cancel()
        self.assertEqual(nueva.state, 'cancel')
        self.assertEqual(siguiente.state, 'cancel', "la entrega de esa línea no queda esperando")
        self.assertEqual((entrega.move_ids - siguiente).mapped('state'), ['waiting'],
                         "la del resto del pedido sigue")
        self.assertNotEqual(pedido.state, 'cancel')

    def test_cancelar_recorre_toda_la_cadena(self):
        """En producción la cadena es PICK → Intercambio - Envíos → recepción de la tienda."""
        tienda = self.tiendas[0]
        pedido, entrega, linea, nueva = self._recuperado()
        siguiente = linea.move_dest_ids
        Picking = self.env['stock.picking'].with_context(skip_wms_integration=True)
        recepcion = Picking.create({
            'picking_type_id': self.tipo_interno.id,
            'location_id': tienda['transito'].id, 'location_dest_id': tienda['salida'].id,
            'move_ids': [(0, 0, {
                'name': siguiente.product_id.name, 'product_id': siguiente.product_id.id,
                'product_uom_qty': siguiente.product_uom_qty, 'product_uom': siguiente.product_uom.id,
                'location_id': tienda['transito'].id, 'location_dest_id': tienda['salida'].id,
                'move_orig_ids': [(6, 0, siguiente.ids)], 'procure_method': 'make_to_order',
            })],
        })
        recepcion.action_confirm()
        self._como_produccion(recepcion)
        nueva.action_cancel()
        self.assertEqual(recepcion.move_ids.state, 'cancel')

    def test_cancelar_un_pedido_comun_no_toca_la_cadena(self):
        """Sólo las operaciones recuperadas arrastran la cadena al cancelarse."""
        pedido = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        entrega = self._entrega(self.tiendas[0], pedido)
        self._como_produccion(pedido, entrega)
        pedido.move_ids.with_context(skip_wms_integration=True)._action_cancel()
        self.assertNotEqual(entrega.move_ids.state, 'cancel')

    def test_cancelada_se_puede_borrar(self):
        _pedido, _entrega, _linea, nueva = self._recuperado()
        nueva.action_cancel()
        nueva.unlink()
        self.assertFalse(nueva.exists())

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
        self.assertEqual(len(nuevas.location_dest_id), 2)

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
        # Forum la manda y la cola la envía (acá con la comunicación apagada, se da por enviada).
        nueva.with_context(skip_wms_integration=True).write({
            'codigo_unico': 'W-P-%s' % nueva.id, 'wms_estado': 'enviado'})

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

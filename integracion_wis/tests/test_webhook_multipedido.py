# -*- coding: utf-8 -*-
"""Webhooks de WIS que traen VARIOS pedidos en un mismo envío.

Caso real (support, 12-10-2026, webhook 132015): WIS confirmó la preparación de 5 pedidos de
crossdock juntos, cada uno con SU contenedor. Odoo no separaba los contenedores por pedido:

- cada picking sumaba las cantidades de los 5 pedidos y tomaba como paquete el primer
  contenedor (WISCO50005, que era de otro pedido), así que los 5 quedaban en el mismo paquete;
- el core rechazaba el segundo picking («You cannot move the same package content more than
  once…») DESPUÉS de mover los quants, y el handler se tragaba el error: stock movido, picking
  en «Listo», backorder huérfano con el mismo código y el e-Remito ya emitido;
- en la expedición, la «Entrega a Tiendas» chocaba con el mismo paquete y la tienda nunca
  veía la recepción pendiente.

Los handlers se llaman directo (sin HTTP): `request` se reemplaza por el entorno del test.
"""
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.integracion_wis.controllers import controllers as wis_controllers


@tagged('post_install', '-at_install', 'wis_webhook_multipedido')
class TestWebhookMultipedido(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        if config:
            # Nada de lo que valida un webhook tiene que salir hacia WIS.
            config.comunicacion_activa = False

        cls.almacen = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        vista = cls.almacen.view_location_id
        cls.origen = cls.env['stock.location'].create({
            'name': 'Entrada multipedido', 'usage': 'internal', 'location_id': vista.id})
        cls.tipo = cls.env['stock.picking.type'].create({
            'name': 'Preparo crossdock (test)',
            'code': 'internal',
            'sequence_code': 'TXDM',
            'warehouse_id': cls.almacen.id,
            'default_location_src_id': cls.origen.id,
            'default_location_dest_id': cls.origen.id,
            'integracion_wms': True,
            'metodo_preparacion_wis': True,
            'tipo_pedido_wis': 'XD',
            'reservation_method': 'manual',
        })
        cls.tipo_interno = cls.env['stock.picking.type'].create({
            'name': 'Entrega a tiendas (test)',
            'code': 'internal',
            'sequence_code': 'TOXD',
            'warehouse_id': cls.almacen.id,
            'default_location_src_id': cls.origen.id,
            'default_location_dest_id': cls.origen.id,
            'reservation_method': 'manual',
        })
        cls.producto_a = cls._producto('Remera multipedido', 'PRD-TMP-A')
        cls.producto_b = cls._producto('Pantalón multipedido', 'PRD-TMP-B')

        cls.tiendas = []
        for letra in 'ABCDE':
            cls.tiendas.append({
                'salida': cls.env['stock.location'].create({
                    'name': f'Salida {letra}', 'usage': 'internal',
                    'location_id': vista.id}),
                'transito': cls.env['stock.location'].create({
                    'name': f'Tránsito {letra}', 'usage': 'internal',
                    'location_id': vista.id}),
                'grupo': cls.env['procurement.group'].create({'name': f'XD test {letra}'}),
            })

    @classmethod
    def _producto(cls, nombre, codigo):
        producto = cls.env['product.product'].create({'name': nombre, 'type': 'product'})
        producto.with_context(_avoid_wms=True).codigo_unico = codigo
        return producto

    # ------------------------------------------------------------------
    # Armado
    # ------------------------------------------------------------------
    def setUp(self):
        super().setUp()
        Quant = self.env['stock.quant']
        for producto in (self.producto_a, self.producto_b):
            Quant._update_available_quantity(producto, self.origen, 100)
        self.controller = wis_controllers.WebhookWIS()
        parche = patch.object(wis_controllers, 'request', SimpleNamespace(env=self.env))
        parche.start()
        self.addCleanup(parche.stop)

    def _picking(self, tipo, origen, destino, grupo, cantidades):
        sin_wis = self.env['stock.picking'].with_context(skip_wms_integration=True)
        picking = sin_wis.create({
            'picking_type_id': tipo.id,
            'location_id': origen.id,
            'location_dest_id': destino.id,
            'group_id': grupo.id,
            'move_ids': [(0, 0, {
                'name': producto.name, 'product_id': producto.id,
                'product_uom_qty': cantidad, 'product_uom': producto.uom_id.id,
                'location_id': origen.id, 'location_dest_id': destino.id,
                'group_id': grupo.id,
            }) for producto, cantidad in cantidades],
        })
        picking.action_confirm()
        return self.env['stock.picking'].browse(picking.id)

    def _preparo(self, tienda, cantidades):
        picking = self._picking(self.tipo, self.origen, tienda['salida'], tienda['grupo'],
                                cantidades)
        picking.with_context(skip_wms_integration=True).action_assign()
        picking.with_context(skip_wms_integration=True).write({
            'codigo_unico': f'W-P-{picking.id}', 'wms_estado': 'enviado'})
        return picking

    def _entrega(self, tienda, preparo):
        """Operación interna Salida -> Tránsito, sin integración, encadenada a la preparación
        por movimientos (como la arma Odoo): es la que avanza cuando WIS despacha."""
        sin_wis = self.env['stock.picking'].with_context(skip_wms_integration=True)
        interna = sin_wis.create({
            'picking_type_id': self.tipo_interno.id,
            'location_id': tienda['salida'].id,
            'location_dest_id': tienda['transito'].id,
            'group_id': tienda['grupo'].id,
            'move_ids': [(0, 0, {
                'name': m.product_id.name, 'product_id': m.product_id.id,
                'product_uom_qty': m.product_uom_qty, 'product_uom': m.product_uom.id,
                'location_id': tienda['salida'].id, 'location_dest_id': tienda['transito'].id,
                'group_id': tienda['grupo'].id, 'move_orig_ids': [(6, 0, m.ids)],
                'procure_method': 'make_to_order',
            }) for m in preparo.move_ids],
        })
        interna.action_confirm()
        interna.with_context(skip_wms_integration=True).write({
            'codigo_unico': preparo.codigo_unico, 'wms_estado': 'no_integrado'})
        return self.env['stock.picking'].browse(interna.id)

    @staticmethod
    def _contenedor(barras, numero, pedido, cantidades, bultos=1):
        return {
            'numeroContenedor': numero,
            'idExternoContenedor': str(numero),
            'codigoBarras': barras,
            'cantidadBultos': bultos,
            'pesoReal': 2.5,
            'detalles': [{
                'pedido': pedido, 'producto': producto.codigo_unico,
                'cantidadPreparada': cantidad,
            } for producto, cantidad in cantidades],
        }

    def _cinco_pedidos(self):
        """Los 5 pedidos del webhook 132015: mismas variantes, cantidades distintas."""
        pickings, contenedores = [], []
        for n, tienda in enumerate(self.tiendas):
            cantidad = 3 if n == 0 else 7
            picking = self._preparo(tienda, [(self.producto_a, cantidad),
                                             (self.producto_b, cantidad)])
            pickings.append(picking)
            contenedores.append(self._contenedor(
                f'WISCO-T{n}', 32780587 + n, picking.codigo_unico,
                [(self.producto_a, cantidad), (self.producto_b, cantidad)]))
        return pickings, contenedores

    def _preparada(self, pickings, contenedores):
        self.controller._handle_mercaderia_preparada({
            'fechaFacturacion': '12/10/2026',
            'pedidos': [{'pedido': p.codigo_unico} for p in pickings],
            'contenedores': contenedores,
        })

    def _ubicaciones_con_stock(self, paquete):
        return paquete.quant_ids.filtered(lambda q: q.quantity > 0).location_id

    # ------------------------------------------------------------------
    # Contenedores por pedido
    # ------------------------------------------------------------------
    def test_cada_pedido_queda_con_su_contenedor(self):
        pickings, contenedores = self._cinco_pedidos()
        self._preparada(pickings, contenedores)

        for n, (picking, tienda) in enumerate(zip(pickings, self.tiendas)):
            self.assertEqual(picking.state, 'done', picking.name)
            self.assertEqual(picking.wms_estado, 'preparado')
            paquetes = picking.move_line_ids.result_package_id
            self.assertEqual(paquetes.mapped('name'), [f'WISCO-T{n}'],
                             "cada pedido viaja en SU contenedor, no en el del primero")
            self.assertEqual(self._ubicaciones_con_stock(paquetes), tienda['salida'])
            esperado = 3 if n == 0 else 7
            self.assertEqual(sorted(picking.move_line_ids.mapped('quantity')),
                             [esperado, esperado],
                             "lo preparado es lo del pedido, no la suma de los 5")

    def test_filtro_de_contenedores_por_pedido(self):
        contenedores = [
            {'codigoBarras': 'C1', 'detalles': [{'pedido': 'W-P-1', 'producto': 'X'}]},
            {'codigoBarras': 'C2', 'detalles': [{'pedido': 'W-P-2', 'producto': 'X'}]},
            {'codigoBarras': 'C3', 'detalles': [{'pedido': 'W-P-1', 'producto': 'Y'},
                                                {'pedido': 'W-P-2', 'producto': 'Z'}]},
            # Formato de un solo pedido: el detalle no trae el pedido.
            {'codigoBarras': 'C4', 'detalles': [{'producto': 'X'}]},
        ]
        propios = self.controller._wis_contenedores_del_pedido(contenedores, 'W-P-1')
        self.assertEqual([c['codigoBarras'] for c in propios], ['C1', 'C3', 'C4'])
        compartido = next(c for c in propios if c['codigoBarras'] == 'C3')
        self.assertEqual([d['producto'] for d in compartido['detalles']], ['Y'])
        self.assertEqual(len(contenedores[2]['detalles']), 2, "no modifica el payload")

    def test_paquete_con_stock_en_otro_lugar_no_se_reusa(self):
        """WIS reusa el código de barras de un contenedor físico: si el paquete viejo todavía
        tiene stock en otra ubicación, no se lo mezcla con lo nuevo."""
        viejo = self.env['stock.quant.package'].create({'name': 'WISCO-REUSO'})
        self.env['stock.quant']._update_available_quantity(
            self.producto_a, self.tiendas[4]['transito'], 5, package_id=viejo)
        tienda = self.tiendas[0]
        picking = self._preparo(tienda, [(self.producto_a, 3)])
        self._preparada([picking], [self._contenedor(
            'WISCO-REUSO', 999, picking.codigo_unico, [(self.producto_a, 3)])])

        self.assertEqual(picking.state, 'done')
        nuevo = picking.move_line_ids.result_package_id
        self.assertNotEqual(nuevo, viejo)
        self.assertEqual(nuevo.name, 'WISCO-REUSO', "el nombre sigue siendo el de WIS")
        self.assertEqual(self._ubicaciones_con_stock(viejo), self.tiendas[4]['transito'])
        self.assertEqual(self._ubicaciones_con_stock(nuevo), tienda['salida'])

    # ------------------------------------------------------------------
    # Una falla no deja nada a medias
    # ------------------------------------------------------------------
    def test_validacion_fallida_deja_preparado_sin_mover_stock(self):
        """La validación (que emite el e-Remito) no puede ir en el savepoint del webhook: la
        emisión hace COMMIT. Si el «Hecho» del core falla, `_action_done` lo deshace con su
        propio savepoint: no se mueve stock; el pedido queda PREPARADO con lo que informó WIS y
        el log dice validarlo a mano (WIS no reenvía)."""
        tienda = self.tiendas[0]
        picking = self._preparo(tienda, [(self.producto_a, 4), (self.producto_b, 4)])
        Move = type(self.env['stock.move'])
        original = Move._action_done

        def falla_despues_de_mover(moves, *args, **kwargs):
            original(moves, *args, **kwargs)
            raise UserError("falla de prueba después de mover el stock")

        # Producto B en 0 -> se separa a un backorder antes de validar.
        contenedor = self._contenedor('WISCO-FALLA', 1, picking.codigo_unico,
                                      [(self.producto_a, 4)])
        with patch.object(Move, '_action_done', falla_despues_de_mover):
            with self.assertRaisesRegex(ValueError, 'falla de prueba'):
                self._preparada([picking], [contenedor])

        picking.invalidate_recordset()
        self.assertNotEqual(picking.state, 'done')
        self.assertEqual(picking.wms_estado, 'preparado', "queda con lo que informó WIS")
        self.assertEqual(picking.move_line_ids.result_package_id.name, 'WISCO-FALLA')
        self.assertEqual(
            self.env['stock.quant']._get_available_quantity(
                self.producto_a, tienda['salida']), 0,
            "el stock no se movió")
        saldo = self.env['stock.picking'].search([('backorder_id', '=', picking.id)])
        self.assertEqual(saldo.move_ids.product_id, self.producto_b, "la línea en 0 queda en el saldo")
        log = self.env['wms.integracion.log'].search(
            [('picking_id', '=', picking.id), ('nivel', '=', 'error')], limit=1)
        self.assertIn('PREPARADO', log.texto)
        self.assertIn('Validarlo desde Odoo', log.texto)

    def test_validar_a_mano_despues_de_la_falla(self):
        """Resuelto el motivo, el operario valida desde Odoo y la cadena sigue."""
        tienda = self.tiendas[0]
        picking = self._preparo(tienda, [(self.producto_a, 4)])
        Move = type(self.env['stock.move'])
        original = Move._action_done

        def falla(moves, *args, **kwargs):
            original(moves, *args, **kwargs)
            raise UserError("falla de prueba")

        contenedor = self._contenedor('WISCO-MANO', 2, picking.codigo_unico, [(self.producto_a, 4)])
        with patch.object(Move, '_action_done', falla):
            with self.assertRaises(ValueError):
                self._preparada([picking], [contenedor])
        picking.with_context(skip_wms_integration=True).button_validate()
        self.assertEqual(picking.state, 'done')
        self.assertEqual(self.env['stock.quant']._get_available_quantity(
            self.producto_a, tienda['salida']), 4)

    def _no_se_proceso(self, picking, contenedores, motivo):
        with self.assertRaisesRegex(ValueError, motivo):
            self._preparada([picking], contenedores)
        picking.invalidate_recordset()
        self.assertEqual(picking.wms_estado, 'enviado', "el pedido quedó como estaba")
        self.assertNotEqual(picking.state, 'done')
        self.assertFalse(picking.move_line_ids.result_package_id)
        self.assertFalse(self.env['stock.picking'].search([('backorder_id', '=', picking.id)]))
        log = self.env['wms.integracion.log'].search(
            [('picking_id', '=', picking.id), ('nivel', '=', 'error')], limit=1)
        self.assertIn(motivo, log.detalle, "el motivo queda en el log de integración")

    def _aviso(self, picking):
        log = self.env['wms.integracion.log'].search(
            [('picking_id', '=', picking.id), ('nivel', '=', 'warning')], limit=1)
        return log.detalle or ''

    def test_producto_que_el_pedido_no_tiene_se_ignora_con_aviso(self):
        """WIS notifica una sola vez: se procesa lo que calza y el resto queda avisado."""
        picking = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        self._preparada([picking], [self._contenedor(
            'WISCO-AJENO', 4, picking.codigo_unico,
            [(self.producto_a, 3), (self.producto_b, 2)])])
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.move_line_ids.mapped('quantity'), [3])
        self.assertIn('PRD-TMP-B', self._aviso(picking))
        self.assertIn('se ignoró', self._aviso(picking))

    def test_mas_de_lo_pendiente_se_toma_lo_pendiente_con_aviso(self):
        picking = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        self._preparada([picking], [self._contenedor(
            'WISCO-DE-MAS', 5, picking.codigo_unico, [(self.producto_a, 5)])])
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.move_line_ids.mapped('quantity'), [3])
        self.assertIn('tenía pendiente 3', self._aviso(picking))

    def test_notificacion_sin_nada_del_pedido_no_lo_marca_preparado(self):
        """Una notificación que no trae nada para el pedido (p. ej. repetida, cuando lo que
        queda es el saldo) dejaba el saldo «preparado» sin cantidades, y la preparación de
        verdad se rechazaba después con «ya tiene wms_estado 'preparado'»."""
        picking = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        self._no_se_proceso(picking, [self._contenedor(
            'WISCO-OTRO', 6, 'W-P-OTRO-PEDIDO', [(self.producto_a, 3)])], 'no trae cantidades')

    def test_notificacion_de_un_envio_sin_confirmar_lo_da_por_enviado(self):
        """Timeout: WIS lo recibió pero Odoo no lo pudo confirmar (o se rindió y lo dejó «sin
        enviar»). La notificación de WIS lo prueba: se procesa y la cola no lo reenvía."""
        Cola = self.env['wis.picking.cola']
        casos = []
        for wms_estado, estado_cola in (('en_cola', 'pendiente'), ('sin_enviar', 'error')):
            picking = self._preparo(self.tiendas[len(casos)], [(self.producto_a, 3)])
            picking.with_context(skip_wms_integration=True).write({'wms_estado': wms_estado})
            entrada = Cola.create({'picking_id': picking.id, 'tipo': 'XD', 'estado': estado_cola,
                                   'tipo_error': 'sin_confirmar', 'intentos': 1})
            casos.append((picking, entrada))
        self._preparada([p for p, _e in casos], [
            self._contenedor(f'WISCO-SC{n}', 70 + n, p.codigo_unico, [(self.producto_a, 3)])
            for n, (p, _e) in enumerate(casos)])
        for picking, entrada in casos:
            self.assertEqual(picking.state, 'done', picking.name)
            self.assertEqual(picking.wms_estado, 'preparado')
            self.assertEqual(entrada.estado, 'enviado', "la cola no lo vuelve a mandar")
            self.assertFalse(entrada.proximo_intento)

    def test_parcial_deja_un_solo_saldo(self):
        """Producto A preparado a medias y B en 0: antes quedaban DOS backorders con el mismo
        código (uno por las líneas en 0 y otro por el saldo de A) y la próxima preparación
        sólo encontraba el primero."""
        tienda = self.tiendas[0]
        picking = self._preparo(tienda, [(self.producto_a, 10), (self.producto_b, 10)])
        self._preparada([picking], [self._contenedor(
            'WISCO-PARCIAL', 2, picking.codigo_unico, [(self.producto_a, 4)])])

        self.assertEqual(picking.state, 'done')
        saldos = self.env['stock.picking'].search([
            ('backorder_id', '=', picking.id), ('state', 'not in', ('done', 'cancel'))])
        self.assertEqual(len(saldos), 1, "un solo saldo por pedido")
        self.assertEqual(saldos.codigo_unico, picking.codigo_unico)
        self.assertEqual(saldos.wms_estado, 'enviado')
        pendiente = {m.product_id: m.product_uom_qty for m in saldos.move_ids}
        self.assertEqual(pendiente, {self.producto_a: 6, self.producto_b: 10})

    # ------------------------------------------------------------------
    # Expedición
    # ------------------------------------------------------------------
    def test_expedicion_valida_la_entrega_a_tienda_de_cada_pedido(self):
        pickings, contenedores = self._cinco_pedidos()
        entregas = [self._entrega(t, p) for t, p in zip(self.tiendas, pickings)]
        self._preparada(pickings, contenedores)
        paquetes = {p: p.move_line_ids.result_package_id for p in pickings}

        self.controller._handle_confirmacion_pedido({
            'fechaCierre': '12/10/2026',
            'pedidos': [{'pedido': p.codigo_unico, 'codigoOrigen': 'API'} for p in pickings],
            'contenedores': contenedores,
        })

        for picking, entrega, tienda in zip(pickings, entregas, self.tiendas):
            self.assertEqual(picking.wms_estado, 'despachado')
            self.assertEqual(picking.move_line_ids.result_package_id, paquetes[picking],
                             "la expedición no le cambia el paquete a lo ya preparado")
            self.assertEqual(entrega.state, 'done', entrega.name)
            self.assertEqual(self._ubicaciones_con_stock(paquetes[picking]),
                             tienda['transito'], "el paquete viajó a la tienda")
            self.assertEqual(picking.wms_cantidad_bultos, 1,
                             "los bultos son los del pedido, no los de los 5")

    def test_entrega_con_producto_partido_no_mueve_de_mas(self):
        """Caso MELO (support, 13-10-2026): la entrega tenía el mismo producto en dos
        movimientos y cada uno tomó las mismas unidades del paquete (salieron 318 de 159)."""
        tienda = self.tiendas[0]
        picking = self._preparo(tienda, [(self.producto_a, 6)])
        # Dos movimientos del mismo producto que NO se fusionan (en support venían de orígenes
        # distintos); el helper `_picking` confirma con fusión y los juntaría en uno.
        entrega = self.env['stock.picking'].with_context(skip_wms_integration=True).create({
            'picking_type_id': self.tipo_interno.id, 'group_id': tienda['grupo'].id,
            'location_id': tienda['salida'].id, 'location_dest_id': tienda['transito'].id,
            'move_ids': [(0, 0, {
                'name': 'partido', 'product_id': self.producto_a.id, 'product_uom_qty': 3,
                'product_uom': self.producto_a.uom_id.id, 'group_id': tienda['grupo'].id,
                'location_id': tienda['salida'].id, 'location_dest_id': tienda['transito'].id,
                'move_orig_ids': [(6, 0, picking.move_ids.ids)], 'procure_method': 'make_to_order',
            }) for _ in range(2)],
        })
        entrega.move_ids._action_confirm(merge=False)
        entrega = self.env['stock.picking'].browse(entrega.id)
        self.assertEqual(len(entrega.move_ids), 2)
        entrega.with_context(skip_wms_integration=True).write({
            'codigo_unico': picking.codigo_unico, 'wms_estado': 'no_integrado'})
        # WIS prepara 4 de 6: queda saldo, y la entrega sólo puede llevar esas 4.
        contenedor = self._contenedor('WISCO-PARTIDO', 8, picking.codigo_unico,
                                      [(self.producto_a, 4)])
        self._preparada([picking], [contenedor])
        self.controller._handle_confirmacion_pedido({
            'pedidos': [{'pedido': picking.codigo_unico}], 'contenedores': [contenedor]})

        Quant = self.env['stock.quant']
        self.assertEqual(Quant._get_available_quantity(self.producto_a, tienda['transito']), 4,
                         "a la tienda van las 4 preparadas, no el doble")
        negativos = Quant.search([('product_id', '=', self.producto_a.id),
                                  ('location_id', 'in', (tienda['salida'] | tienda['transito']).ids),
                                  ('quantity', '<', 0)])
        self.assertFalse(negativos, "sin stock negativo en la salida")
        self.assertEqual(sum(entrega.move_ids.filtered(lambda m: m.state == 'done').mapped('quantity')), 4)

    def test_entrega_a_tienda_fallida_no_deja_stock_a_medias(self):
        """Si la «Entrega a Tiendas» no se puede validar, se deshace entera y queda el error en
        el log; antes quedaban los quants movidos y la operación en «Listo»."""
        tienda = self.tiendas[0]
        picking = self._preparo(tienda, [(self.producto_a, 3)])
        entrega = self._entrega(tienda, picking)
        contenedor = self._contenedor('WISCO-EXP', 3, picking.codigo_unico,
                                      [(self.producto_a, 3)])
        self._preparada([picking], [contenedor])
        paquete = picking.move_line_ids.result_package_id

        Move = type(self.env['stock.move'])
        original = Move._action_done

        def falla(moves, *args, **kwargs):
            original(moves, *args, **kwargs)
            raise UserError("falla de prueba en la entrega")

        with patch.object(Move, '_action_done', falla), \
                self.assertRaisesRegex(ValueError, 'falla de prueba'):
            # El despacho queda registrado y la falla de la entrega vuelve en la respuesta.
            self.controller._handle_confirmacion_pedido({
                'pedidos': [{'pedido': picking.codigo_unico}],
                'contenedores': [contenedor],
            })
        entrega.invalidate_recordset()
        self.assertEqual(picking.wms_estado, 'despachado', "WIS sí despachó")
        self.assertNotEqual(entrega.state, 'done')
        self.assertEqual(self._ubicaciones_con_stock(paquete), tienda['salida'],
                         "el paquete sigue en la salida: no se movió a medias")
        self.assertTrue(self.env['wms.integracion.log'].search([
            ('picking_id', '=', entrega.id), ('nivel', '=', 'error')]))

    # ------------------------------------------------------------------
    # e-Remito
    # ------------------------------------------------------------------
    def test_paquete_con_stock_ajeno_frena_antes_del_remito(self):
        """El control del core corre después de emitir el CFE: el propio corre antes."""
        ajeno = self.env['stock.quant.package'].create({'name': 'WISCO-AJENO'})
        self.env['stock.quant']._update_available_quantity(
            self.producto_a, self.tiendas[3]['salida'], 2, package_id=ajeno)
        picking = self._preparo(self.tiendas[0], [(self.producto_a, 3)])
        picking.move_line_ids.write({'quantity': 3, 'result_package_id': ajeno.id})
        with self.assertRaisesRegex(UserError, 'WISCO-AJENO'):
            picking._wis_controlar_paquetes()

        libre = self.env['stock.quant.package'].create({'name': 'WISCO-LIBRE'})
        picking.move_line_ids.write({'result_package_id': libre.id})
        picking._wis_controlar_paquetes()
    def test_adenda_del_remito_lleva_el_paquete(self):
        pickings, contenedores = self._cinco_pedidos()
        self._preparada(pickings[:1], contenedores[:1])
        adenda = pickings[0]._get_remito_adenda()
        self.assertIn('Paquetes: WISCO-T0', adenda)
        self.assertNotIn('WISCO-T1', adenda)

    # ------------------------------------------------------------------
    # 0.10.0: casos de producción del 08 al 22-10-2026
    # ------------------------------------------------------------------
    def test_reenvio_de_la_preparacion_no_se_aplica_dos_veces(self):
        """Producción 21/22-10: WIS reenvió la preparación (corte por tiempo) y Odoo la aplicó
        otra vez sobre el saldo: 88 e-Remitos duplicados."""
        picking = self._preparo(self.tiendas[0], [(self.producto_a, 2), (self.producto_b, 2)])
        caja = self._contenedor('WISCO-REENVIO', 77, picking.codigo_unico,
                                [(self.producto_a, 2), (self.producto_b, 1)])
        self._preparada([picking], [caja])
        self._preparada([picking], [caja])
        todos = self.env['stock.picking'].search([('codigo_unico', '=', picking.codigo_unico)])
        self.assertEqual(len(todos.filtered(lambda p: p.state == 'done')), 1)
        saldo = todos.filtered(lambda p: p.state not in ('done', 'cancel'))
        self.assertEqual(sum(saldo.move_ids.mapped('product_uom_qty')), 1, "el saldo sigue pendiente")

    def test_despacho_sin_preparacion_previa_valida_con_las_cajas(self):
        """W-P-26167 y los mayoristas: WIS despachó sin que Odoo tuviera la preparación."""
        tienda = self.tiendas[1]
        picking = self._preparo(tienda, [(self.producto_a, 3)])
        entrega = self._entrega(tienda, picking)
        caja = self._contenedor('WISCO-DIRECTO', 78, picking.codigo_unico, [(self.producto_a, 3)])
        self.controller._handle_confirmacion_pedido({
            'pedidos': [{'pedido': picking.codigo_unico}], 'contenedores': [caja]})
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.wms_estado, 'despachado')
        self.assertEqual(entrega.state, 'done')

    def test_despacho_marca_solo_las_cajas_del_camion(self):
        picking = self._preparo(self.tiendas[2], [(self.producto_a, 4), (self.producto_b, 4)])
        c1 = self._contenedor('WISCO-T1A', 81, picking.codigo_unico, [(self.producto_a, 4)])
        c2 = self._contenedor('WISCO-T1B', 82, picking.codigo_unico, [(self.producto_b, 4)])
        self._preparada([picking], [c1])
        self._preparada([picking], [c2])
        hechos = self.env['stock.picking'].search([('codigo_unico', '=', picking.codigo_unico),
                                                   ('state', '=', 'done')])
        self.assertEqual(len(hechos), 2)
        self.controller._handle_confirmacion_pedido({
            'pedidos': [{'pedido': picking.codigo_unico}], 'contenedores': [c1]})
        tanda1 = hechos.filtered(lambda p: self.producto_a in p.move_ids.product_id)
        tanda2 = hechos - tanda1
        self.assertEqual(tanda1.wms_estado, 'despachado')
        self.assertEqual(tanda2.wms_estado, 'preparado', "la segunda tanda viaja en otro camión")
        self.assertTrue(self.env['wms.integracion.log'].search([
            ('picking_id', '=', tanda1.id), ('texto', 'ilike', 'confirmacionPedido')]))

    def test_entrega_unificada_avanza_solo_lo_despachado(self):
        """Odoo junta en una entrega los pedidos de una tienda: sólo avanza lo despachado y el
        resto queda pendiente, aunque ya esté preparado."""
        tienda = self.tiendas[3]
        p1 = self._preparo(tienda, [(self.producto_a, 2)])
        p2 = self._preparo(tienda, [(self.producto_b, 2)])
        sin_wis = self.env['stock.picking'].with_context(skip_wms_integration=True)
        entrega = sin_wis.create({
            'picking_type_id': self.tipo_interno.id, 'group_id': tienda['grupo'].id,
            'location_id': tienda['salida'].id, 'location_dest_id': tienda['transito'].id,
            'move_ids': [(0, 0, {
                'name': m.product_id.name, 'product_id': m.product_id.id,
                'product_uom_qty': m.product_uom_qty, 'product_uom': m.product_uom.id,
                'location_id': tienda['salida'].id, 'location_dest_id': tienda['transito'].id,
                'group_id': tienda['grupo'].id, 'move_orig_ids': [(6, 0, m.ids)],
                'procure_method': 'make_to_order'}) for m in (p1 | p2).move_ids],
        })
        entrega.action_confirm()
        c1 = self._contenedor('WISCO-U1', 91, p1.codigo_unico, [(self.producto_a, 2)])
        c2 = self._contenedor('WISCO-U2', 92, p2.codigo_unico, [(self.producto_b, 2)])
        self._preparada([p1, p2], [c1, c2])
        self.controller._handle_confirmacion_pedido({
            'pedidos': [{'pedido': p1.codigo_unico}], 'contenedores': [c1]})
        m1 = p1.move_ids.move_dest_ids
        m2 = p2.move_ids.move_dest_ids
        self.assertTrue(all(m.state == 'done' for m in m1))
        self.assertTrue(all(m.state not in ('done', 'cancel') for m in m2),
                        "lo preparado del pedido 2 no salió: no se entrega")

    def test_caja_compartida_entre_entregas(self):
        """WIS arma una caja por tienda con varios pedidos (producción: 69 OUTCROSS)."""
        tienda = self.tiendas[4]
        p1 = self._preparo(tienda, [(self.producto_a, 2)])
        p2 = self._preparo(tienda, [(self.producto_b, 3)])
        e1 = self._entrega(tienda, p1)
        e2 = self._entrega(tienda, p2)
        caja = {
            'numeroContenedor': 95, 'idExternoContenedor': '95', 'codigoBarras': 'WISCO-COMPARTIDA',
            'cantidadBultos': 1,
            'detalles': [{'pedido': p1.codigo_unico, 'producto': self.producto_a.codigo_unico, 'cantidadPreparada': 2},
                         {'pedido': p2.codigo_unico, 'producto': self.producto_b.codigo_unico, 'cantidadPreparada': 3}],
        }
        self._preparada([p1, p2], [caja])
        self.controller._handle_confirmacion_pedido({
            'pedidos': [{'pedido': p1.codigo_unico}, {'pedido': p2.codigo_unico}], 'contenedores': [caja]})
        self.assertEqual(e1.state, 'done')
        self.assertEqual(e2.state, 'done')
        Quant = self.env['stock.quant']
        self.assertEqual(Quant._get_available_quantity(self.producto_a, tienda['transito']), 2)
        self.assertEqual(Quant._get_available_quantity(self.producto_b, tienda['transito']), 3)

    def test_otro_asistente_al_validar_no_se_da_por_bueno(self):
        """Los mayoristas: «Validar» devolvía el asistente de SMS, no validaba y el log decía éxito."""
        picking = self._preparo(self.tiendas[0], [(self.producto_a, 2)])
        caja = self._contenedor('WISCO-SMS', 96, picking.codigo_unico, [(self.producto_a, 2)])
        Picking = type(self.env['stock.picking'])
        # Sin assertRaises: en Odoo deshace el savepoint y se lleva los logs que hay que mirar.
        with patch.object(Picking, 'button_validate',
                          lambda self: {'res_model': 'confirm.stock.sms', 'name': 'SMS', 'type': 'ir.actions.act_window'}):
            try:
                self._preparada([picking], [caja])
                self.fail("la preparación tenía que informar el error")
            except ValueError:
                pass
        self.assertNotEqual(picking.state, 'done')
        logs = self.env['wms.integracion.log'].search([('picking_id', '=', picking.id)])
        self.assertFalse(logs.filtered(lambda l: l.nivel == 'info' and l.resultado == 'exito'))
        self.assertTrue(logs.filtered(lambda l: l.resultado == 'error' and 'SMS' in (l.detalle or '')),
                        [(l.nivel, l.resultado, l.texto, l.detalle) for l in logs])

    def test_reproceso_valida_lo_ya_aplicado_que_no_se_valido(self):
        """Uruware caído o receptor: la preparación queda cargada sin validar. El reproceso de
        la misma notificación no vuelve a cargar nada, pero tiene que validarla."""
        picking = self._preparo(self.tiendas[4], [(self.producto_a, 2)])
        caja = self._contenedor('WISCO-REPRO', 101, picking.codigo_unico, [(self.producto_a, 2)])
        Picking = type(self.env['stock.picking'])

        def caida(self):
            raise UserError("Uruware fuera de servicio (prueba)")
        with patch.object(Picking, 'button_validate', caida):
            try:
                self._preparada([picking], [caja])
            except ValueError:
                pass
        self.assertNotEqual(picking.state, 'done')
        self.assertEqual(picking.wms_estado, 'preparado')
        self._preparada([picking], [caja])
        self.assertEqual(picking.state, 'done')
        self.assertEqual(sum(picking.move_ids.mapped('quantity')), 2, "no se cargó dos veces")

    def test_stock_en_dos_paquetes_no_deja_lineas_en_cero(self):
        """CROSS/00375 y otros: la reserva dejaba dos líneas y una quedaba en 0."""
        tienda = self.tiendas[1]
        producto = self._producto('Remera dos paquetes', 'PRD-TMP-DOS')
        for n in (1, 2):
            paq = self.env['stock.quant.package'].create({'name': f'PAQ-DOS-{n}'})
            self.env['stock.quant']._update_available_quantity(producto, self.origen, 6, package_id=paq)
        picking = self._preparo(tienda, [(producto, 10)])
        self.assertEqual(len(picking.move_line_ids), 2)
        self._preparada([picking], [self._contenedor('WISCO-DOS', 97, picking.codigo_unico, [(producto, 10)])])
        self.assertEqual(picking.state, 'done')
        self.assertFalse(picking.move_line_ids.filtered(lambda l: l.quantity <= 0))

    def test_pedido_que_solo_esta_en_las_cajas_se_procesa(self):
        """642399: WIS mandó cajas de pedidos que no estaban en la lista Pedidos."""
        a = self._preparo(self.tiendas[2], [(self.producto_a, 1)])
        b = self._preparo(self.tiendas[3], [(self.producto_b, 1)])
        self.controller._handle_mercaderia_preparada({
            'fechaFacturacion': '20/10/2026',
            'pedidos': [{'pedido': a.codigo_unico}],
            'contenedores': [self._contenedor('WISCO-LA', 98, a.codigo_unico, [(self.producto_a, 1)]),
                             self._contenedor('WISCO-LB', 99, b.codigo_unico, [(self.producto_b, 1)])],
        })
        self.assertEqual(a.state, 'done')
        self.assertEqual(b.state, 'done')

    def test_ajuste_fuera_de_escala_queda_retenido(self):
        """Ajuste 275407 «ASD»: +430.970 bolsas aplicadas sin control."""
        self.env['ir.config_parameter'].sudo().set_param('integracion_wis.ajuste_tope_unidades', '5000')
        antes = self.env['stock.picking'].search_count([])
        self.controller._wis_retenida = False
        self.controller._handle_ajustes({'ajustes': [{
            'producto': self.producto_a.codigo_unico, 'cantidadMovimiento': 430970.0,
            'tipoAjuste': 'US', 'numeroAjusteStock': 275407, 'descripcionMotivo': 'ASD'}]})
        self.assertTrue(self.controller._wis_retenida)
        self.assertEqual(self.env['stock.picking'].search_count([]), antes)


@tagged('post_install', '-at_install', 'wis_notificaciones')
class TestNotificacionesWIS(TransactionCase):

    def test_reenvio_con_el_mismo_numero_no_se_registra_dos_veces(self):
        Notif = self.env['wis.notificacion']
        payload = {'Id': 'ConfirmacionMercaderiaPreparada', 'NumeroInterfazEjecucion': 645737,
                   'ConfirmacionMercaderiaPreparada': {'Pedidos': [{'Pedido': 'W-P-1'}], 'Contenedores': []}}
        n1, nueva1 = Notif._registrar('confirmacionMercaderiaPreparada', 645737, payload)
        n2, nueva2 = Notif._registrar('confirmacionMercaderiaPreparada', 645737, payload)
        self.assertTrue(nueva1)
        self.assertFalse(nueva2)
        self.assertEqual(n1, n2)

    def test_pedidos_de_la_lista_y_de_las_cajas(self):
        datos = {'Pedidos': [{'Pedido': 'W-P-1'}],
                 'Contenedores': [{'Detalles': [{'Pedido': 'W-P-1'}, {'Pedido': 'W-P-2'}]}]}
        self.assertEqual(self.env['wis.notificacion']._pedidos_de('confirmacionPedido', datos),
                         ['W-P-1', 'W-P-2'])

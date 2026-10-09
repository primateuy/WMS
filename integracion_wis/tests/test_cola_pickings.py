# -*- coding: utf-8 -*-
"""Cola de envíos de pickings a WIS (`wis.picking.cola`).

No golpean la red: `enviarWS` se reemplaza por un doble.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'wis_cola_pickings')
class TestColaPickingsWIS(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        if not cls.config:
            cls.config = cls.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test', 'client_secret': 'test', 'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api',
                'company_id': cls.env.company.id,
            })
        cls.config.write({'comunicacion_activa': True, 'cola_max_intentos': 3})

        cls.almacen = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        cls.tipo = cls.env['stock.picking.type'].create({
            'name': 'Pedido WIS de prueba',
            'code': 'internal',
            'sequence_code': 'TWIS',
            'warehouse_id': cls.almacen.id,
            'default_location_src_id': cls.almacen.lot_stock_id.id,
            'default_location_dest_id': cls.almacen.lot_stock_id.id,
            'integracion_wms': True,
            'estado_disparo_wis': 'confirmed',
            'tipo_pedido_wis': 'REP',
            'reservation_method': 'manual',
        })
        cls.producto = cls.env['product.product'].create({
            'name': 'Producto cola WIS', 'type': 'product'})
        cls.producto.with_context(_avoid_wms=True).codigo_unico = 'PRD-TEST-COLA'
        cls.partner = cls.env['res.partner'].create({
            'name': 'Sucursal cola WIS', 'codigo_unico_cliente': 'CLI-TEST-COLA'})
        cls.destino = cls.env['stock.location'].create({
            'name': 'Destino cola WIS', 'usage': 'internal',
            'location_id': cls.almacen.view_location_id.id})

    def _nuevo_picking(self):
        # Se crea sin disparar WIS y se devuelve con el contexto limpio: el contexto de
        # creación queda pegado al registro y saltearía WIS en todo lo que sigue.
        picking = self.env['stock.picking'].with_context(skip_wms_integration=True).create({
            'picking_type_id': self.tipo.id,
            'partner_id': self.partner.id,
            'location_id': self.almacen.lot_stock_id.id,
            'location_dest_id': self.destino.id,
            'move_ids': [(0, 0, {
                'name': 'cola', 'product_id': self.producto.id, 'product_uom_qty': 7,
                'product_uom': self.producto.uom_id.id,
                'location_id': self.almacen.lot_stock_id.id,
                'location_dest_id': self.destino.id,
            })],
        })
        return self.env['stock.picking'].browse(picking.id)

    def _encolado(self):
        picking = self._nuevo_picking()
        picking.with_context(wis_encolar_envios=True).action_confirm()
        return picking

    def _parchear_envio(self, **kwargs):
        return patch.object(type(self.env['stock.picking']), 'enviarWS', autospec=True, **kwargs)

    # ------------------------------------------------------------------
    def test_con_contexto_encola_en_lugar_de_enviar(self):
        with self._parchear_envio() as enviar:
            picking = self._encolado()
        enviar.assert_not_called()
        self.assertEqual(picking.wms_estado, 'en_cola')
        self.assertEqual(picking.codigo_unico, f"W-P-{picking.id}",
                         "el código se asigna al encolar, igual que lo armaría el envío")
        entrada = self.env['wis.picking.cola'].search([('picking_id', '=', picking.id)])
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertEqual(entrada.tipo, 'REP')

    def test_sin_contexto_envia_como_siempre(self):
        picking = self._nuevo_picking()
        with self._parchear_envio(return_value={'numeroInterfaz': 77}) as enviar:
            picking.action_confirm()
        enviar.assert_called_once()
        self.assertEqual(picking.wms_estado, 'enviado')
        self.assertFalse(self.env['wis.picking.cola'].search([('picking_id', '=', picking.id)]))

    def test_cola_envia_y_marca_enviado(self):
        picking = self._encolado()
        with self._parchear_envio(return_value={'numeroInterfaz': 4242}) as enviar:
            self.env['wis.picking.cola']._cron_procesar()
        enviar.assert_called_once()
        self.assertTrue(enviar.call_args.args[0].env.context.get('wis_envio_desde_cola'))
        entrada = self.env['wis.picking.cola'].search([('picking_id', '=', picking.id)])
        self.assertEqual(entrada.estado, 'enviado')
        self.assertEqual(picking.wms_estado, 'enviado')
        self.assertEqual(picking.idPedidoWMS, '4242')
        self.assertEqual(picking.codigo_unico, f"W-P-{picking.id}")
        self.assertEqual(picking.move_ids.wis_cantidad_original, 7,
                         "la cantidad original se fija al pasar a 'enviado'")

    def test_falla_reintenta_en_la_proxima_corrida(self):
        picking = self._encolado()
        entrada = self.env['wis.picking.cola'].search([('picking_id', '=', picking.id)])
        with self._parchear_envio(side_effect=Exception("WIS caído")) as enviar:
            self.env['wis.picking.cola']._cron_procesar()
        self.assertEqual(enviar.call_count, 1, "una falla no se reintenta en la misma corrida")
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertEqual(entrada.intentos, 1)
        self.assertEqual(picking.wms_estado, 'en_cola')

        with self._parchear_envio(side_effect=Exception("WIS caído")):
            self.env['wis.picking.cola']._cron_procesar()
            self.env['wis.picking.cola']._cron_procesar()
        self.assertEqual(entrada.estado, 'error')
        self.assertEqual(entrada.intentos, 3)
        self.assertEqual(picking.wms_estado, 'sin_enviar',
                         "agotados los intentos vuelve a 'sin enviar', con su código")
        self.assertEqual(picking.codigo_unico, f"W-P-{picking.id}")

        with self._parchear_envio(return_value={'numeroInterfaz': 1}):
            entrada.action_reintentar()
            self.assertEqual(picking.wms_estado, 'en_cola')
            self.env['wis.picking.cola']._cron_procesar()
        self.assertEqual(entrada.estado, 'enviado')

    def test_cancelar_lo_encolado_no_llama_a_wis(self):
        picking = self._encolado()
        with self._parchear_envio() as enviar, \
                patch.object(type(self.config), 'anularReferenciaRecepcion') as anular:
            picking.action_cancel()
        enviar.assert_not_called()
        anular.assert_not_called()
        self.assertEqual(picking.state, 'cancel')
        entrada = self.env['wis.picking.cola'].search([('picking_id', '=', picking.id)])
        self.assertEqual(entrada.estado, 'cancelado')

    def test_envio_desde_cola_acepta_picking_con_codigo(self):
        """`insertarPedidos` rechaza un picking con código, salvo que venga de la cola."""
        picking = self._encolado()
        with patch.object(type(self.config), 'consultarAPI', autospec=True,
                          return_value={'numeroInterfaz': 9}) as api:
            with self.assertRaises(Exception):
                self.config.insertarPedidos(picking, 'REP')
            self.config.with_context(wis_envio_desde_cola=True).insertarPedidos(picking, 'REP')
        cuerpo = api.call_args.kwargs['body']
        self.assertEqual(cuerpo['pedidos'][0]['nroPedido'], picking.codigo_unico)

    def test_falla_en_write_de_estado_no_anula_la_operacion(self):
        """Antes el hook de `write(state)` relanzaba el error de WIS y deshacía todo."""
        picking = self._nuevo_picking()
        with self._parchear_envio(side_effect=Exception("WIS caído")):
            picking.write({'state': 'confirmed'})
        self.assertEqual(picking.state, 'confirmed')
        self.assertEqual(picking.codigo_unico, f"W-P-{picking.id}")
        self.assertEqual(picking.wms_estado, 'sin_enviar')

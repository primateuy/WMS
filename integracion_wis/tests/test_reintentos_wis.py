# -*- coding: utf-8 -*-
"""Reintentos hacia WIS: que nada se le vuelva a mandar sin motivo.

12-oct-2026, support: WIS reportó reintentos «infinitos». Una recepción de 1000 líneas
llegó 16 veces en 25 min: tardaba más que el timeout, se reenviaba en la corrida
siguiente —segundos después— y «Enviar ahora» la mandaba a la par del cron. Los rechazos
de datos (500 con LogID) se reenviaban igual que un problema de conexión.

No golpean la red: se reemplaza `enviarWS`, `_enviar_wms_en_lote` o `requests`.
"""
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.tools import config as odoo_config

from ..models.models import (WisErrorGeneral, WisTimeout, corte_odoo, log_id_wis,
                             timeout_maximo)

MODELO_API = 'odoo.addons.integracion_wis.models.models.IntegracionWIS'
REQUESTS = 'odoo.addons.integracion_wis.models.models.requests.request'
CUERPO_500 = ('{"LogID":"b714c2de-2801-43af-897e-85df94ffa34d","Message":"Se ha producido '
              'un error no controlado. Por favor consulte con sistemas indicando el LogId"}')
ERROR_500 = 'Error en la API: 500 - ' + CUERPO_500


def _corte(real, cron=-1, workers=4):
    """Simula los límites de tiempo del servidor de Odoo."""
    return patch.dict(odoo_config.options, {
        'limit_time_real': real, 'limit_time_real_cron': cron, 'workers': workers})


class _Respuesta:
    def __init__(self, status, texto='{}', datos=None):
        self.status_code = status
        self.text = texto
        self._datos = datos or {}

    def json(self):
        return self._datos


@tagged('post_install', '-at_install', 'wis_reintentos')
class TestReintentosWIS(TransactionCase):

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
        cls.config.write({
            'comunicacion_activa': True, 'cola_max_intentos': 5,
            'reintentar_errores_datos': False, 'espera_timeout_minutos': 30,
            'timeout_api': 60, 'timeout_api_operaciones': 120,
            'pausa_hasta': False, 'pausa_fallos': 0,
            'chunk_size_productos': 200, 'cola_productos_por_corrida': 500,
            'verificar_barcode_existente': False,
            'token': 'tok', 'expiracionToken': fields.Datetime.now() + timedelta(hours=1),
        })
        cls.Cola = cls.env['wis.picking.cola']
        cls.ColaProductos = cls.env['wis.sync.queue']
        cls.Producto = cls.env['product.product']

        cls.almacen = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        cls.tipo = cls.env['stock.picking.type'].create({
            'name': 'Pedido WIS reintentos',
            'code': 'internal',
            'sequence_code': 'TWRE',
            'warehouse_id': cls.almacen.id,
            'default_location_src_id': cls.almacen.lot_stock_id.id,
            'default_location_dest_id': cls.almacen.lot_stock_id.id,
            'integracion_wms': True,
            'estado_disparo_wis': 'confirmed',
            'tipo_pedido_wis': 'REP',
            'reservation_method': 'manual',
        })
        cls.producto = cls.Producto.create({'name': 'Producto reintentos WIS', 'type': 'product'})
        cls.producto.with_context(_avoid_wms=True).codigo_unico = 'PRD-TEST-REINT'
        cls.partner = cls.env['res.partner'].create({
            'name': 'Sucursal reintentos WIS', 'codigo_unico_cliente': 'CLI-TEST-REINT'})
        cls.destino = cls.env['stock.location'].create({
            'name': 'Destino reintentos WIS', 'usage': 'internal',
            'location_id': cls.almacen.view_location_id.id})

    # ------------------------------------------------------------------
    # Ayudas
    # ------------------------------------------------------------------
    def _encolado(self):
        picking = self.env['stock.picking'].with_context(skip_wms_integration=True).create({
            'picking_type_id': self.tipo.id,
            'partner_id': self.partner.id,
            'location_id': self.almacen.lot_stock_id.id,
            'location_dest_id': self.destino.id,
            'move_ids': [(0, 0, {
                'name': 'reintentos', 'product_id': self.producto.id, 'product_uom_qty': 3,
                'product_uom': self.producto.uom_id.id,
                'location_id': self.almacen.lot_stock_id.id,
                'location_dest_id': self.destino.id,
            })],
        })
        picking = self.env['stock.picking'].browse(picking.id)
        picking.with_context(wis_encolar_envios=True).action_confirm()
        return picking, self.Cola.search([('picking_id', '=', picking.id)])

    def _parchear_envio(self, **kwargs):
        return patch.object(type(self.env['stock.picking']), 'enviarWS', autospec=True, **kwargs)

    def _variantes(self, cantidad, nombre='Reintentos'):
        return self.Producto.create([{'name': f'{nombre} {i}', 'type': 'product'}
                                     for i in range(cantidad)])

    # ------------------------------------------------------------------
    # Cómo se clasifica la respuesta de WIS
    # ------------------------------------------------------------------
    def test_timeout_de_lectura_es_wis_timeout(self):
        with patch(REQUESTS, side_effect=requests.exceptions.ReadTimeout("Read timed out.")):
            with self.assertRaises(WisTimeout):
                self.config.consultarAPI('/Pedido/Create', None, {}, method='POST')

    def test_conexion_caida_es_error_general_no_timeout(self):
        with patch(REQUESTS, side_effect=requests.exceptions.ConnectionError("refused")):
            with self.assertRaises(WisErrorGeneral) as error:
                self.config.consultarAPI('/Pedido/Create', None, {}, method='POST')
        self.assertNotIsInstance(error.exception, WisTimeout)

    def test_500_con_log_id_es_error_de_datos(self):
        with patch(REQUESTS, return_value=_Respuesta(500, CUERPO_500)):
            with self.assertRaises(ValidationError) as error:
                self.config.consultarAPI('/Pedido/Create', None, {}, method='POST')
        self.assertNotIsInstance(error.exception, WisErrorGeneral)
        self.assertEqual(log_id_wis(str(error.exception)), 'b714c2de-2801-43af-897e-85df94ffa34d')

    def test_503_es_error_general(self):
        with patch(REQUESTS, return_value=_Respuesta(503, 'The service is unavailable.')):
            with self.assertRaises(WisErrorGeneral):
                self.config.consultarAPI('/Pedido/Create', None, {}, method='POST')

    def test_el_timeout_sale_de_la_configuracion(self):
        with patch(REQUESTS, return_value=_Respuesta(200)) as req:
            self.config.consultarAPI('/Producto/CreateOrUpdate', None, {}, method='POST')
            self.config.consultarAPI('/Pedido/Create', None, {}, method='POST', operaciones=True)
        self.assertEqual(req.call_args_list[0].kwargs['timeout'], 60)
        self.assertEqual(req.call_args_list[1].kwargs['timeout'], 120)

    def test_recepciones_y_pedidos_usan_el_timeout_de_operaciones(self):
        picking, _entrada = self._encolado()
        with patch(f'{MODELO_API}.consultarAPI', autospec=True,
                   return_value={'numeroInterfaz': 9}) as api:
            self.config.with_context(wis_envio_desde_cola=True).insertarPedidos(picking, 'REP')
        self.assertTrue(api.call_args.kwargs.get('operaciones'))

    def test_timeout_fuera_de_rango_no_se_acepta(self):
        with _corte(240), self.assertRaises(ValidationError):
            self.config.timeout_api_operaciones = 300

    def test_el_presupuesto_deja_terminar_el_ultimo_envio(self):
        """Odoo mata la corrida a los 240 s: el último envío tiene que entrar."""
        with _corte(240):
            self.assertEqual(self.config._presupuesto_corrida(operaciones=True), 200 - 120)
            self.assertEqual(self.config._presupuesto_corrida(llamadas_por_envio=2), 200 - 2 * 60)

    def test_el_tope_sale_del_corte_del_servidor(self):
        """Con el corte de producción (480 s) se puede configurar hasta 420 s: 60 s de
        margen para que el envío termine antes de que Odoo mate el proceso."""
        with _corte(480):
            self.assertEqual(timeout_maximo(), 420)
            self.config.timeout_api_operaciones = 300
            self.config.timeout_api_operaciones = 420
            with self.assertRaisesRegex(ValidationError, '420.*480'):
                self.config.timeout_api_operaciones = 421
            # Con el tope, la cola empieza envíos durante 20 s y el último termina a los
            # 440 s: 40 s antes del corte.
            self.assertEqual(self.config._presupuesto_corrida(operaciones=True), 20)
            self.config.timeout_api_operaciones = 300
            self.assertEqual(self.config._presupuesto_corrida(operaciones=True), 440 - 300)

    def test_el_corte_de_cron_manda_si_es_menor(self):
        with _corte(600, cron=300):
            self.assertEqual(corte_odoo(), 300)
            self.assertEqual(timeout_maximo(), 240)
        with _corte(480, cron=0):
            # 0 = los crons sin corte: queda el de las peticiones.
            self.assertEqual(corte_odoo(), 480)

    def test_sin_workers_se_supone_el_corte_por_defecto(self):
        """Sin workers Odoo no corta nada: se toma 240 s, el tope queda en 180 s."""
        with _corte(480, workers=0):
            self.assertEqual(corte_odoo(), 240)
            self.assertEqual(timeout_maximo(), 180)
        with _corte(0):
            self.assertEqual(corte_odoo(), 240)

    def test_enviar_ws_no_disfraza_el_timeout(self):
        """`enviarWS` envolvía todo en ValidationError: la cola no podía distinguir un
        timeout de un rechazo y lo reenviaba como cualquier error."""
        picking, _entrada = self._encolado()
        with patch(f'{MODELO_API}.insertarPedidos', side_effect=WisTimeout("Read timed out")):
            with self.assertRaises(WisTimeout):
                picking.with_context(wis_envio_desde_cola=True).enviarWS('REP')

    # ------------------------------------------------------------------
    # Cola de operaciones
    # ------------------------------------------------------------------
    def test_rechazo_de_datos_no_se_reenvia_solo(self):
        picking, entrada = self._encolado()
        with self._parchear_envio(side_effect=ValidationError(ERROR_500)) as enviar:
            self.Cola._cron_procesar()
            self.Cola._cron_procesar()
        self.assertEqual(enviar.call_count, 1, "un rechazo de WIS no se vuelve a mandar solo")
        self.assertEqual(entrada.estado, 'error')
        self.assertEqual(entrada.tipo_error, 'datos')
        self.assertEqual(entrada.log_id, 'b714c2de-2801-43af-897e-85df94ffa34d')
        self.assertEqual(picking.wms_estado, 'sin_enviar')
        self.assertFalse(self.config.pausa_hasta, "un rechazo de datos no pausa a nadie")

    def test_timeout_espera_antes_de_reenviar_y_corta_la_corrida(self):
        _p1, primera = self._encolado()
        _p2, segunda = self._encolado()
        with self._parchear_envio(side_effect=WisTimeout("Read timed out")) as enviar:
            self.Cola._cron_procesar()
        self.assertEqual(enviar.call_count, 1, "tras un timeout no se manda la siguiente")
        self.assertEqual(primera.estado, 'pendiente')
        self.assertEqual(primera.tipo_error, 'sin_confirmar')
        self.assertEqual(primera.intentos, 1)
        self.assertGreaterEqual(primera.proximo_intento,
                                fields.Datetime.now() + timedelta(minutes=29))
        self.assertEqual(segunda.estado, 'pendiente')
        self.assertEqual(segunda.intentos, 0)
        self.assertTrue(self.config._wis_en_pausa())

        with self._parchear_envio(return_value={'numeroInterfaz': 1}) as enviar:
            self.Cola._cron_procesar()
        enviar.assert_not_called()

    def test_terminada_la_pausa_sale_la_siguiente_y_no_la_del_timeout(self):
        _p1, primera = self._encolado()
        _p2, segunda = self._encolado()
        with self._parchear_envio(side_effect=WisTimeout("Read timed out")):
            self.Cola._cron_procesar()
        self.config.pausa_hasta = fields.Datetime.now() - timedelta(seconds=1)
        with self._parchear_envio(return_value={'numeroInterfaz': 1}) as enviar:
            self.Cola._cron_procesar()
        self.assertEqual(enviar.call_count, 1)
        self.assertEqual(segunda.estado, 'enviado')
        self.assertEqual(primera.estado, 'pendiente', "la del timeout espera su turno")
        self.assertFalse(self.config.pausa_fallos, "un envío que sale levanta la pausa")

    def test_timeouts_agotan_los_intentos(self):
        picking, entrada = self._encolado()
        with self._parchear_envio(side_effect=WisTimeout("Read timed out")):
            for _i in range(5):
                entrada.proximo_intento = False
                self.config.pausa_hasta = False
                self.Cola._cron_procesar()
        self.assertEqual(entrada.estado, 'error')
        self.assertEqual(entrada.intentos, 5)
        self.assertEqual(picking.wms_estado, 'sin_enviar')

    def test_wis_sin_atender_no_cuenta_intento(self):
        _picking, entrada = self._encolado()
        with self._parchear_envio(side_effect=WisErrorGeneral("Error en la API: 503")):
            self.Cola._cron_procesar()
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertEqual(entrada.tipo_error, 'transitorio')
        self.assertEqual(entrada.intentos, 0)
        self.assertTrue(self.config._wis_en_pausa())

    def test_la_pausa_se_alarga_con_cada_falla_seguida(self):
        minutos = []
        for _i in range(4):
            antes = fields.Datetime.now()
            self.config._wis_pausar('prueba')
            minutos.append(round((self.config.pausa_hasta - antes).total_seconds() / 60))
        self.assertEqual(minutos, [5, 15, 60, 60])
        self.config._wis_levantar_pausa()
        self.assertFalse(self.config.pausa_hasta)
        self.assertEqual(self.config.pausa_fallos, 0)

    def test_enviar_ahora_no_envia_en_la_peticion(self):
        """Antes mandaba en la petición del usuario, a la par del cron."""
        _p1, primera = self._encolado()
        _p2, segunda = self._encolado()
        with self._parchear_envio(return_value={'numeroInterfaz': 1}) as enviar:
            accion = segunda.action_enviar_ahora()
            enviar.assert_not_called()
            self.assertEqual(accion['params']['type'], 'info')
            self.assertEqual(segunda.prioridad, 0)
            self.assertEqual(self.Cola._siguiente([]), segunda, "sale primera")
            self.Cola._cron_procesar()
        self.assertEqual(enviar.call_args_list[0].args[0], segunda.picking_id)
        self.assertEqual((primera | segunda).mapped('estado'), ['enviado', 'enviado'])

    def test_enviar_ahora_en_pausa_avisa_y_espera(self):
        _picking, entrada = self._encolado()
        self.config._wis_pausar('prueba')
        with self._parchear_envio() as enviar:
            accion = entrada.action_enviar_ahora()
            self.Cola._cron_procesar()
        enviar.assert_not_called()
        self.assertEqual(accion['params']['type'], 'warning')

    def test_enviar_ahora_adelanta_la_espera_de_un_timeout(self):
        _picking, entrada = self._encolado()
        with self._parchear_envio(side_effect=WisTimeout("Read timed out")):
            self.Cola._cron_procesar()
        self.config._wis_levantar_pausa()
        entrada.action_enviar_ahora()
        self.assertFalse(entrada.proximo_intento)
        with self._parchear_envio(return_value={'numeroInterfaz': 1}):
            self.Cola._cron_procesar()
        self.assertEqual(entrada.estado, 'enviado')

    def test_la_que_se_esta_enviando_no_la_toma_otro(self):
        _picking, entrada = self._encolado()
        entrada.estado = 'procesando'
        self.assertFalse(self.Cola._siguiente([]))
        with self._parchear_envio() as enviar:
            self.Cola._cron_procesar()
            entrada.action_enviar_ahora()
        enviar.assert_not_called()

    def test_envio_cortado_a_mitad_se_trata_como_timeout(self):
        _picking, entrada = self._encolado()
        entrada.estado = 'procesando'
        entrada.flush_recordset()
        self.env.cr.execute("UPDATE wis_picking_cola SET write_date = now() - interval '1 hour' "
                            "WHERE id = %s", [entrada.id])
        entrada.invalidate_recordset()
        with self._parchear_envio() as enviar:
            self.Cola._cron_procesar()
        enviar.assert_not_called()
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertEqual(entrada.tipo_error, 'sin_confirmar')
        self.assertGreater(entrada.proximo_intento, fields.Datetime.now())

    # ------------------------------------------------------------------
    # Cola de productos
    # ------------------------------------------------------------------
    def _enviar_rechazando(self, rechazadas):
        config = self.config

        def enviar(variantes, motivo=''):
            errores = {}
            for v in variantes:
                codigo = config._wis_codigo_producto(v)
                if v in rechazadas:
                    errores[codigo] = f"{codigo}: 400 - rechazado"
                elif not v.codigo_unico:
                    v.with_context(_avoid_wms=True).codigo_unico = codigo
            return {'enviados': len(variantes) - len(errores), 'errores': len(errores),
                    'errores_detalle': list(errores.values()), 'errores_por_codigo': errores,
                    'barcodes_enviados': 0}
        return patch.object(type(self.Producto), '_enviar_wms_en_lote', enviar)

    def test_producto_rechazado_no_se_reenvia_solo(self):
        variante = self._variantes(1)
        entrada = self.ColaProductos._encolar(variante)
        with self._enviar_rechazando(variante):
            self.ColaProductos._cron_procesar_cola()
        self.assertEqual((entrada.estado, entrada.subestado), ('pendiente', 'error'))
        self.assertFalse(entrada.proximo_intento)
        self.assertFalse(self.ColaProductos._siguiente_lote(10),
                         "un rechazo no vuelve a salir solo")

        entrada.action_reintentar()
        self.assertEqual(self.ColaProductos._siguiente_lote(10), entrada,
                         "con «Reintentar» sale, antes que lo no enviado")

    def test_producto_timeout_pausa_la_cola_de_productos_y_la_de_operaciones(self):
        self.ColaProductos._encolar(self._variantes(2))
        llamadas = []

        def lento(variantes, motivo=''):
            llamadas.append(1)
            raise WisTimeout("Read timed out")

        with patch.object(type(self.Producto), '_enviar_wms_en_lote', lento):
            self.ColaProductos._cron_procesar_cola()
            self.ColaProductos._cron_procesar_cola()
        self.assertEqual(len(llamadas), 1, "en pausa la cola de productos no manda nada")

        _picking, entrada = self._encolado()
        with self._parchear_envio() as enviar:
            self.Cola._cron_procesar()
        enviar.assert_not_called()
        self.assertEqual(entrada.estado, 'pendiente')

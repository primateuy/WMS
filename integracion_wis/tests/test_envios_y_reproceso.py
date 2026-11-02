# -*- coding: utf-8 -*-
"""Lo que se manda a WIS queda registrado, y un webhook se reprocesa sin que WIS lo reenvíe.

Forum, 02-11-2026: «cuando recibimos un webhook de WIS, si en Odoo algo falla no hay manera
de que WIS lo vuelva a enviar». La notificación ya está guardada: se corrige en Odoo y se
reprocesa. Lo que ya había entrado no entra dos veces.

No golpean la red: se reemplaza `requests.request`.
"""
import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


def _lanza(test, excepcion, funcion):
    """Como assertRaises, pero sin el savepoint de Odoo: deshacerlo se llevaría el registro del
    envío, que en producción va en su propio cursor y queda."""
    try:
        funcion()
    except excepcion:
        return
    test.fail(f"no lanzó {excepcion.__name__}")

REQUESTS = 'odoo.addons.integracion_wis.models.models.requests.request'


class _Respuesta:
    def __init__(self, status, texto='{}'):
        self.status_code = status
        self.text = texto

    def json(self):
        return json.loads(self.text)


def _config(env):
    Config = env['integracion_wis.integracion_wis']
    config = Config.search([('company_id', '=', env.company.id)], limit=1) or Config.create({
        'client_id': 'test', 'client_secret': 'test', 'empresa_id': 1,
        'url_access_token': 'https://example.invalid/token',
        'apiLink': 'https://example.invalid/api', 'company_id': env.company.id,
    })
    config.write({'token': 'tok', 'expiracionToken': fields.Datetime.now() + timedelta(hours=1),
                  'pausa_hasta': False, 'pausa_fallos': 0})
    return config


@tagged('post_install', '-at_install', 'wis_envios')
class TestEnviosAWIS(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = _config(cls.env)
        cls.Log = cls.env['wis.envio.log']
        almacen = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.picking = cls.env['stock.picking'].with_context(skip_wms_integration=True).create({
            'picking_type_id': almacen.int_type_id.id,
            'location_id': almacen.lot_stock_id.id, 'location_dest_id': almacen.lot_stock_id.id,
        })
        cls.codigo = f"W-P-{cls.picking.id}"
        cls.picking.with_context(skip_wms_integration=True).codigo_unico = cls.codigo

    def _ultimo(self):
        return self.Log.search([], limit=1)

    def test_envio_aceptado_queda_con_lo_mandado_y_la_respuesta(self):
        body = {'empresa': 1, 'pedidos': [{'nroPedido': self.codigo, 'detalles': [{'producto': 'X'}]}]}
        with patch(REQUESTS, return_value=_Respuesta(200, '{"ok": true}')):
            self.config.consultarAPI('/Pedido/Create', None, body, method='POST')
        log = self._ultimo()
        self.assertEqual(log.endpoint, '/Pedido/Create')
        self.assertEqual(log.metodo, 'POST')
        self.assertEqual(log.resultado, 'exito')
        self.assertEqual(log.status_http, 200)
        self.assertEqual(json.loads(log.request), body)
        self.assertEqual(json.loads(log.respuesta), {'ok': True})
        self.assertEqual(log.codigo_unico, self.codigo)
        self.assertEqual(log.picking_id, self.picking)
        self.assertEqual(self.picking.wis_envio_count, 1)

    def test_rechazo_de_wis_queda_registrado(self):
        with patch(REQUESTS, return_value=_Respuesta(400, '{"Message": "Producto inexistente"}')):
            _lanza(self, ValidationError, lambda: self.config.consultarAPI(
                '/Pedido/Create', None, {'nroPedido': self.codigo}, method='POST'))
        log = self._ultimo()
        self.assertEqual(log.resultado, 'rechazado')
        self.assertEqual(log.status_http, 400)
        self.assertIn('Producto inexistente', log.respuesta)

    def test_sin_respuesta_queda_registrado(self):
        with patch(REQUESTS, side_effect=requests.exceptions.ConnectionError('conexión rechazada')):
            _lanza(self, ValidationError, lambda: self.config.consultarAPI(
                '/Pedido/Create', None, {'nroPedido': self.codigo}, method='POST'))
        log = self._ultimo()
        self.assertEqual(log.resultado, 'error')
        self.assertFalse(log.status_http)
        self.assertIn('conexión rechazada', log.error)

    def test_no_se_guarda_el_token(self):
        with patch(REQUESTS, return_value=_Respuesta(200)):
            self.config.consultarAPI('/Pedido/Create', None, {'nroPedido': self.codigo}, method='POST')
        log = self._ultimo()
        for campo in ('request', 'params', 'respuesta', 'error'):
            self.assertNotIn('tok', log[campo] or '')

    def test_la_limpieza_borra_solo_lo_viejo(self):
        with patch(REQUESTS, return_value=_Respuesta(200)):
            self.config.consultarAPI('/Pedido/Create', None, {}, method='POST')
            self.config.consultarAPI('/Pedido/Create', None, {}, method='POST')
        viejo, nuevo = self.Log.search([], limit=2)
        viejo.fecha = fields.Datetime.now() - timedelta(days=91)
        self.Log._cron_limpiar()
        self.assertFalse(viejo.exists())
        self.assertTrue(nuevo.exists())


@tagged('post_install', '-at_install', 'wis_reproceso')
class TestReprocesoWebhook(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.config = _config(cls.env)
        cls.config.comunicacion_activa = False
        almacen = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.stock = almacen.lot_stock_id
        ajustes = cls.env['stock.location'].create({'name': 'Ajustes WIS (test)', 'usage': 'inventory'})
        cls.tipo = cls.env['stock.picking.type'].create({
            'name': 'Ajuste WIS (test)', 'code': 'internal', 'sequence_code': 'TAJW',
            'warehouse_id': almacen.id,
            'default_location_src_id': ajustes.id, 'default_location_dest_id': cls.stock.id,
        })
        cls.config.picking_type_ajuste_wis_id = cls.tipo
        cls.env['ir.config_parameter'].sudo().set_param('integracion_wis.ajuste_tope_unidades', '0')
        cls.producto_a = cls._producto('Remera reproceso', 'PRD-REP-A')
        cls.producto_b = cls.env['product.product'].create({'name': 'Short reproceso', 'type': 'product'})

    @classmethod
    def _producto(cls, nombre, codigo):
        producto = cls.env['product.product'].create({'name': nombre, 'type': 'product'})
        producto.with_context(_avoid_wms=True).codigo_unico = codigo
        return producto

    def _payload(self, numero):
        return {'Id': 'Ajustes', 'NumeroInterfazEjecucion': numero, 'Ajustes': {'Ajustes': [
            {'Producto': 'PRD-REP-A', 'CantidadMovimiento': 7, 'TipoAjuste': 'US',
             'NumeroAjusteStock': 9001, 'DescripcionMotivo': 'reproceso'},
            {'Producto': 'PRD-REP-B', 'CantidadMovimiento': 3, 'TipoAjuste': 'US',
             'NumeroAjusteStock': 9001, 'DescripcionMotivo': 'reproceso'},
        ]}}

    def _cantidad(self, producto):
        return producto.with_context(location=self.stock.id).qty_available

    def _procesar(self, notif):
        notif._procesar(10 ** 12)

    def test_corregir_y_reprocesar_aplica_solo_lo_que_faltaba(self):
        Notif = self.env['wis.notificacion']
        notif, _nueva = Notif._registrar('ajustes', 880001, self._payload(880001))
        self._procesar(notif)
        # El producto B no existe con ese código en Odoo: la notificación queda con errores.
        self.assertEqual(notif.estado, 'con_errores')
        self.assertIn('PRD-REP-B', notif.errores)
        a_antes = self._cantidad(self.producto_a)

        # Se corrige en Odoo y se reprocesa desde el registro del webhook.
        self.producto_b.with_context(_avoid_wms=True).codigo_unico = 'PRD-REP-B'
        log = self.env['wis.webhook.log'].search([('notificacion_id', '=', notif.id)], limit=1)
        self.assertTrue(log, 'el procesamiento deja su registro enlazado a la notificación')
        log.action_reprocesar()
        self.assertEqual(notif.estado, 'pendiente')
        self._procesar(notif)

        self.assertEqual(notif.estado, 'hecho')
        self.assertEqual(self._cantidad(self.producto_b), 3)
        self.assertEqual(self._cantidad(self.producto_a), a_antes, 'lo que ya había entrado no se repite')

        # Reprocesar una que terminó bien no aplica nada de nuevo.
        notif.action_reprocesar()
        self._procesar(notif)
        self.assertEqual(self._cantidad(self.producto_a), a_antes)
        self.assertEqual(self._cantidad(self.producto_b), 3)

    def test_notificacion_vieja_de_ajustes_bien_procesada_no_se_reprocesa(self):
        notif, _nueva = self.env['wis.notificacion']._registrar('ajustes', 880002, self._payload(880002))
        notif.write({'estado': 'hecho', 'marca_aplicado': False})
        with self.assertRaises(UserError):
            notif.action_reprocesar()

    def test_log_anterior_a_las_notificaciones(self):
        Log = self.env['wis.webhook.log']
        base = {'fecha': fields.Date.today(), 'hora': '10:00:00', 'estado': 'error'}
        ajuste = Log.create(dict(base, tipo='ajustes', numero_interfaz_ejecucion=880003,
                                 request=json.dumps(self._payload(880003))))
        with self.assertRaises(UserError):
            ajuste.action_reprocesar()
        llegada = Log.create(dict(base, tipo='ENTRADA_DEBUG', request='WEBHOOK RECIBIDO: Body: {...'))
        with self.assertRaises(UserError):
            llegada.action_reprocesar()
        recepcion = Log.create(dict(base, tipo='confirmacionRecepcion', numero_interfaz_ejecucion=880004,
                                    request=json.dumps({'Id': 'ConfirmacionRecepcion',
                                                        'ConfirmacionRecepcion': {'Referencias': []}})))
        recepcion.action_reprocesar()
        self.assertEqual(recepcion.notificacion_id.tipo, 'confirmacionRecepcion')
        self.assertEqual(recepcion.notificacion_id.estado, 'pendiente')

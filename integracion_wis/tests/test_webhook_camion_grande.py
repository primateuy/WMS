# -*- coding: utf-8 -*-
"""Webhooks de camiones grandes que se perdían con HTTP 200.

Producción, 02 al 20-11-2026: 12 ejecuciones de 6 camiones (686367, 686399, 705986, 705990,
706413, 706443, 715425, 715426, 716741, 716751, 742999, 743032) llegaron a Odoo y no quedó nada.
El registro de llegada ponía en `codigo_unico` los ~250-330 W-P del camión, el valor pasaba el
máximo del índice btree (2704 bytes) y el INSERT fallaba. El `except` lo tragaba, pero la
transacción quedaba abortada: la notificación no se guardaba y Odoo contestaba 200 igual, así
que WIS marcaba «ProcesadoOK».

Lo que se fija acá:
- el código se recorta y el log se graba, también con `write`;
- un camión de 400 pedidos queda registrado como notificación;
- si el registro falla, WIS recibe un HTTP 500 de verdad, no un 200.
"""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.exceptions import HTTPException

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.integracion_wis.controllers import controllers as wis_controllers

Controller = wis_controllers.WebhookWIS


def _payload_preparada(numero_interfaz, n_pedidos):
    pedidos = [{'Pedido': f'W-P-{41000 + i}', 'Contenedores': []} for i in range(n_pedidos)]
    return {'Id': 'ConfirmacionMercaderiaPreparada', 'NumeroInterfazEjecucion': numero_interfaz,
            'ConfirmacionMercaderiaPreparada': {'Pedidos': pedidos}}


@tagged('post_install', '-at_install')
class TestWebhookCamionGrande(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Log = cls.env['wis.webhook.log']
        cls.codigos = ', '.join(f'W-P-{41000 + i}' for i in range(400))

    def _llamar(self, payload):
        """Llama al callback como lo haría WIS, con el entorno del test y sin abrir cursores
        propios (un cursor aparte commitearía de verdad en la base de tests)."""
        body = json.dumps(payload).encode()
        env_test = self.env

        def update_env(user=None, **kw):
            fake.env = env_test(user=user) if user else env_test

        fake = SimpleNamespace(
            env=env_test,
            httprequest=SimpleNamespace(get_data=lambda: body, headers={}, remote_addr='10.0.0.1',
                                        method='POST'),
            update_env=update_env,
        )

        def log_aparte(vals):
            self.Log.create(vals)

        with patch.object(wis_controllers, 'request', fake), \
                patch.object(Controller, '_log_aparte', staticmethod(log_aparte)), \
                patch.object(type(self.env['wis.notificacion']), '_disparar_cron', lambda s: None), \
                patch.object(Controller, '_wis_en_segundo_plano', lambda s: True):
            return Controller().callback()

    def test_codigo_largo_se_recorta_al_crear(self):
        self.assertGreater(len(self.codigos), 2704)
        log = self.Log.create({'hora': '10:00:00', 'tipo': 'ENTRADA_DEBUG', 'codigo_unico': self.codigos})
        self.assertLessEqual(len(log.codigo_unico.encode()), self.Log.LARGO_MAXIMO_CODIGO)
        self.assertTrue(log.codigo_unico.startswith('W-P-41000, W-P-41001'))
        self.assertRegex(log.codigo_unico, r'\(\+\d+ más\)$')
        guardados = log.codigo_unico.rsplit(' (+', 1)[0].split(', ')
        faltan = int(log.codigo_unico.rsplit('(+', 1)[1].split()[0])
        self.assertEqual(len(guardados) + faltan, 400)

    def test_codigo_largo_se_recorta_al_escribir(self):
        log = self.Log.create({'hora': '10:00:00', 'tipo': 'ENTRADA_DEBUG', 'codigo_unico': 'W-P-1'})
        log.write({'codigo_unico': self.codigos})
        self.assertLessEqual(len(log.codigo_unico.encode()), self.Log.LARGO_MAXIMO_CODIGO)

    def test_codigo_que_no_comprime_se_graba(self):
        """PostgreSQL comprime el valor antes de indexarlo: códigos repetitivos entran igual.
        Con códigos que no comprimen, 400 superan el máximo del índice; recortados, entran."""
        codigos = ', '.join('W-P-' + hashlib.md5(str(i).encode()).hexdigest() for i in range(400))
        log = self.Log.create({'hora': '10:00:00', 'tipo': 'ENTRADA_DEBUG', 'codigo_unico': codigos})
        log.flush_recordset()
        self.assertLessEqual(len(log.codigo_unico.encode()), self.Log.LARGO_MAXIMO_CODIGO)

    def test_codigo_corto_no_cambia(self):
        log = self.Log.create({'hora': '10:00:00', 'tipo': 'ENTRADA_DEBUG',
                               'codigo_unico': 'W-P-1, W-P-2'})
        self.assertEqual(log.codigo_unico, 'W-P-1, W-P-2')

    def test_codigo_sin_comas_se_recorta(self):
        log = self.Log.create({'hora': '10:00:00', 'tipo': 'ENTRADA_DEBUG', 'codigo_unico': 'X' * 5000})
        self.assertLessEqual(len(log.codigo_unico.encode()), self.Log.LARGO_MAXIMO_CODIGO)

    def test_camion_grande_queda_registrado(self):
        """El caso de producción: 400 pedidos en una ejecución quedan en la cola."""
        respuesta = self._llamar(_payload_preparada(990001, 400))
        self.assertEqual(respuesta.get('status'), 200)
        notif = self.env['wis.notificacion'].search(
            [('tipo', '=', 'confirmacionMercaderiaPreparada'), ('numero_interfaz', '=', 990001)])
        self.assertEqual(len(notif), 1)
        self.assertEqual(notif.pedidos_total, 400)
        entrada = self.Log.search([('tipo', '=', 'ENTRADA_DEBUG')], order='id desc', limit=1)
        self.assertIn('W-P-41000', entrada.codigo_unico)

    def test_falla_al_registrar_devuelve_500(self):
        """Si no se puede guardar la notificación, WIS no puede ver un 200."""
        def falla(*a, **k):
            raise ValueError('base caída')

        Notificacion = type(self.env['wis.notificacion'])
        # El rollback del controlador desharía el savepoint del test: se neutraliza.
        with patch.object(Notificacion, '_registrar', falla), \
                patch.object(type(self.env.cr), 'rollback', lambda cr: None):
            with self.assertRaises(HTTPException) as ctx:
                self._llamar(_payload_preparada(990002, 3))
        resp = ctx.exception.get_response()
        self.assertEqual(resp.status_code, 500)
        self.assertIn('base caída', json.loads(resp.get_data())['detail'])

    def test_json_invalido_devuelve_400(self):
        """JSON roto: WIS recibe 400 y el error se graba aparte (`_log_aparte`), así no se pierde
        cuando la petición se deshace. El `assertRaises` de Odoo deshace lo grabado adentro del
        bloque, por eso se miran los registros que pasaron por `_log_aparte`."""
        fake_body = b'{esto no es json'
        fake = SimpleNamespace(env=self.env, httprequest=SimpleNamespace(
            get_data=lambda: fake_body, headers={}, remote_addr='10.0.0.1', method='POST'),
            update_env=lambda **kw: None)
        aparte = []
        with patch.object(wis_controllers, 'request', fake), \
                patch.object(Controller, '_log_aparte', staticmethod(aparte.append)):
            with self.assertRaises(HTTPException) as ctx:
                Controller().callback()
        self.assertEqual(ctx.exception.get_response().status_code, 400)
        self.assertEqual([(v['tipo'], v['estado']) for v in aparte],
                         [('ENTRADA_DEBUG', 'exito'), ('desconocido', 'error')])

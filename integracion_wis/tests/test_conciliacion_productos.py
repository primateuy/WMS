# -*- coding: utf-8 -*-
"""Conciliación de productos con WIS por tandas, incremental de verdad.

Forum, 18-11-2026: la conciliación semanal mandaba los 32.000 productos en una sola corrida.
Tardaba 8-9 minutos y ese día pasó los 10 del cron: Odoo la cortaba, se perdía todo y volvía
a empezar (14 arranques). Además nunca era incremental: 12 productos fallaban siempre y, con
un solo error, la fecha de la última sincronización no se guardaba.

WIS no se llama: `insertarProductosMasivo` se reemplaza por uno que registra lo que recibe.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.integracion_wis.models import conciliacionMaestros as modulo
from odoo.addons.integracion_wis.models.models import WisErrorGeneral


@tagged('post_install', '-at_install', 'wis_conciliacion_productos')
class TestConciliacionProductos(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Config = cls.env['integracion_wis.integracion_wis']
        cls.config = Config._get_config() or Config.create({'company_id': cls.env.company.id})
        cls.config.write({'apiLink': cls.config.apiLink or 'https://wis.invalid', 'chunk_size_productos': 2,
                          'productos_rechazados_wis': False, 'comunicacion_activa': False})
        # La hora de la transacción, sin zona: los productos de abajo quedan «modificados después».
        cls.env.cr.execute("SELECT (now() at time zone 'UTC') - interval '1 second'")
        cls.config.ultima_sync_productos = cls.env.cr.fetchone()[0]
        cls.productos = cls.env['product.product'].create([
            {'name': 'Remera conciliación %s' % i, 'type': 'product', 'integracion_wms': True}
            for i in range(5)])
        for p in cls.productos:
            p.with_context(_avoid_wms=True).codigo_unico = 'PRD-CONC-%s' % p.id
        cls.Conc = cls.env['conciliacion.maestros']

    def setUp(self):
        super().setUp()
        self.lotes = []
        self.rechazar = set()
        self.wis_caido_en = None
        Config = type(self.env['integracion_wis.integracion_wis'])

        def falso(config_self, variantes, chunk_size=None, enviar_barcodes=True):
            if self.wis_caido_en is not None and len(self.lotes) == self.wis_caido_en:
                raise WisErrorGeneral('WIS no atiende (prueba)')
            self.lotes.append(variantes.ids)
            malos = {v.codigo_unico: 'rechazado (prueba)' for v in variantes if v.id in self.rechazar}
            return {'enviados': len(variantes) - len(malos), 'errores': len(malos),
                    'errores_detalle': list(malos.values()), 'errores_por_codigo': malos,
                    'barcodes_enviados': 0}
        parche = patch.object(Config, 'insertarProductosMasivo', falso)
        parche.start()
        self.addCleanup(parche.stop)
        # La comunicación con WIS queda apagada en la base: sólo el cron la ve habilitada.
        habilitada = patch.object(Config, '_comunicacion_habilitada', lambda s: True)
        habilitada.start()
        self.addCleanup(habilitada.stop)
        disparo = patch.object(type(self.Conc), '_disparar_cron_productos', lambda s, en=None: True)
        disparo.start()
        self.addCleanup(disparo.stop)

    def _mandados(self):
        return [pid for lote in self.lotes for pid in lote]

    def _cortar_despues_de(self, n):
        """Se acaba el tiempo cuando se mandaron `n` lotes."""
        return patch.object(modulo, '_sin_tiempo', lambda limite: len(self.lotes) >= n)

    def _en_curso(self):
        return self.Conc._conciliacion_productos_en_curso()

    # ------------------------------------------------------------------
    def test_por_tandas_y_retoma_donde_quedo(self):
        with self._cortar_despues_de(1):
            self.Conc._cron_conciliar_productos()
        conc = self._en_curso()
        self.assertTrue(conc, 'Quedó a medias, para la corrida siguiente.')
        self.assertEqual(len(self.lotes), 1)
        self.assertEqual(json.loads(conc.prod_pendientes), self.productos.ids[2:])

        self.Conc._cron_conciliar_productos()
        self.assertEqual(sorted(self._mandados()), sorted(self.productos.ids), 'Ninguno se repite.')
        self.assertEqual(len(self._mandados()), 5)
        self.assertEqual(conc.estado, 'completado')
        self.assertEqual((conc.prod_total, conc.prod_enviados, conc.prod_errores), (5, 5, 0))
        self.assertFalse(self._en_curso())
        self.assertEqual(self.Conc.search_count([('productos', '=', True),
                                                 ('id', '>=', conc.id)]), 1,
                         'La segunda corrida siguió la misma conciliación.')

    def test_al_menos_un_lote_por_corrida(self):
        with self._cortar_despues_de(0):
            self.Conc._cron_conciliar_productos()
        self.assertEqual(len(self.lotes), 1, 'Aunque no haya margen, la corrida avanza.')

    def test_un_rechazado_no_se_reenvia_hasta_que_lo_corrijan(self):
        """Los 12 de Forum: «ProductoEmpresa: El valor está asignado a otro producto». Mandar
        el mismo dato cada semana no lo arregla; corregido el producto, entra solo."""
        malo = self.productos[3]
        self.rechazar = {malo.id}
        self.Conc._cron_conciliar_productos()
        conc = self.Conc.search([('productos', '=', True)], order='id desc', limit=1)
        self.assertEqual(conc.estado, 'completado_con_errores')
        self.assertEqual(conc.prod_errores, 1)
        self.assertTrue(self.config.ultima_sync_productos >= conc.prod_inicio_sync,
                        'La fecha se guarda aunque haya rechazos.')
        self.assertEqual(json.loads(self.config.productos_rechazados_wis),
                         {str(malo.id): 'rechazado (prueba)'}, 'Queda anotado con el motivo.')

        # Semana siguiente, nadie lo tocó: no se manda nada y sigue rechazado.
        self.lotes, self.rechazar = [], set()
        self.config.ultima_sync_productos = datetime.now() + timedelta(days=1)
        self.Conc._cron_conciliar_productos()
        self.assertEqual(self._mandados(), [], 'El rechazado sin cambios no se reenvía.')
        self.assertEqual(json.loads(self.config.productos_rechazados_wis), {str(malo.id): 'rechazado (prueba)'})

        # Lo corrigen: entra solo en la siguiente y, aceptado, sale de la lista.
        self.config.ultima_sync_productos = datetime.now() - timedelta(days=1)
        otros = self.productos - malo
        self.env.cr.execute("UPDATE product_product SET write_date = %s WHERE id IN %s",
                            [datetime.now() - timedelta(days=2), tuple(otros.ids)])
        malo.write({'name': 'Remera conciliación corregida'})
        self.env.invalidate_all()
        self.Conc._cron_conciliar_productos()
        self.assertEqual(self._mandados(), [malo.id])
        self.assertFalse(self.config.productos_rechazados_wis)

    def test_motivo_corto_de_wis(self):
        detalle = ('PRD-99369: Error en la API: 400 - {\r\n "detail": "[{\\"ItemId\\":1,'
                   '\\"Messages\\":[\\"ProductoEmpresa: El valor está asignado a otro producto.'
                   '\\"]}]"\r\n}')
        self.assertEqual(self.Conc._motivo_corto(detalle),
                         'ProductoEmpresa: El valor está asignado a otro producto.')

    def test_wis_caido_corta_sin_perder_lo_hecho(self):
        self.wis_caido_en = 1
        self.Conc._cron_conciliar_productos()
        conc = self._en_curso()
        self.assertTrue(conc)
        self.assertEqual(conc.prod_enviados, 2, 'El primer lote quedó registrado.')
        self.assertEqual(json.loads(conc.prod_pendientes), self.productos.ids[2:])
        self.wis_caido_en = None
        self.Conc._cron_conciliar_productos()
        self.assertEqual(conc.estado, 'completado')

    def test_el_boton_no_manda_nada_en_la_peticion(self):
        conc = self.Conc.create({'name': 'Manual', 'productos': True, 'clientes': False,
                                 'codigoBarras': False})
        respuesta = conc.conciliarProductos()
        self.assertEqual(self.lotes, [], 'Nada se manda a WIS desde la pantalla.')
        self.assertEqual(respuesta['tag'], 'display_notification')
        self.assertEqual(self._en_curso(), conc)

    def test_una_sola_a_la_vez(self):
        primera = self.Conc.create({'name': 'Primera', 'productos': True})
        primera.conciliarProductos()
        segunda = self.Conc.create({'name': 'Segunda', 'productos': True})
        with self.assertRaises(UserError):
            segunda.conciliarProductos()
        self.Conc._cron_conciliar_productos()
        self.assertEqual(primera.estado, 'completado', 'El cron siguió la que estaba en curso.')
        self.assertEqual(len(self._mandados()), 5)

    def test_proximo_viernes_a_las_0030_de_uruguay(self):
        # Miércoles 18-11-2026 19:00 UTC -> viernes 20-11 00:30 UY = 03:30 UTC.
        self.assertEqual(self.Conc._proximo_viernes_0030(datetime(2026, 11, 18, 19, 0)),
                         datetime(2026, 11, 20, 3, 30))
        # Viernes 00:45 UY (03:45 UTC): ya pasó, el de la semana siguiente.
        self.assertEqual(self.Conc._proximo_viernes_0030(datetime(2026, 11, 20, 3, 45)),
                         datetime(2026, 11, 27, 3, 30))

# -*- coding: utf-8 -*-
"""La cola de WIS bajo carga (prueba de volumen del 27-11-2026): un pedido tardó minutos, Odoo
cortó la corrida a mitad de uno y la notificación quedó «procesando» media hora."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.integracion_wis.models import wis_notificacion as modulo


class _Reloj:
    def __init__(self):
        self.ahora = 1000.0

    def time(self):
        return self.ahora


@tagged('post_install', '-at_install')
class TestColaBajoCarga(TransactionCase):

    def _notificacion(self, pedidos):
        notif, _nueva = self.env['wis.notificacion']._registrar(
            'confirmacionMercaderiaPreparada', 990100,
            {'Id': 'ConfirmacionMercaderiaPreparada', 'NumeroInterfazEjecucion': 990100,
             'ConfirmacionMercaderiaPreparada': {'Pedidos': [{'Pedido': p} for p in pedidos]}})
        return notif

    def test_no_empieza_un_pedido_si_no_queda_el_doble_del_mas_lento(self):
        notif = self._notificacion(['W-P-1', 'W-P-2', 'W-P-3'])
        reloj = _Reloj()
        hechos = []

        def handler(tipo, payload, pedido=None):
            hechos.append(pedido)
            reloj.ahora += 100          # cada pedido tarda 100 s
            return None

        ctrl = SimpleNamespace(_wis_ejecutar_handler=handler)
        limite = reloj.ahora + 250       # alcanza para 2 de 100 s, pero no para 2 × 100 de reserva
        with patch.object(modulo, 'time', reloj), patch.object(modulo, '_cpu_restante', lambda: None):
            terminada = notif._procesar_por_pedido(ctrl, {}, limite)
        self.assertFalse(terminada)
        self.assertEqual(hechos, ['W-P-1'], "Después de un pedido de 100 s quedaban 150 s: no alcanza")
        self.assertEqual(notif.estado, 'pendiente')

    def test_colgada_se_retoma_al_pasar_el_corte_de_odoo(self):
        with patch('odoo.addons.integracion_wis.models.models.corte_odoo', lambda: 600):
            self.assertEqual(modulo._procesando_colgada(), timedelta(seconds=600 + modulo.MARGEN_COLGADA))
        with patch('odoo.addons.integracion_wis.models.models.corte_odoo', lambda: 0):
            self.assertEqual(modulo._procesando_colgada(), modulo.PROCESANDO_COLGADA)

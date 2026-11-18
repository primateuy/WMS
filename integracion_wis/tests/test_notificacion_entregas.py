# -*- coding: utf-8 -*-
"""Entregas a tienda de un despacho de WIS, de a una y retomando donde quedó.

Caso real (producción, 17/18-11-2026): dos «confirmacionPedido» de 163 y 273 pedidos. Los pedidos
se aplicaban bien, uno por transacción, pero después se validaban TODAS las entregas a tienda
(POLO/OUT) en una sola transacción, sin tope de tiempo. Pasaba los 600 s del cron, Odoo cortaba,
se deshacía todo y la notificación volvía a empezar: 43 y 38 intentos en un día, unas 8.000
entregas parciales creadas y deshechas, y el stock de POLO bloqueado 10 minutos cada vez.

Ahora la notificación arma la lista de entregas y las valida de a una, en el mismo cron, con el
mismo tope que los pedidos.
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from odoo.addons.integracion_wis.models import wis_notificacion as modulo_notificacion
from odoo.addons.integracion_wis.models.wis_notificacion import MAX_CORTES_ENTREGA

from .test_webhook_multipedido import WisMultipedidoCase

SIN_LIMITE = 10 ** 12


@tagged('post_install', '-at_install', 'wis_notificaciones')
class TestNotificacionEntregas(WisMultipedidoCase):

    def setUp(self):
        super().setUp()
        self.pickings, self.contenedores = self._cinco_pedidos()
        self.entregas = [self._entrega(t, p) for t, p in zip(self.tiendas, self.pickings)]
        self._preparada(self.pickings, self.contenedores)
        self.avanzadas = []

    # ------------------------------------------------------------------
    # Ayudas
    # ------------------------------------------------------------------
    def _notificacion(self, numero, pickings=None, contenedores=None):
        pickings = pickings or self.pickings
        contenedores = contenedores or self.contenedores
        payload = {'Id': 'ConfirmacionPedido', 'NumeroInterfazEjecucion': numero,
                   'confirmacionPedido': {
                       'fechaCierre': '18/11/2026',
                       'pedidos': [{'pedido': p.codigo_unico, 'codigoOrigen': 'API'} for p in pickings],
                       'contenedores': contenedores}}
        notif, _nueva = self.env['wis.notificacion']._registrar('confirmacionPedido', numero, payload)
        return notif

    def _contar_entregas(self):
        """Registra cada entrega que se intenta avanzar, para ver que ninguna se repite."""
        Picking = type(self.env['stock.picking'])
        original = Picking._wis_avanzar_entrega
        avanzadas = self.avanzadas

        def contar(picking_self, entrega, hechos, origen, registrar_error=True):
            avanzadas.append(entrega.id)
            return original(picking_self, entrega, hechos, origen, registrar_error=registrar_error)
        return patch.object(Picking, '_wis_avanzar_entrega', contar)

    def _reloj_que_corta(self, notif, despues_de):
        """Reloj de la notificación: sin apuro en los pedidos, se acaba el tiempo en las
        entregas después de avanzar `despues_de`."""
        def ahora():
            if notif.fase == 'cadena' and len(self.avanzadas) >= despues_de:
                return SIN_LIMITE + 1
            return 0
        return patch.object(modulo_notificacion, 'time', SimpleNamespace(time=ahora, strftime=time.strftime))

    def _en_transito(self, n):
        Quant = self.env['stock.quant']
        tienda = self.tiendas[n]
        cantidad = 3 if n == 0 else 7
        return (Quant._get_available_quantity(self.producto_a, tienda['transito']) == cantidad
                and Quant._get_available_quantity(self.producto_b, tienda['transito']) == cantidad)

    # ------------------------------------------------------------------
    # Casos
    # ------------------------------------------------------------------
    def test_despacho_avanza_las_entregas_de_a_una(self):
        notif = self._notificacion(990001)
        with self._contar_entregas():
            self.assertTrue(notif._procesar(SIN_LIMITE))

        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual(notif.fase, 'fin')
        self.assertEqual((notif.entregas_total, notif.entregas_hechas), (5, 5))
        self.assertEqual(sorted(self.avanzadas), sorted(e.id for e in self.entregas),
                         "cada entrega se avanza una vez, por separado")
        for n, entrega in enumerate(self.entregas):
            self.assertEqual(entrega.state, 'done', entrega.name)
            self.assertTrue(self._en_transito(n), "lo despachado llegó a la tienda")

    def test_sin_tiempo_sigue_en_la_corrida_siguiente(self):
        """Lo del caso real: la corrida no alcanza para todas las entregas."""
        notif = self._notificacion(990002)
        with self._contar_entregas():
            with self._reloj_que_corta(notif, despues_de=2):
                self.assertFalse(notif._procesar(SIN_LIMITE), "se acabó el tiempo")
            self.assertEqual(notif.estado, 'pendiente')
            self.assertEqual(notif.fase, 'cadena', "no vuelve a la fase de pedidos")
            self.assertEqual(notif.entregas_hechas, 2)
            self.assertEqual(len(json.loads(notif.entregas_pendientes)), 3)
            hechas = [e for e in self.entregas if e.state == 'done']
            self.assertEqual(len(hechas), 2, "lo avanzado queda avanzado")

            self.assertTrue(notif._procesar(SIN_LIMITE))

        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual(notif.entregas_hechas, 5)
        self.assertEqual(len(self.avanzadas), 5, "ninguna entrega se repite")
        self.assertEqual(len(set(self.avanzadas)), 5)
        self.assertTrue(all(e.state == 'done' for e in self.entregas))

    def test_entrega_que_falla_no_frena_a_las_demas(self):
        notif = self._notificacion(990003)
        rota = self.entregas[2]
        Picking = type(self.env['stock.picking'])
        original = Picking._wis_validar_lo_despachado

        def validar(picking_self, lineas, origen_moves):
            if picking_self == rota:
                raise UserError("falla de prueba en una entrega")
            return original(picking_self, lineas, origen_moves)

        with patch.object(Picking, '_wis_validar_lo_despachado', validar):
            self.assertTrue(notif._procesar(SIN_LIMITE))

        self.assertEqual(notif.estado, 'con_errores')
        self.assertIn(rota.name, notif.errores)
        self.assertEqual(notif.entregas_hechas, 4)
        self.assertNotEqual(rota.state, 'done')
        self.assertTrue(all(e.state == 'done' for e in self.entregas if e != rota))
        self.assertTrue(self.env['wms.integracion.log'].search([
            ('picking_id', '=', rota.id), ('nivel', '=', 'error')]))

    def test_entrega_que_corta_la_corrida_se_saltea(self):
        """Una entrega que hace matar al proceso una y otra vez no puede repetir el bucle."""
        notif = self._notificacion(990004)
        with self._contar_entregas(), self._reloj_que_corta(notif, despues_de=0):
            self.assertFalse(notif._procesar(SIN_LIMITE))
        primera = self.env['stock.picking'].browse(json.loads(notif.entregas_pendientes)[0])
        # Así queda cuando la corrida se cortó MAX_CORTES_ENTREGA veces validando esa entrega.
        notif.write({'entrega_en_curso_id': primera.id, 'entrega_cortes': MAX_CORTES_ENTREGA})

        self.assertTrue(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'con_errores')
        self.assertIn('se cortó', notif.errores)
        self.assertNotEqual(primera.state, 'done', "la que cortaba no se intentó otra vez")
        self.assertEqual(notif.entregas_hechas, 4)
        self.assertFalse(notif.entrega_en_curso_id)

    def test_avanzar_entregas_de_una_notificacion_apartada(self):
        """Las 514 y 516 de producción: pedidos aplicados, entregas sin hacer, apartadas a mano."""
        notif = self._notificacion(990005)
        with self._contar_entregas(), self._reloj_que_corta(notif, despues_de=0):
            notif._procesar(SIN_LIMITE)
        notif.write({'estado': 'con_errores', 'fase': 'pedidos', 'entregas_pendientes': '[]'})
        self.assertTrue(notif.despachado_ids)

        notif.action_avanzar_entregas()
        self.assertEqual((notif.estado, notif.fase, notif.entregas_total), ('pendiente', 'cadena', 5))
        self.assertTrue(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertTrue(all(e.state == 'done' for e in self.entregas))

        # Otra vez: no queda nada pendiente y no se toca ninguna entrega.
        notif.action_avanzar_entregas()
        self.assertEqual(notif.entregas_total, 0)
        with self._contar_entregas():
            self.assertTrue(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'hecho')

    def test_despacho_grande_en_varias_corridas(self):
        """40 pedidos y un tope que alcanza para 7 entregas por corrida: termina en varias
        corridas, sin repetir ni saltear ninguna."""
        pickings, contenedores, entregas = [], [], []
        for n in range(40):
            tienda = self.tiendas[n % 5]
            picking = self._preparo(tienda, [(self.producto_a, 1)])
            entregas.append(self._entrega(tienda, picking))
            pickings.append(picking)
            contenedores.append(self._contenedor(f'WISCO-G{n}', 50000 + n, picking.codigo_unico,
                                                 [(self.producto_a, 1)]))
        self._preparada(pickings, contenedores)
        notif = self._notificacion(990008, pickings, contenedores)

        corridas = 0
        with self._contar_entregas():
            while notif.estado != 'hecho' and corridas < 20:
                corridas += 1
                tope = len(self.avanzadas) + 7

                def ahora(tope=tope):
                    if notif.fase == 'cadena' and len(self.avanzadas) >= tope:
                        return SIN_LIMITE + 1
                    return 0
                with patch.object(modulo_notificacion, 'time', SimpleNamespace(time=ahora, strftime=time.strftime)):
                    notif._procesar(SIN_LIMITE)

        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual(corridas, 6, "40 entregas de a 7 por corrida")
        self.assertEqual(notif.entregas_total, 40)
        self.assertEqual(notif.entregas_hechas, 40)
        self.assertEqual(len(self.avanzadas), 40, "ninguna se repite")
        self.assertEqual(len(set(self.avanzadas)), 40)
        self.assertTrue(all(e.state == 'done' for e in entregas))

    # ------------------------------------------------------------------
    # CPU: Odoo no mata el cron, le lanza «CPU time limit exceeded.»
    # ------------------------------------------------------------------
    def test_poca_cpu_corta_la_corrida_sin_error(self):
        notif = self._notificacion(990009)

        def cpu():
            return 1.0 if len(self.avanzadas) >= 2 else 10 ** 6
        with self._contar_entregas():
            with patch.object(modulo_notificacion, '_cpu_restante', cpu):
                self.assertFalse(notif._procesar(SIN_LIMITE))
            self.assertEqual(notif.estado, 'pendiente')
            self.assertFalse(notif.errores, "quedarse sin CPU no es un error")
            self.assertEqual(notif.entregas_hechas, 2)
            self.assertTrue(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual(len(self.avanzadas), 5)

    def test_excepcion_de_cpu_en_una_entrega_es_un_corte(self):
        notif = self._notificacion(990010)
        Picking = type(self.env['stock.picking'])
        original = Picking._wis_validar_lo_despachado
        estado = {'cortada': None}

        def validar(picking_self, lineas, origen_moves):
            if estado['cortada'] is None and picking_self.id != json.loads(notif.entregas_pendientes)[-1]:
                estado['cortada'] = picking_self
                raise Exception('CPU time limit exceeded.')
            return original(picking_self, lineas, origen_moves)

        with patch.object(Picking, '_wis_validar_lo_despachado', validar):
            self.assertFalse(notif._procesar(SIN_LIMITE), "la corrida termina")
        cortada = estado['cortada']
        self.assertEqual(notif.estado, 'pendiente')
        self.assertFalse(notif.errores, "no se registra como error de la entrega")
        self.assertEqual(json.loads(notif.entregas_pendientes)[0], cortada.id, "sigue primera")
        self.assertEqual((notif.entrega_en_curso_id, notif.entrega_cortes), (cortada, 1))
        self.assertFalse(self.env['wms.integracion.log'].search([
            ('picking_id', '=', cortada.id), ('nivel', '=', 'error')]))

        self.assertTrue(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual(cortada.state, 'done')
        self.assertTrue(all(e.state == 'done' for e in self.entregas))

    def test_excepcion_de_cpu_en_un_pedido_lo_deja_en_la_fila(self):
        notif = self._notificacion(990011)
        Ctrl = type(self.controller)
        original = Ctrl._handle_confirmacion_pedido
        estado = {'n': 0}

        def handler(ctrl_self, data):
            estado['n'] += 1
            if estado['n'] == 3:
                raise Exception('CPU time limit exceeded.')
            return original(ctrl_self, data)

        with patch.object(Ctrl, '_handle_confirmacion_pedido', handler):
            self.assertFalse(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'pendiente')
        self.assertEqual(notif.fase, 'pedidos')
        self.assertEqual(notif.pedidos_hechos, 2)
        self.assertEqual(len(json.loads(notif.pendientes)), 3, "el pedido cortado vuelve a la fila")
        self.assertFalse(json.loads(notif.errores or '[]'))

        self.assertTrue(notif._procesar(SIN_LIMITE))
        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual(notif.pedidos_hechos, 5)
        self.assertTrue(all(e.state == 'done' for e in self.entregas))

    def test_avanzar_entregas_sin_despacho_avisa(self):
        notif = self._notificacion(990006)
        with self.assertRaises(UserError):
            notif.action_avanzar_entregas()

    def test_caja_compartida_entre_entregas_de_a_una(self):
        """WIS arma una caja por tienda con varios pedidos: cada entrega va en su transacción
        y la caja se mueve una sola vez."""
        tienda = self.tiendas[4]
        p1 = self._preparo(tienda, [(self.producto_a, 2)])
        p2 = self._preparo(tienda, [(self.producto_b, 3)])
        e1 = self._entrega(tienda, p1)
        e2 = self._entrega(tienda, p2)
        caja = {
            'numeroContenedor': 195, 'idExternoContenedor': '195', 'codigoBarras': 'WISCO-COMP-COLA',
            'cantidadBultos': 1,
            'detalles': [{'pedido': p1.codigo_unico, 'producto': self.producto_a.codigo_unico, 'cantidadPreparada': 2},
                         {'pedido': p2.codigo_unico, 'producto': self.producto_b.codigo_unico, 'cantidadPreparada': 3}],
        }
        self._preparada([p1, p2], [caja])
        antes_a = self.env['stock.quant']._get_available_quantity(self.producto_a, tienda['transito'])
        antes_b = self.env['stock.quant']._get_available_quantity(self.producto_b, tienda['transito'])

        notif = self._notificacion(990007, [p1, p2], [caja])
        self.assertTrue(notif._procesar(SIN_LIMITE))

        self.assertEqual(notif.estado, 'hecho', notif.errores)
        self.assertEqual((e1.state, e2.state), ('done', 'done'))
        Quant = self.env['stock.quant']
        self.assertEqual(Quant._get_available_quantity(self.producto_a, tienda['transito']) - antes_a, 2)
        self.assertEqual(Quant._get_available_quantity(self.producto_b, tienda['transito']) - antes_b, 3)

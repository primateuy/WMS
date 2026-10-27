# -*- coding: utf-8 -*-
"""«Integrar ahora» desde la orden de compra cuando WIS no contesta.

Support, 27-10-2026: 43 variantes, WIS de pruebas no respondió en 60 s y el
WisTimeout subía tal cual. El funcional veía el traceback, la orden no se
confirmaba y las variantes no quedaban en ninguna cola. Ahora van a la cola
urgente y la orden se confirma, igual que cuando superan el umbral.
No golpean la red: la llamada HTTP a WIS se reemplaza por un timeout.
"""
import datetime
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.wis_sync_queue import PRIORIDAD_URGENTE

REQUESTS = 'odoo.addons.integracion_wis.models.models.requests.request'


@tagged('post_install', '-at_install', 'wis_productos')
class TestWizardIntegrarSinRespuesta(TransactionCase):

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
        # Token vigente: así la única llamada a WIS es la del alta de productos.
        cls.config.write({
            'comunicacion_activa': True, 'exigir_integracion_oc': False,
            'bloquear_oc_sin_integrar': False, 'verificar_barcode_existente': False,
            'pausa_hasta': False, 'pausa_fallos': 0, 'token': 'token-de-prueba',
            'expiracionToken': datetime.datetime.now() + datetime.timedelta(hours=1),
        })
        cls.picking_type = cls.env['stock.picking.type'].search([
            ('code', '=', 'incoming'), ('company_id', '=', cls.env.company.id)], limit=1)
        cls.picking_type.integracion_wms = True
        cls.proveedor = cls.env['res.partner'].create({'name': 'Proveedor WIS sin respuesta'})
        cls.variantes = cls.env['product.product'].create([
            {'name': f'Sin respuesta {i}', 'type': 'product'} for i in range(3)])
        cls.variantes.with_context(_avoid_wms=True).write({'integracion_wms': True})

    def _orden_y_asistente(self):
        orden = self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'picking_type_id': self.picking_type.id,
            'order_line': [(0, 0, {
                'product_id': v.id, 'name': v.name, 'product_qty': 1, 'price_unit': 10,
                'date_planned': '2026-01-01 00:00:00',
            }) for v in self.variantes],
        })
        asistente = self.env['wis.integrar.variantes.wizard'].create({
            'order_id': orden.id, 'variante_ids': [(6, 0, self.variantes.ids)]})
        return orden, asistente

    def test_wis_no_responde_encola_y_confirma(self):
        orden, asistente = self._orden_y_asistente()
        timeout = requests.exceptions.ReadTimeout('Read timed out. (read timeout=60)')
        with patch(REQUESTS, side_effect=timeout) as llamada:
            asistente.action_integrar_ahora()

        self.assertEqual(llamada.call_count, 1, 'un solo intento: no se reintenta de a uno')
        self.assertEqual(orden.state, 'purchase')
        self.assertFalse(any(self.variantes.mapped('codigo_unico')))
        cola = self.env['wis.sync.queue'].search([
            ('product_id', 'in', self.variantes.ids), ('estado', '=', 'pendiente')])
        self.assertEqual(cola.product_id, self.variantes)
        self.assertEqual(set(cola.mapped('prioridad')), {PRIORIDAD_URGENTE})
        self.assertTrue(any('WIS no respondió' in (m.body or '') for m in orden.message_ids))

    def test_rechazo_de_datos_sigue_avisando_y_no_confirma(self):
        """Un producto que WIS rechaza no es «WIS no responde»: se avisa como siempre."""
        orden, asistente = self._orden_y_asistente()
        rechazo = {'enviados': 0, 'errores': 3, 'barcodes_enviados': 0,
                   'errores_detalle': ['PRD-X: 400 - rechazado'], 'errores_por_codigo': {}}
        with patch.object(type(self.env['product.product']), '_enviar_wms_en_lote',
                          return_value=rechazo):
            with self.assertRaises(UserError):
                asistente.action_integrar_ahora()
        self.assertIn(orden.state, ('draft', 'sent'))

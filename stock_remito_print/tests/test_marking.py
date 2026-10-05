# -*- coding: utf-8 -*-
from odoo.tests.common import tagged

from .common import RemitoPrintCommon


@tagged('post_install', '-at_install')
class TestMarking(RemitoPrintCommon):
    """Marcado automático y manual de operaciones impresas."""

    def test_registro_de_impresion(self):
        self.assertFalse(self.picking_a.remito_printed)
        self.assertEqual(self.picking_a.remito_print_count, 0)

        self.picking_a._remito_register_print('standard')

        self.assertTrue(self.picking_a.remito_printed)
        self.assertEqual(self.picking_a.remito_print_count, 1)
        self.assertEqual(self.picking_a.remito_last_source, 'standard')
        self.assertEqual(self.picking_a.remito_printed_by, self.env.user)
        self.assertTrue(self.picking_a.remito_printed_date)

    def test_la_cantidad_se_acumula(self):
        self.picking_a._remito_register_print('standard')
        self.picking_a._remito_register_print('ucfe')

        self.assertEqual(self.picking_a.remito_print_count, 2)
        self.assertEqual(self.picking_a.remito_last_source, 'ucfe')

    def test_marcar_manualmente(self):
        mensajes_antes = len(self.picking_a.message_ids)

        self.picking_a.action_remito_mark_printed()

        self.assertTrue(self.picking_a.remito_printed)
        self.assertEqual(self.picking_a.remito_print_count, 1)
        # El marcado manual no inventa un origen de PDF.
        self.assertFalse(self.picking_a.remito_last_source)
        self.assertGreater(len(self.picking_a.message_ids), mensajes_antes)

    def test_desmarcar_manualmente(self):
        self.picking_a._remito_register_print('ucfe')
        mensajes_antes = len(self.picking_a.message_ids)

        self.picking_a.action_remito_mark_unprinted()

        self.assertFalse(self.picking_a.remito_printed)
        self.assertFalse(self.picking_a.remito_printed_date)
        self.assertFalse(self.picking_a.remito_printed_by)
        # El historial de lo que sí se imprimió se conserva.
        self.assertEqual(self.picking_a.remito_print_count, 1)
        self.assertEqual(self.picking_a.remito_last_source, 'ucfe')
        self.assertGreater(len(self.picking_a.message_ids), mensajes_antes)

    def test_marcado_en_lote(self):
        pickings = self.picking_a | self.picking_b
        pickings.action_remito_mark_printed()
        self.assertTrue(all(pickings.mapped('remito_printed')))

    def test_el_autor_del_mensaje_es_el_usuario(self):
        """El marcado escribe con sudo pero el chatter no dice OdooBot."""
        usuario = self._crear_usuario(
            'remito.marcador', self.env.ref('stock.group_stock_user'))
        self.picking_a.with_user(usuario).action_remito_mark_printed()

        mensaje = self.picking_a.message_ids[0]
        self.assertEqual(mensaje.author_id, usuario.partner_id)

# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .common import RemitoPrintCommon


@tagged('post_install', '-at_install')
class TestRecordRules(RemitoPrintCommon):
    """Restricción de tipos de operación por usuario."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.grupo_stock = cls.env.ref('stock.group_stock_user')
        cls.usuario_libre = cls._crear_usuario(
            'remito.libre', cls.grupo_stock)
        cls.usuario_restringido = cls._crear_usuario(
            'remito.restringido', cls.grupo_stock,
            tipos_permitidos=cls.picking_type_a)

    def test_usuario_sin_restriccion_ve_todo(self):
        visibles = self.env['stock.picking'].with_user(
            self.usuario_libre).search([('id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(len(visibles), 2)

    def test_usuario_restringido_ve_solo_sus_tipos(self):
        visibles = self.env['stock.picking'].with_user(
            self.usuario_restringido).search(
            [('id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(visibles, self.picking_a)

    def test_usuario_restringido_ve_solo_sus_tipos_de_operacion(self):
        tipos = self.env['stock.picking.type'].with_user(
            self.usuario_restringido).search(
            [('id', 'in', (self.picking_type_a | self.picking_type_b).ids)])
        self.assertEqual(tipos, self.picking_type_a)

    def test_usuario_restringido_ve_solo_sus_movimientos(self):
        movimientos = self.env['stock.move'].with_user(
            self.usuario_restringido).search(
            [('picking_id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(movimientos.picking_id, self.picking_a)

    def test_movimientos_sin_operacion_siguen_visibles(self):
        """Valorización, producción y ajustes usan movimientos sin picking."""
        movimiento = self.env['stock.move'].create({
            'name': 'Movimiento suelto',
            'product_id': self.product.id,
            'product_uom_qty': 1,
            'product_uom': self.product.uom_id.id,
            'location_id': self.location_src.id,
            'location_dest_id': self.location_dest.id,
        })
        visibles = self.env['stock.move'].with_user(
            self.usuario_restringido).search([('id', '=', movimiento.id)])
        self.assertEqual(visibles, movimiento)

    def test_restriccion_sin_tipos_es_invalida(self):
        with self.assertRaises(ValidationError):
            self.usuario_libre.write({'restrict_picking_types': True})

    def test_cambiar_la_restriccion_tiene_efecto_inmediato(self):
        """Sin invalidar la caché de reglas el usuario seguiría viendo lo de antes."""
        visibles = self.env['stock.picking'].with_user(
            self.usuario_restringido).search(
            [('id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(visibles, self.picking_a)

        self.usuario_restringido.allowed_picking_type_ids = [
            (6, 0, (self.picking_type_a | self.picking_type_b).ids)]

        visibles = self.env['stock.picking'].with_user(
            self.usuario_restringido).search(
            [('id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(len(visibles), 2)

    def test_superusuario_no_queda_restringido(self):
        """Los procesos internos corren en sudo y no los toca la regla."""
        self.usuario_restringido.sudo()  # no cambia nada, solo documenta
        visibles = self.env['stock.picking'].sudo().search(
            [('id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(len(visibles), 2)

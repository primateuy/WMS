# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'crossdock_multiplos')
class TestMultiplosDistribucion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        atributo = cls.env['product.attribute'].create({
            'name': 'Talle múltiplos test', 'create_variant': 'always',
            'value_ids': [(0, 0, {'name': n}) for n in ('S', 'M', 'L')]})
        cls.plantilla = cls.env['product.template'].create({
            'name': 'Remera múltiplos test', 'type': 'product',
            'attribute_line_ids': [(0, 0, {'attribute_id': atributo.id,
                                           'value_ids': [(6, 0, atributo.value_ids.ids)]})]})
        cls.variantes = cls.plantilla.product_variant_ids

    def test_la_plantilla_propaga_siempre_salvo_valor_propio(self):
        """🔴 Antes, desde el segundo cambio la plantilla dejaba de propagar: propagar
        marcaba a cada variante como «valor propio»."""
        propia = self.variantes[0]
        propia.mutiplos_distribucion = 4
        self.assertTrue(propia.mutiplos_distribucion_override)

        self.plantilla.mutiplos_distribucion = 6
        resto = self.variantes - propia
        self.assertEqual(set(resto.mapped('mutiplos_distribucion')), {6})
        self.assertFalse(any(resto.mapped('mutiplos_distribucion_override')))
        self.assertEqual(propia.mutiplos_distribucion, 4)

        self.plantilla.mutiplos_distribucion = 12
        self.assertEqual(set(resto.mapped('mutiplos_distribucion')), {12},
                         "el segundo cambio también llega")
        self.assertEqual(propia.mutiplos_distribucion, 4)

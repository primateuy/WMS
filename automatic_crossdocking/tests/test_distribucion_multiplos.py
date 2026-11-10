# -*- coding: utf-8 -*-
"""Reparto del cupo de Crossdocking en MÚLTIPLOS (ticket 5227).

`_apply_distribution_with_limit_control` convierte la distribución objetivo (pareja
o ponderada por capacidad) a cantidades en múltiplos enteros, sin pasar el cupo, con
la regla de mayor-resto / primeras-N y alerta cuando el cupo no alcanza.

El método no toca `self`, así que se prueba sobre el recordset vacío de purchase.order
con un `line` liviano (SimpleNamespace) que solo aporta `distribution_multiple`.
"""
from types import SimpleNamespace

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'crossdock_multiplos')
class TestDistribucionMultiplos(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.PO = cls.env['purchase.order']

    def _dists(self, targets):
        return [{'location': i, 'warehouse': i, 'quantity': q} for i, q in enumerate(targets)]

    def _run(self, targets, cupo, multiple):
        line = SimpleNamespace(distribution_multiple=multiple)
        return self.PO._apply_distribution_with_limit_control(line, self._dists(targets), cupo, 'nearest')

    def test_ejemplo_ticket_280_12_45pct(self):
        # 280 compradas, 45% -> cupo 126, múltiplo 12, 16 sucursales parejas.
        cupo = 126
        finals, central, warnings = self._run([cupo / 16.0] * 16, cupo, 12)
        qtys = [d['quantity'] for d in finals]
        self.assertEqual(len(finals), 10, "deben quedar 10 sucursales con mercadería")
        self.assertTrue(all(q == 12 for q in qtys), "cada una con un múltiplo de 12")
        self.assertEqual(sum(qtys), 120, "se distribuyen 120, quedan 6 sin asignar")
        self.assertEqual(len(warnings), 1)
        self.assertIn("10 de las 16", warnings[0])

    def test_cupo_suficiente_sin_alerta(self):
        finals, central, warnings = self._run([120, 120], 240, 12)
        qtys = sorted(d['quantity'] for d in finals)
        self.assertEqual(qtys, [120, 120])
        self.assertEqual(warnings, [])

    def test_multiplo_1_passthrough(self):
        dists = self._dists([7, 3, 1])
        finals, central, warnings = self.PO._apply_distribution_with_limit_control(
            SimpleNamespace(distribution_multiple=1), dists, 11, 'nearest')
        self.assertEqual(finals, dists, "con múltiplo 1 no se toca la distribución")
        self.assertEqual(central, 0)
        self.assertEqual(warnings, [])

    def test_ponderado_por_capacidad_respeta_proporcion(self):
        # objetivos desparejos (capacidad): 100 vs 20, cupo 120, múltiplo 12.
        finals, central, warnings = self._run([100, 20], 120, 12)
        by_loc = {d['location']: d['quantity'] for d in finals}
        self.assertEqual(by_loc.get(0), 96, "la de mayor capacidad recibe 8 packs")
        self.assertEqual(by_loc.get(1), 24, "la de menor recibe 2 packs (mayor resto)")
        self.assertEqual(sum(by_loc.values()), 120)
        self.assertEqual(warnings, [])

    def test_shortfall_primeras_n_en_orden(self):
        # cupo 24, múltiplo 12, 3 sucursales parejas -> solo 2 packs: primeras 2.
        finals, central, warnings = self._run([8, 8, 8], 24, 12)
        locs = sorted(d['location'] for d in finals)
        self.assertEqual(locs, [0, 1], "las primeras dos en orden")
        self.assertTrue(all(d['quantity'] == 12 for d in finals))
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 de las 3", warnings[0])

    def test_nunca_supera_el_cupo(self):
        finals, central, warnings = self._run([50, 50, 50], 100, 12)
        self.assertLessEqual(sum(d['quantity'] for d in finals), 100)

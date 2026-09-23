# -*- coding: utf-8 -*-
"""Conciliación de stock por fases y tandas.

Lo que más se cuida acá: que **el silencio de WIS no se confunda con un cero**.
Tomarlo como cero pondría en cero productos que sólo no se pudieron consultar,
y eso termina en movimientos de inventario con valuación y asientos.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

MODELO_API = 'odoo.addons.integracion_wis.models.models.IntegracionWIS'


@tagged('post_install', '-at_install', 'wis_conciliacion_stock')
class TestConciliacionStockFases(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ubicacion = cls.env['stock.location'].create({
            'name': 'WIS Reposición prueba',
            'usage': 'internal',
            'location_id': cls.env.ref('stock.stock_location_locations').id,
        })
        cls.config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        if not cls.config:
            cls.config = cls.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test', 'client_secret': 'test', 'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api',
                'company_id': cls.env.company.id, 'comunicacion_activa': True,
            })
        cls.config.write({'comunicacion_activa': True,
                          'ubicacionReponerStock': cls.ubicacion.id,
                          'diferenciaMinima': 1})

        # 🔴 La fase 0 toma TODAS las variantes integradas de la base, y en
        # `o17_support_forum` hay 1.963 reales: sin aislar, la tanda procesa
        # ésas y no las del fixture. Se desmarcan acá (la transacción del test
        # se revierte, no queda nada).
        cls.env['product.product'].search(
            [('integracion_wms', '=', True)]
        ).with_context(_avoid_wms=True).write({'integracion_wms': False})

        # Tres variantes integradas, con stock conocido en la ubicación.
        cls.variantes = cls.env['product.product']
        for i, cantidad in enumerate((10, 5, 0)):
            v = cls.env['product.product'].with_context(_avoid_wms=True).create({
                'name': 'Producto conciliación %d' % i,
                'type': 'product',
                'integracion_wms': True,
                'codigo_unico': 'PRD-TEST-%d' % i,
            })
            if cantidad:
                cls.env['stock.quant'].with_context(inventory_mode=True).create({
                    'product_id': v.id, 'location_id': cls.ubicacion.id,
                    'inventory_quantity': cantidad,
                })._apply_inventory()
            cls.variantes |= v

    def _nueva(self):
        return self.env['conciliacion.stock'].create({'name': 'Prueba fases'})

    def _filas(self, conc):
        self.env.cr.execute(
            "SELECT product_id, cantidad_odoo, cantidad_wis, diferencia, estado "
            "FROM %s ORDER BY row_num" % conc._cs_tabla())
        return {f[0]: f[1:] for f in self.env.cr.fetchall()}

    # --- fase 0 -------------------------------------------------------
    def test_armar_tabla_toma_las_integradas_con_codigo(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        filas = self._filas(conc)
        for v in self.variantes:
            self.assertIn(v.id, filas)
        self.assertEqual(conc.fase, 'tabla')

    def test_armar_tabla_trae_el_stock_de_la_ubicacion(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        filas = self._filas(conc)
        self.assertEqual(float(filas[self.variantes[0].id][0]), 10.0)
        self.assertEqual(float(filas[self.variantes[2].id][0]), 0.0)

    def test_sin_ubicacion_de_reposicion_no_arranca(self):
        self.config.ubicacionReponerStock = False
        conc = self._nueva()
        with self.assertRaises(UserError):
            conc.action_armar_tabla()
        self.config.ubicacionReponerStock = self.ubicacion.id

    # --- fase 1 -------------------------------------------------------
    def test_la_consulta_escribe_cantidad_y_diferencia(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        respuestas = {'PRD-TEST-0': 7.0, 'PRD-TEST-1': 5.0, 'PRD-TEST-2': 3.0}
        with patch(f'{MODELO_API}.consultaStockCodigo',
                   lambda self, codigo: respuestas[codigo]):
            self.assertEqual(conc._cs_consultar_tanda(), 0)
        filas = self._filas(conc)
        self.assertEqual(float(filas[self.variantes[0].id][1]), 7.0)
        self.assertEqual(float(filas[self.variantes[0].id][2]), -3.0)   # 7 - 10
        self.assertEqual(filas[self.variantes[1].id][3], 'ok')

    def test_sin_respuesta_no_es_cero(self):
        """🔴 El invariante que importa: si WIS no contesta, la fila queda
        marcada y NO se toma como stock cero."""
        conc = self._nueva()
        conc.action_armar_tabla()

        def caido(self, codigo):
            raise ValueError("WIS no responde")

        with patch(f'{MODELO_API}.consultaStockCodigo', caido):
            conc._cs_consultar_tanda()

        for fila in self._filas(conc).values():
            self.assertEqual(fila[3], 'sin_respuesta')
            self.assertIsNone(fila[1], "nunca puede quedar una cantidad asumida")
            self.assertIsNone(fila[2])
        self.assertEqual(conc.cs_errors, 3)

    def test_cantidad_none_tampoco_es_cero(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        with patch(f'{MODELO_API}.consultaStockCodigo', lambda self, codigo: None):
            conc._cs_consultar_tanda()
        for fila in self._filas(conc).values():
            self.assertEqual(fila[3], 'sin_respuesta')
        self.assertEqual(conc.cs_errors, 3)

    def test_el_cero_explicito_de_wis_si_cuenta(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        with patch(f'{MODELO_API}.consultaStockCodigo', lambda self, codigo: 0):
            conc._cs_consultar_tanda()
        filas = self._filas(conc)
        self.assertEqual(filas[self.variantes[0].id][3], 'ok')
        self.assertEqual(float(filas[self.variantes[0].id][1]), 0.0)
        self.assertEqual(float(filas[self.variantes[0].id][2]), -10.0)
        self.assertEqual(conc._cs_resumen()['a_cero'], 2)   # los que tenían stock

    def test_procesa_por_tandas(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.cs_batch_size = 2
        with patch(f'{MODELO_API}.consultaStockCodigo', lambda self, codigo: 1):
            quedan = conc._cs_consultar_tanda()
            self.assertEqual(quedan, 2, "la tanda se llenó: puede quedar más")
            self.assertEqual(conc.cs_done, 2)
            self.assertEqual(conc._cs_consultar_tanda(), 0)
        self.assertEqual(conc.cs_done, 3)

    def test_el_resumen_cuenta_las_diferencias(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        respuestas = {'PRD-TEST-0': 10.0, 'PRD-TEST-1': 99.0, 'PRD-TEST-2': 0.0}
        with patch(f'{MODELO_API}.consultaStockCodigo',
                   lambda self, codigo: respuestas[codigo]):
            conc._cs_consultar_tanda()
        resumen = conc._cs_resumen()
        self.assertEqual(resumen['consultadas'], 3)
        self.assertEqual(resumen['con_diferencia'], 1)   # sólo el de 5 -> 99
        self.assertEqual(resumen['sin_respuesta'], 0)

    def test_cancelar_deja_la_tabla_para_reanudar(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.write({'fase': 'consultando'})
        conc.action_cancelar_consulta()
        self.assertTrue(conc.cs_cancel_requested)
        self.assertTrue(conc._cs_tabla_existe())

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

    def _listado(self, stock_por_codigo, tam=10):
        """Finge `/ConsultaDeStock/GetData`: páginas de `tam` filas."""
        codigos = sorted(stock_por_codigo)

        def paginado(cfg, pagina, filtros=None):
            desde = (pagina - 1) * tam
            return [{'producto': c,
                     'stockGeneral': stock_por_codigo[c],
                     'stockDisponible': stock_por_codigo[c]}
                    for c in codigos[desde:desde + tam]]
        return paginado

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
        with patch(f'{MODELO_API}.consultaStockPaginado', self._listado(respuestas)):
            while conc._cs_consultar_tanda():
                pass
        filas = self._filas(conc)
        self.assertEqual(float(filas[self.variantes[0].id][1]), 7.0)
        self.assertEqual(float(filas[self.variantes[0].id][2]), -3.0)   # 7 - 10
        self.assertEqual(filas[self.variantes[1].id][3], 'ok')

    def test_lo_que_wis_no_lista_no_es_cero(self):
        """🔴 El invariante que importa: lo que el listado no trae queda
        marcado y NO se toma como stock cero."""
        conc = self._nueva()
        conc.action_armar_tabla()
        # WIS sólo informa una de las tres.
        with patch(f'{MODELO_API}.consultaStockPaginado',
                   self._listado({'PRD-TEST-1': 5.0})):
            while conc._cs_consultar_tanda():
                pass

        filas = self._filas(conc)
        for v in (self.variantes[0], self.variantes[2]):
            self.assertEqual(filas[v.id][3], 'sin_respuesta')
            self.assertIsNone(filas[v.id][1], "nunca puede quedar una cantidad asumida")
            self.assertIsNone(filas[v.id][2])
        self.assertEqual(conc.cs_errors, 2)

    def test_si_el_listado_falla_no_marca_a_nadie_como_faltante(self):
        """Una página caída no puede dar por terminado el listado: eso dejaría
        como «no informadas» a todas las que faltaban leer."""
        conc = self._nueva()
        conc.action_armar_tabla()

        def caido(cfg, pagina, filtros=None):
            raise ValueError("WIS no responde")

        with patch(f'{MODELO_API}.consultaStockPaginado', caido):
            with self.assertRaises(ValueError):
                conc._cs_consultar_tanda()

        for fila in self._filas(conc).values():
            self.assertEqual(fila[3], 'pendiente')

    def test_listado_vacio_deja_todo_sin_respuesta(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        with patch(f'{MODELO_API}.consultaStockPaginado', self._listado({})):
            while conc._cs_consultar_tanda():
                pass
        for fila in self._filas(conc).values():
            self.assertEqual(fila[3], 'sin_respuesta')
            self.assertIsNone(fila[1])
        self.assertEqual(conc.cs_errors, 3)

    def test_el_cero_explicito_de_wis_si_cuenta(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        with patch(f'{MODELO_API}.consultaStockPaginado',
                   self._listado({'PRD-TEST-0': 0.0, 'PRD-TEST-1': 0.0, 'PRD-TEST-2': 0.0})):
            while conc._cs_consultar_tanda():
                pass
        filas = self._filas(conc)
        self.assertEqual(filas[self.variantes[0].id][3], 'ok')
        self.assertEqual(float(filas[self.variantes[0].id][1]), 0.0)
        self.assertEqual(float(filas[self.variantes[0].id][2]), -10.0)
        self.assertEqual(conc._cs_resumen()['a_cero'], 2)   # los que tenían stock

    def test_procesa_por_tandas(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.cs_batch_size = 2
        respuestas = {'PRD-TEST-0': 1.0, 'PRD-TEST-1': 1.0, 'PRD-TEST-2': 1.0}
        with patch(f'{MODELO_API}.consultaStockPaginado',
                   self._listado(respuestas, tam=2)):
            quedan = conc._cs_consultar_tanda()
            self.assertEqual(quedan, 2, "la tanda se llenó: puede quedar más")
            self.assertEqual(conc.cs_done, 2)
            self.assertEqual(conc._cs_consultar_tanda(), 0)
        self.assertEqual(conc.cs_done, 3)

    def test_el_resumen_cuenta_las_diferencias(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        respuestas = {'PRD-TEST-0': 10.0, 'PRD-TEST-1': 99.0, 'PRD-TEST-2': 0.0}
        with patch(f'{MODELO_API}.consultaStockPaginado', self._listado(respuestas)):
            while conc._cs_consultar_tanda():
                pass
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

    # --- fase 2: la costura con el motor de ajuste ---------------------
    def _consultar_todo(self, conc, respuestas):
        with patch(f'{MODELO_API}.consultaStockPaginado', self._listado(respuestas)):
            while conc._cs_consultar_tanda():
                pass
        conc.write({'fase': 'consultado'})

    def test_genera_el_ajuste_con_las_diferencias(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        # 10 -> 7 (diferencia), 5 -> 5 (sin diferencia), 0 -> 4 (diferencia)
        self._consultar_todo(conc, {'PRD-TEST-0': 7.0, 'PRD-TEST-1': 5.0,
                                    'PRD-TEST-2': 4.0})
        conc.action_generar_ajuste()

        self.assertEqual(conc.fase, 'ajuste')
        batch = self.env['forum.import.batch'].browse(conc.cs_batch_id)
        self.assertEqual(batch.state, 'ready')
        self.env.cr.execute("SELECT product_id, cantidad FROM %s ORDER BY row_num"
                            % batch._staging_name())
        celdas = dict(self.env.cr.fetchall())
        self.assertEqual(set(celdas), {self.variantes[0].id, self.variantes[2].id},
                         "sólo las que tienen diferencia")
        self.assertEqual(float(celdas[self.variantes[0].id]), 7.0,
                         "va el stock que debe quedar, no la diferencia")

    def test_las_sin_respuesta_no_llegan_al_ajuste(self):
        """🔴 El invariante, ahora del otro lado de la costura."""
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar_todo(conc, {'PRD-TEST-0': 7.0})   # las otras dan None
        conc.action_generar_ajuste()

        batch = self.env['forum.import.batch'].browse(conc.cs_batch_id)
        self.env.cr.execute("SELECT product_id FROM %s" % batch._staging_name())
        productos = {f[0] for f in self.env.cr.fetchall()}
        self.assertEqual(productos, {self.variantes[0].id})

    def test_el_tope_de_variantes_a_cero_frena(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.cs_tope_a_cero_pct = 10
        self._consultar_todo(conc, {'PRD-TEST-0': 0.0, 'PRD-TEST-1': 0.0,
                                    'PRD-TEST-2': 0.0})
        with self.assertRaises(UserError):
            conc.action_generar_ajuste()
        self.assertFalse(conc.cs_batch_id)

    def test_sin_diferencias_no_genera_nada(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar_todo(conc, {'PRD-TEST-0': 10.0, 'PRD-TEST-1': 5.0,
                                    'PRD-TEST-2': 0.0})
        with self.assertRaises(UserError):
            conc.action_generar_ajuste()

    # --- las fases del motor, desde esta pantalla ----------------------
    def test_las_acciones_del_motor_se_disparan_desde_aca(self):
        """Lo pedido: manejar todo el proceso desde Conciliación de Stock, sin
        saltar al batch. Son delegaciones: la lógica sigue siendo del motor."""
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar_todo(conc, {'PRD-TEST-0': 7.0})
        conc.action_generar_ajuste()

        conc.invalidate_recordset(['cs_batch_estado'])
        self.assertEqual(conc.cs_batch_estado, 'ready')

        llamadas = []
        Batch = type(self.env['forum.import.batch'])
        with patch.object(Batch, 'action_aplicar_ajuste',
                          lambda self: llamadas.append(('aplicar', self.id))), \
             patch.object(Batch, 'action_publicar_asientos',
                          lambda self: llamadas.append(('publicar', self.id))), \
             patch.object(Batch, 'action_conciliar',
                          lambda self: llamadas.append(('conciliar', self.id))):
            conc.action_aplicar_ajuste()
            conc.action_publicar_asientos()
            conc.action_conciliar_asientos()

        self.assertEqual([c[0] for c in llamadas], ['aplicar', 'publicar', 'conciliar'])
        self.assertEqual({c[1] for c in llamadas}, {conc.cs_batch_id})

    def test_sin_ajuste_generado_las_acciones_avisan(self):
        conc = self._nueva()
        with self.assertRaises(UserError):
            conc.action_aplicar_ajuste()

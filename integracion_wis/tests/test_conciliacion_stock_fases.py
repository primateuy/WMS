# -*- coding: utf-8 -*-
"""Conciliación de stock por fases y tandas.

Lo que más se cuida acá: que **el silencio de WIS no se confunda con un cero**.
Tomarlo como cero pondría en cero productos que sólo no se pudieron consultar,
y eso termina en movimientos de inventario con valuación y asientos.
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

from odoo.addons.integracion_wis.models.models import ErrorConsultaStockWIS

MODULO_API = 'odoo.addons.integracion_wis.models.models'
MODELO_API = MODULO_API + '.IntegracionWIS'
# Lo que contesta el WIS real (medido contra el de pruebas el 28-09-2026).
SIN_STOCK = {'title': 'Errores en la consulta', 'status': 400,
             'detail': '[{"ItemId":1,"Messages":["No se encontró stock para los filtros enviados."]}]'}
NO_EXISTE = {'title': 'Errores en la consulta', 'status': 400,
             'detail': '[{"ItemId":1,"Messages":["Producto: WMSAPI_msg_Error_ProductoNoExiste"]}]'}


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
        """Finge `/ConsultaDeStock/GetData`: páginas de `tam` filas.

        Pasado el final devuelve lista vacía, que es como `consultaStockPaginado`
        traduce el 400 «No se encontró stock» de WIS.
        """
        codigos = sorted(stock_por_codigo)

        def paginado(cfg, pagina, filtros=None, traza=None):
            desde = (pagina - 1) * tam
            filas = [{'producto': c,
                      'stockGeneral': stock_por_codigo[c],
                      'stockDisponible': stock_por_codigo[c]}
                     for c in codigos[desde:desde + tam]]
            if traza is not None:
                traza.update({'endpoint': '/ConsultaDeStock/GetData', 'pagina': pagina,
                              'body': {'pagina': pagina, 'filtros': filtros or {}},
                              'status': 200 if filas else 400, 'ms': 1, 'filas': filas,
                              'clase': 'ok' if filas else 'fin'})
            return filas
        return paginado

    def _por_codigo(self, respuestas=None):
        """Finge la consulta filtrada por código. Por defecto: «no existe»."""
        respuestas = respuestas or {}

        def consulta(cfg, codigo, traza=None):
            r = respuestas.get(codigo, ('no_existe', None))
            if traza is not None:
                traza.update({'endpoint': '/ConsultaDeStock/GetData', 'pagina': 1,
                              'producto': codigo, 'body': {'filtros': {'producto': codigo}},
                              'status': 400, 'ms': 1, 'filas': [],
                              'clase': {'sin_stock': 'fin'}.get(r[0], r[0])})
            if isinstance(r, Exception):
                raise r
            return r
        return consulta

    def _wis(self, stock_por_codigo, tam=10, por_codigo=None):
        """Los dos caminos de consulta fingidos a la vez."""
        from contextlib import ExitStack
        pila = ExitStack()
        pila.enter_context(patch(f'{MODELO_API}.consultaStockPaginado',
                                 self._listado(stock_por_codigo, tam)))
        pila.enter_context(patch(f'{MODELO_API}.consultaStockProducto',
                                 self._por_codigo(por_codigo)))
        return pila

    def _consultar(self, conc, stock_por_codigo, tam=10, por_codigo=None):
        with self._wis(stock_por_codigo, tam, por_codigo):
            vueltas = 0
            while conc._cs_consultar_tanda():
                vueltas += 1
                self.assertLess(vueltas, 100, "la consulta no termina")

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
        self._consultar(conc, {'PRD-TEST-0': 7.0, 'PRD-TEST-1': 5.0, 'PRD-TEST-2': 3.0})
        filas = self._filas(conc)
        self.assertEqual(float(filas[self.variantes[0].id][1]), 7.0)
        self.assertEqual(float(filas[self.variantes[0].id][2]), -3.0)   # 7 - 10
        self.assertEqual(filas[self.variantes[1].id][3], 'ok')

    def test_lo_que_wis_no_conoce_no_es_cero(self):
        """🔴 El invariante que importa: lo que WIS no conoce queda marcado y
        NO se toma como stock cero."""
        conc = self._nueva()
        conc.action_armar_tabla()
        # El listado sólo informa una de las tres; por código, las otras dos
        # no existen en WIS.
        self._consultar(conc, {'PRD-TEST-1': 5.0})

        filas = self._filas(conc)
        for v in (self.variantes[0], self.variantes[2]):
            self.assertEqual(filas[v.id][3], 'no_existe')
            self.assertIsNone(filas[v.id][1], "nunca puede quedar una cantidad asumida")
            self.assertIsNone(filas[v.id][2])
        self.assertEqual(conc.cs_errors, 2)
        self.assertEqual(conc.cs_no_existe, 2)

    def test_lo_que_el_listado_no_trae_se_pregunta_por_codigo(self):
        """Lo que no aparece en el listado no se marca a ciegas: se consulta."""
        conc = self._nueva()
        conc.action_armar_tabla()
        consultados = []
        original = self._por_codigo({'PRD-TEST-0': ('sin_stock', 0.0),
                                     'PRD-TEST-2': ('no_existe', None)})

        def espia(cfg, codigo, traza=None):
            consultados.append(codigo)
            return original(cfg, codigo, traza)

        with patch(f'{MODELO_API}.consultaStockPaginado',
                   self._listado({'PRD-TEST-1': 5.0})), \
                patch(f'{MODELO_API}.consultaStockProducto', espia):
            while conc._cs_consultar_tanda():
                pass
        self.assertEqual(sorted(consultados), ['PRD-TEST-0', 'PRD-TEST-2'])
        self.assertEqual(conc.cs_pc_total, 2)
        self.assertEqual(conc.cs_pc_done, 2)

    def test_sin_stock_por_codigo_entra_como_cero(self):
        """Decidido el 28-09-2026: «existe y no tiene stock», preguntado POR
        CÓDIGO, es una respuesta explícita de WIS y entra al ajuste como cero."""
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar(conc, {'PRD-TEST-1': 5.0},
                        por_codigo={'PRD-TEST-0': ('sin_stock', 0.0),
                                    'PRD-TEST-2': ('sin_stock', 0.0)})
        filas = self._filas(conc)
        self.assertEqual(filas[self.variantes[0].id][3], 'sin_stock')
        self.assertEqual(float(filas[self.variantes[0].id][1]), 0.0)
        self.assertEqual(float(filas[self.variantes[0].id][2]), -10.0)
        self.assertEqual(conc.cs_sin_stock, 2)
        self.assertEqual(conc.cs_errors, 0)
        resumen = conc._cs_resumen()
        self.assertEqual(resumen['consultadas'], 3)
        self.assertEqual(resumen['a_cero'], 1)   # sólo la que tenía 10

    def test_si_el_listado_falla_no_marca_a_nadie_como_faltante(self):
        """Una página caída no puede dar por terminado el listado: eso dejaría
        como «no informadas» a todas las que faltaban leer."""
        conc = self._nueva()
        conc.action_armar_tabla()

        conc.write({'cs_etapa_consulta': 'listado', 'cs_estrategia': 'listado',
                    'cs_paginas_total': 1})

        def caido(cfg, pagina, filtros=None, traza=None):
            raise ValueError("WIS no responde")

        with patch(f'{MODELO_API}.consultaStockPaginado', caido):
            with self.assertRaises(ValueError):
                conc._cs_consultar_tanda()

        for fila in self._filas(conc).values():
            self.assertEqual(fila[3], 'pendiente')

    def test_listado_vacio_consulta_todo_por_codigo(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar(conc, {})
        self.assertEqual(conc.cs_paginas_total, 0)
        self.assertEqual(conc.cs_estrategia, 'por_codigo')
        for fila in self._filas(conc).values():
            self.assertEqual(fila[3], 'no_existe')
            self.assertIsNone(fila[1])
        self.assertEqual(conc.cs_errors, 3)

    def test_el_cero_explicito_de_wis_si_cuenta(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar(conc, {'PRD-TEST-0': 0.0, 'PRD-TEST-1': 0.0, 'PRD-TEST-2': 0.0})
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
        with self._wis(respuestas, tam=2):
            self.assertTrue(conc._cs_consultar_tanda(), "el sondeo deja trabajo")
            self.assertEqual(conc.cs_paginas_total, 2)
            self.assertEqual(conc.cs_estrategia, 'listado')
            quedan = conc._cs_consultar_tanda()
            self.assertEqual(quedan, 2, "la tanda se llenó: puede quedar más")
            self.assertEqual(conc.cs_done, 2)
            self.assertEqual(conc._cs_consultar_tanda(), 0)
        self.assertEqual(conc.cs_done, 3)

    def test_el_resumen_cuenta_las_diferencias(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar(conc, {'PRD-TEST-0': 10.0, 'PRD-TEST-1': 99.0, 'PRD-TEST-2': 0.0})
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
    def _consultar_todo(self, conc, respuestas, por_codigo=None):
        self._consultar(conc, respuestas, por_codigo=por_codigo)
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
        self._consultar_todo(conc, {'PRD-TEST-0': 7.0})   # las otras no existen en WIS
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

    def test_sin_stock_llega_al_ajuste_en_cero(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.cs_tope_a_cero_pct = 100
        self._consultar_todo(conc, {'PRD-TEST-1': 5.0, 'PRD-TEST-2': 0.0},
                             por_codigo={'PRD-TEST-0': ('sin_stock', 0.0)})
        conc.action_generar_ajuste()
        batch = self.env['forum.import.batch'].browse(conc.cs_batch_id)
        self.env.cr.execute("SELECT product_id, cantidad FROM %s" % batch._staging_name())
        celdas = dict(self.env.cr.fetchall())
        self.assertEqual(float(celdas[self.variantes[0].id]), 0.0)
        self.assertEqual(conc.cs_ajuste_celdas, 1)
        self.assertTrue(conc.cs_ajuste_ended_at)

    # --- estrategia y sondeo -------------------------------------------
    def test_el_sondeo_mide_las_paginas(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        # 33 productos en WIS de a 10: 4 páginas, la última con 3.
        stock = {'PRD-TEST-%d' % i: 1.0 for i in range(3)}
        stock.update({'OTRO-%02d' % i: 1.0 for i in range(30)})
        with self._wis(stock):
            conc._cs_consultar_tanda()
        self.assertEqual(conc.cs_paginas_total, 4)
        self.assertEqual(conc.cs_etapa_consulta, 'por_codigo',
                         "4 páginas contra 3 variantes: conviene preguntar por código")

    def test_listado_mas_largo_que_la_tabla_va_por_codigo(self):
        """Si WIS tiene muchos más productos que los nuestros, recorrer el
        listado es más caro que preguntar por cada código."""
        conc = self._nueva()
        conc.action_armar_tabla()
        stock = {'OTRO-%03d' % i: 1.0 for i in range(80)}   # 8 páginas
        leidas = []
        listado = self._listado(stock)

        def espia(cfg, pagina, filtros=None, traza=None):
            leidas.append(pagina)
            return listado(cfg, pagina, filtros, traza)

        with patch(f'{MODELO_API}.consultaStockPaginado', espia), \
                patch(f'{MODELO_API}.consultaStockProducto', self._por_codigo({
                    'PRD-TEST-0': ('ok', 9.0), 'PRD-TEST-1': ('ok', 5.0),
                    'PRD-TEST-2': ('sin_stock', 0.0)})):
            while conc._cs_consultar_tanda():
                pass
        self.assertEqual(conc.cs_estrategia, 'por_codigo')
        self.assertLessEqual(len(leidas), 10, "sólo el sondeo lee páginas")
        self.assertEqual(conc.cs_encontradas, 2)
        self.assertEqual(conc.cs_sin_stock, 1)

    # --- lo que se le pide a WIS queda a la vista -----------------------
    def test_cada_request_queda_registrado(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.cs_started_at = '2000-01-01 00:00:00'
        self._consultar(conc, {'PRD-TEST-0': 7.0, 'PRD-TEST-1': 5.0})
        consultas = conc.cs_consulta_ids
        self.assertTrue(consultas.filtered(lambda c: c.etapa == 'sondeo'))
        listado = consultas.filtered(lambda c: c.etapa == 'listado' and c.resultado == 'ok')
        self.assertEqual(sum(listado.mapped('nuestras')), 2)
        self.assertIn('"pagina"', listado[:1].body)
        por_codigo = consultas.filtered(lambda c: c.etapa == 'por_codigo')
        self.assertEqual(por_codigo.producto, 'PRD-TEST-2')
        conc.invalidate_recordset(['cs_requests'])
        self.assertEqual(conc.cs_requests, len(consultas))
        self.assertTrue(conc.cs_ultimo_request)

    # --- el bug de producción: el fin del listado es un 400 --------------
    def _wis_http(self, paginas, codigos_sin_stock=(), caido_desde=None):
        """Finge el SERVIDOR de WIS: el 400 al final, como el real."""
        cfg = self.config
        cfg.write({'token': 'falso', 'expiracionToken': '2999-01-01 00:00:00'})
        llamadas = []

        class Resp:
            def __init__(self, status, cuerpo):
                self.status_code = status
                self.text = json.dumps(cuerpo)
                self._c = cuerpo

            def json(self):
                return self._c

        def request(url=None, method=None, json=None, **kw):
            llamadas.append(json)
            if caido_desde and len(llamadas) >= caido_desde:
                return Resp(500, {'title': 'Internal Server Error'})
            producto = (json.get('filtros') or {}).get('producto')
            if producto:
                if producto in codigos_sin_stock:
                    return Resp(400, SIN_STOCK)
                return Resp(400, NO_EXISTE)
            filas = paginas.get(json['pagina'])
            if not filas:
                return Resp(400, SIN_STOCK)
            return Resp(200, {'stock': filas})

        return patch(f'{MODULO_API}.requests.request', side_effect=request), llamadas

    def test_la_clasificacion_de_las_respuestas_de_wis(self):
        parche, _llamadas = self._wis_http(
            {1: [{'producto': 'X', 'stockGeneral': 3.0, 'stockDisponible': 3.0}]},
            codigos_sin_stock=('SIN',))
        with parche:
            self.assertEqual(len(self.config.consultaStockPaginado(1)), 1)
            self.assertEqual(self.config.consultaStockPaginado(2), [],
                             "🔴 el 400 «No se encontró stock» es el fin, no un error")
            self.assertEqual(self.config.consultaStockProducto('SIN'), ('sin_stock', 0.0))
            self.assertEqual(self.config.consultaStockProducto('NADA'), ('no_existe', None))
            self.assertIsNone(self.config.consultaStockCodigo('NADA'))

    def test_un_500_si_es_error(self):
        parche, _llamadas = self._wis_http({}, caido_desde=1)
        traza = {}
        with parche, self.assertRaises(ErrorConsultaStockWIS):
            self.config.consultaStockPaginado(1, traza=traza)
        self.assertEqual(traza['status'], 500)
        self.assertEqual(traza['clase'], 'error')

    def test_la_consulta_termina_contra_el_400_real(self):
        """El caso de producción del 28-09-2026: con el WIS real, que contesta
        400 al pasar la última página, la consulta tiene que TERMINAR."""
        conc = self._nueva()
        conc.action_armar_tabla()
        paginas = {1: [{'producto': 'PRD-TEST-0', 'stockGeneral': 4.0, 'stockDisponible': 4.0},
                       {'producto': 'PRD-TEST-1', 'stockGeneral': 5.0, 'stockDisponible': 5.0},
                       {'producto': 'PRD-TEST-2', 'stockGeneral': 0.0, 'stockDisponible': 0.0}]}
        parche, llamadas = self._wis_http(paginas)
        with parche:
            vueltas = 0
            while conc._cs_consultar_tanda():
                vueltas += 1
                self.assertLess(vueltas, 10, "🔴 la consulta no termina")
        self.assertEqual(conc.cs_done, 3)
        self.assertEqual(conc.cs_encontradas, 3)
        self.assertLess(len(llamadas), 10)

    def _sin_commit(self):
        """`_cs_varias_tandas` commitea y hace rollback: en un test no se puede."""
        from contextlib import ExitStack
        pila = ExitStack()
        pila.enter_context(patch.object(type(self.env.cr), 'commit', lambda s: None))
        pila.enter_context(patch.object(type(self.env.cr), 'rollback', lambda s: None))
        return pila

    def test_un_error_detiene_la_consulta_sin_bucle(self):
        """🔴 Antes: la tanda caía, el cron la retomaba cada 5 minutos y la
        consulta repetía la misma tanda sin fin. Ahora queda DETENIDA."""
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.write({'fase': 'consultando', 'estado': 'en_proceso',
                    'cs_started_at': '2000-01-01 00:00:00'})
        parche, _llamadas = self._wis_http({}, caido_desde=1)
        with parche, self._sin_commit():
            conc._cs_varias_tandas()
        self.assertEqual(conc.fase, 'consultando')
        self.assertEqual(conc.estado, 'error')
        self.assertIn('500', conc.cs_error_msg)
        self.assertTrue(conc.cs_consulta_ids.filtered(lambda c: c.resultado == 'error'),
                        "el request que falló queda registrado")
        self.assertNotIn(conc, self.env['conciliacion.stock'].search(
            [('fase', '=', 'consultando'), ('estado', '!=', 'error')]),
            "el cron no la retoma sola")

    def test_reanudar_sigue_desde_donde_iba(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.write({'fase': 'consultando', 'estado': 'error', 'cs_pagina': 4,
                    'cs_etapa_consulta': 'listado', 'cs_error_msg': 'HTTP 500'})
        with self._sin_commit(), patch.object(type(conc), '_cs_encolar_cron', lambda s: None):
            conc.action_reanudar_consulta()
        self.assertEqual(conc.estado, 'en_proceso')
        self.assertEqual(conc.cs_pagina, 4)
        self.assertEqual(conc.cs_etapa_consulta, 'listado')
        self.assertFalse(conc.cs_error_msg)

    def test_cancelar_una_detenida_la_deja_para_reanudar(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        conc.write({'fase': 'consultando', 'estado': 'error'})
        conc.action_cancelar_consulta()
        self.assertEqual(conc.fase, 'tabla')
        self.assertTrue(conc.cs_cancel_requested)

    def test_descargar_el_detalle(self):
        conc = self._nueva()
        conc.action_armar_tabla()
        self._consultar(conc, {'PRD-TEST-0': 7.0})
        accion = conc.action_descargar_detalle()
        self.assertEqual(accion['type'], 'ir.actions.act_url')
        adjunto = self.env['ir.attachment'].search(
            [('res_model', '=', 'conciliacion.stock'), ('res_id', '=', conc.id)])
        self.assertTrue(adjunto.datas)

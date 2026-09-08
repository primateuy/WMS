# -*- coding: utf-8 -*-
"""Tests de la integración de productos con WIS.

Cubren el requerimiento "Integración de Productos con WIS" y las regresiones
de los bugs que se arreglaron junto con él. No golpean la red: la capa HTTP
(`consultarAPI`) se mockea.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

MODELO_API = 'odoo.addons.integracion_wis.models.models.IntegracionWIS'


@tagged('post_install', '-at_install', 'wis_productos')
class TestIntegracionProductosWIS(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1
        )
        if not cls.config:
            cls.config = cls.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test',
                'client_secret': 'test',
                'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api',
                'company_id': cls.env.company.id,
                'comunicacion_activa': True,
            })
        else:
            cls.config.write({'comunicacion_activa': True})

        # Productos sin integración: no disparan llamadas a WIS al crearse.
        cls.producto = cls.env['product.product'].create({
            'name': 'Producto de prueba WIS',
            'type': 'product',
        })

    # ------------------------------------------------------------------
    # Código único
    # ------------------------------------------------------------------
    def test_codigo_unico_es_deterministico(self):
        """El código WIS se deriva del id, no de un random."""
        codigo = self.config._wis_codigo_producto(self.producto)
        self.assertEqual(codigo, f"PRD-{self.producto.id}")
        # Estable entre llamadas: antes cambiaba en cada invocación.
        self.assertEqual(codigo, self.config._wis_codigo_producto(self.producto))

    def test_codigo_unico_respeta_el_existente(self):
        """Si el producto ya tiene código, no se le asigna otro."""
        self.producto.with_context(_avoid_wms=True).codigo_unico = 'PRD-999999'
        self.assertEqual(self.config._wis_codigo_producto(self.producto), 'PRD-999999')

    def test_codigo_unico_desambigua_colision(self):
        """Un código random viejo que coincida con un id no se pisa."""
        otro = self.env['product.product'].create({
            'name': 'Producto con código heredado',
            'type': 'product',
        })
        otro.with_context(_avoid_wms=True).codigo_unico = f"PRD-{self.producto.id}"

        codigo = self.config._wis_codigo_producto(self.producto)
        self.assertEqual(codigo, f"PRD-{self.producto.id}-1")

    def test_codigo_unico_no_se_duplica(self):
        """La constraint impide asignar el mismo código a dos productos."""
        from odoo.exceptions import ValidationError

        otro = self.env['product.product'].create({
            'name': 'Otro producto',
            'type': 'product',
        })
        self.producto.with_context(_avoid_wms=True).codigo_unico = 'PRD-DUP'
        with self.assertRaises(ValidationError):
            otro.with_context(_avoid_wms=True).codigo_unico = 'PRD-DUP'

    # ------------------------------------------------------------------
    # Regresión: el nombre no se trunca en la base de Odoo
    # ------------------------------------------------------------------
    def test_insertar_producto_no_trunca_el_nombre_en_odoo(self):
        """El recorte a 65 caracteres es solo para el payload de WIS.

        Antes `insertarProducto` hacía `vals.name = vals.name[:65]` sobre el
        record, lo que escribía el nombre recortado en el template — o sea, en
        todas sus variantes.
        """
        nombre_largo = 'X' * 120
        self.producto.with_context(_avoid_wms=True).name = nombre_largo

        with patch(f'{MODELO_API}.consultarAPI', return_value={'numeroInterfaz': 'IF-1'}):
            self.config.insertarProducto(self.producto)

        self.assertEqual(self.producto.name, nombre_largo,
                         "insertarProducto no debe modificar el nombre del producto en Odoo")

    def test_payload_trunca_a_65(self):
        """El payload sí viaja recortado."""
        self.producto.with_context(_avoid_wms=True).name = 'Y' * 120
        payload = self.config._build_producto_payload(self.producto)
        self.assertEqual(len(payload['descripcion']), 65)

    # ------------------------------------------------------------------
    # Regresión: campos que NO deben disparar una llamada a WIS
    # ------------------------------------------------------------------
    def test_standard_price_no_dispara_envio(self):
        """El costo se recalcula solo con AVCO/FIFO en cada recepción.

        Tenerlo en CAMPOS_WIS hacía que validar una recepción de N líneas
        disparara N llamadas HTTP dentro de la transacción.
        """
        self.assertNotIn('standard_price', self.env['product.product'].CAMPOS_WIS)
        self.assertNotIn('taxes_id', self.env['product.product'].CAMPOS_WIS)

    def test_campos_wis_son_los_del_payload(self):
        """CAMPOS_WIS no debe tener campos que no viajan a WIS."""
        campos_payload = {'name', 'active', 'integracion_wms', 'barcode',
                          'list_price', 'weight', 'uom_id', 'categ_id'}
        self.assertEqual(self.env['product.product'].CAMPOS_WIS, campos_payload)

    # ------------------------------------------------------------------
    # Envío masivo
    # ------------------------------------------------------------------
    def test_masivo_da_de_alta_variantes_sin_codigo(self):
        """El alta inicial también viaja por lote.

        Antes `insertarProductosMasivo` filtraba `if v.codigo_unico` y se
        salteaba en silencio justo a las variantes que había que dar de alta.
        """
        variantes = self.env['product.product']
        for i in range(3):
            variantes |= self.env['product.product'].create({
                'name': f'Variante masiva {i}',
                'type': 'product',
            })

        with patch(f'{MODELO_API}.consultarAPI', return_value={}) as mock_api:
            resultado = self.config.insertarProductosMasivo(variantes, enviar_barcodes=False)

        self.assertEqual(resultado['enviados'], 3)
        self.assertEqual(resultado['errores'], 0)
        # Una sola llamada HTTP para las 3 variantes.
        self.assertEqual(mock_api.call_count, 1)
        # Y el código quedó persistido en Odoo.
        for variante in variantes:
            self.assertEqual(variante.codigo_unico, f"PRD-{variante.id}")

    def test_masivo_respeta_el_chunk_size(self):
        variantes = self.env['product.product']
        for i in range(5):
            variantes |= self.env['product.product'].create({
                'name': f'Variante chunk {i}',
                'type': 'product',
            })

        with patch(f'{MODELO_API}.consultarAPI', return_value={}) as mock_api:
            self.config.insertarProductosMasivo(variantes, chunk_size=2, enviar_barcodes=False)

        # 5 variantes en lotes de 2 -> 3 llamadas
        self.assertEqual(mock_api.call_count, 3)

    # ------------------------------------------------------------------
    # Códigos de barras
    # ------------------------------------------------------------------
    def test_existe_barcode_404_es_false(self):
        """Un 404 significa que el código NO existe.

        Antes se evaluaba `if req is not None`, que es siempre verdadero: todo
        código se mandaba como sustitución en vez de alta.
        """
        class RespuestaFalsa:
            status_code = 404
            text = 'not found'

            def json(self):
                return {}

        with patch('odoo.addons.integracion_wis.models.models.requests.get',
                   return_value=RespuestaFalsa()):
            self.config.token = 'x'
            self.config.expiracionToken = '2999-01-01 00:00:00'
            self.assertFalse(self.config._existe_barcode_codigo('7791234567890'))

    def test_existe_barcode_acepta_alfanumerico(self):
        """`int(barcode)` reventaba con códigos no numéricos."""
        class RespuestaFalsa:
            status_code = 200
            text = '[]'

            def json(self):
                return []

        with patch('odoo.addons.integracion_wis.models.models.requests.get',
                   return_value=RespuestaFalsa()):
            self.config.token = 'x'
            self.config.expiracionToken = '2999-01-01 00:00:00'
            self.assertFalse(self.config._existe_barcode_codigo('ABC-123'))


@tagged('post_install', '-at_install', 'wis_productos')
class TestColaWIS(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Cola = cls.env['wis.sync.queue']
        cls.variantes = cls.env['product.product']
        for i in range(3):
            cls.variantes |= cls.env['product.product'].create({
                'name': f'Producto cola {i}',
                'type': 'product',
            })

    def test_encolar_crea_una_entrada_por_variante(self):
        entradas = self.Cola._encolar(self.variantes, origen='masiva')
        self.assertEqual(len(entradas), 3)
        self.assertEqual(set(entradas.mapped('estado')), {'pendiente'})

    def test_encolar_no_duplica(self):
        self.Cola._encolar(self.variantes, origen='masiva')
        self.Cola._encolar(self.variantes, origen='masiva')
        pendientes = self.Cola.search([
            ('product_id', 'in', self.variantes.ids),
            ('estado', '=', 'pendiente'),
        ])
        self.assertEqual(len(pendientes), 3)

    def test_encolar_sube_la_prioridad(self):
        """Si la variante ya estaba en cola con prioridad baja y aparece en una
        OC, pasa al frente en vez de duplicarse."""
        self.Cola._encolar(self.variantes, origen='template', prioridad=20)
        self.Cola._encolar(self.variantes, origen='oc', origen_ref='P00001', prioridad=1)

        entradas = self.Cola.search([('product_id', 'in', self.variantes.ids)])
        self.assertEqual(len(entradas), 3)
        self.assertEqual(set(entradas.mapped('prioridad')), {1})
        self.assertEqual(set(entradas.mapped('origen')), {'oc'})

    def test_solo_almacenables(self):
        servicio = self.env['product.product'].create({
            'name': 'Servicio',
            'type': 'service',
        })
        entradas = self.Cola._encolar(servicio, origen='masiva')
        self.assertFalse(entradas)


@tagged('post_install', '-at_install', 'wis_productos')
class TestOrdenCompraPendientes(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1
        )
        if not cls.config:
            cls.config = cls.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test',
                'client_secret': 'test',
                'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api',
                'company_id': cls.env.company.id,
                'comunicacion_activa': True,
            })
        cls.config.write({'comunicacion_activa': True, 'exigir_integracion_oc': False})

        cls.picking_type = cls.env['stock.picking.type'].search([
            ('code', '=', 'incoming'),
            ('company_id', '=', cls.env.company.id),
        ], limit=1)
        cls.picking_type.integracion_wms = True

        cls.proveedor = cls.env['res.partner'].create({'name': 'Proveedor WIS'})

        # Marcado para WMS y sin código -> pendiente
        cls.sin_integrar = cls.env['product.product'].create({
            'name': 'Sin integrar',
            'type': 'product',
        })
        cls.sin_integrar.with_context(_avoid_wms=True).integracion_wms = True

        # Marcado para WMS y con código -> integrado
        cls.integrado = cls.env['product.product'].create({
            'name': 'Ya integrado',
            'type': 'product',
        })
        cls.integrado.with_context(_avoid_wms=True).write({
            'integracion_wms': True,
            'codigo_unico': 'PRD-YA-INTEGRADO',
        })

        # Sin marcar -> no cuenta, salvo exigir_integracion_oc
        cls.no_marcado = cls.env['product.product'].create({
            'name': 'No marcado',
            'type': 'product',
        })

    def _crear_orden(self, productos):
        return self.env['purchase.order'].create({
            'partner_id': self.proveedor.id,
            'picking_type_id': self.picking_type.id,
            'order_line': [(0, 0, {
                'product_id': p.id,
                'name': p.name,
                'product_qty': 1,
                'price_unit': 10,
                'date_planned': '2026-01-01 00:00:00',
            }) for p in productos],
        })

    def test_detecta_solo_las_no_integradas(self):
        orden = self._crear_orden(self.sin_integrar | self.integrado)
        self.assertTrue(orden.wis_recepcion_integrada)
        self.assertEqual(orden.wis_cantidad_pendientes, 1)
        self.assertEqual(orden.wis_variantes_pendientes_ids, self.sin_integrar)

    def test_no_marcado_no_cuenta_por_defecto(self):
        orden = self._crear_orden(self.no_marcado)
        self.assertEqual(orden.wis_cantidad_pendientes, 0)

    def test_exigir_integracion_oc_incluye_todo_almacenable(self):
        self.config.exigir_integracion_oc = True
        orden = self._crear_orden(self.no_marcado)
        orden.invalidate_recordset()
        self.assertEqual(orden.wis_cantidad_pendientes, 1)
        self.config.exigir_integracion_oc = False

    def test_picking_type_no_integrado_no_marca_pendientes(self):
        otro_tipo = self.picking_type.copy({'integracion_wms': False})
        orden = self._crear_orden(self.sin_integrar)
        orden.picking_type_id = otro_tipo
        orden.invalidate_recordset()
        self.assertFalse(orden.wis_recepcion_integrada)
        self.assertEqual(orden.wis_cantidad_pendientes, 0)

    def test_comunicacion_deshabilitada_no_marca_pendientes(self):
        self.config.comunicacion_activa = False
        orden = self._crear_orden(self.sin_integrar)
        orden.invalidate_recordset()
        self.assertEqual(orden.wis_cantidad_pendientes, 0)
        self.config.comunicacion_activa = True

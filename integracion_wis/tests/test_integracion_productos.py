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
                          'default_code', 'list_price', 'weight', 'uom_id',
                          'categ_id'}
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


@tagged('post_install', '-at_install', 'wis_productos')
class TestMarcadoEnLaFichaEncola(TransactionCase):
    """Tildar «Integración con WMS» y guardar NO puede llamar a WIS.

    Era el último camino automático que mandaba adentro de su transacción: con
    muchas variantes el guardado quedaba minutos esperando la respuesta, con la
    plantilla y todas sus variantes bloqueadas.
    """

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
        cls.Cola = cls.env['wis.sync.queue']

    def _template_con_variantes(self, cantidad=3):
        """Template SIN marcar (así el alta no encola) y con `cantidad` variantes."""
        atributo = self.env['product.attribute'].create({
            'name': 'Talle prueba WIS',
            'value_ids': [(0, 0, {'name': f'T{i}'}) for i in range(cantidad)],
        })
        return self.env['product.template'].create({
            'name': 'Template con variantes WIS',
            'type': 'product',
            'attribute_line_ids': [(0, 0, {
                'attribute_id': atributo.id,
                'value_ids': [(6, 0, atributo.value_ids.ids)],
            })],
        })

    def test_marcar_la_casilla_no_llama_a_wis(self):
        template = self._template_con_variantes(3)

        def explotar(*args, **kwargs):
            raise AssertionError("El guardado llamó a WIS: tiene que encolar.")

        with patch.object(type(self.env['product.product']),
                          '_enviar_wms_en_lote', explotar), \
             patch.object(type(self.env['product.product']), 'enviarWS', explotar):
            template.write({'integracion_wms': True})

        entradas = self.Cola.search([
            ('product_id', 'in', template.product_variant_ids.ids),
            ('estado', '=', 'pendiente'),
        ])
        self.assertEqual(len(entradas), 3)

    def test_marcar_la_casilla_marca_las_variantes(self):
        template = self._template_con_variantes(3)
        with patch.object(type(self.env['product.product']), '_enviar_wms_en_lote',
                          lambda *a, **k: {}):
            template.write({'integracion_wms': True})
        self.assertTrue(all(template.product_variant_ids.mapped('integracion_wms')))

    def test_marcar_deja_constancia_en_el_chatter(self):
        """Un `write` no devuelve notificación: si no hay rastro, el usuario
        guarda y no ve nada."""
        template = self._template_con_variantes(2)
        mensajes_antes = len(template.message_ids)
        template.write({'integracion_wms': True})
        cuerpos = template.message_ids.mapped('body')
        self.assertGreater(len(template.message_ids), mensajes_antes)
        self.assertTrue(any('en cola' in (c or '') for c in cuerpos))

    def test_marcar_dos_veces_no_duplica_la_cola(self):
        template = self._template_con_variantes(3)
        template.write({'integracion_wms': True})
        template.write({'integracion_wms': False})
        template.write({'integracion_wms': True})
        entradas = self.Cola.search([
            ('product_id', 'in', template.product_variant_ids.ids),
            ('estado', 'in', ('pendiente', 'procesando')),
        ])
        self.assertEqual(len(entradas), 3)

    def test_el_contador_de_la_ficha_cuenta_lo_pendiente(self):
        template = self._template_con_variantes(3)
        template.write({'integracion_wms': True})
        template.invalidate_recordset(['wis_variantes_en_cola'])
        self.assertEqual(template.wis_variantes_en_cola, 3)

    def test_alta_de_variante_marcada_encola(self):
        """Crear una variante marcada, con su template sin marcar, tampoco
        puede mandar: un WIS caído no bloquea el maestro de productos."""

        def explotar(*args, **kwargs):
            raise AssertionError("El alta llamó a WIS: tiene que encolar.")

        with patch.object(type(self.env['product.product']),
                          '_enviar_wms_en_lote', explotar), \
             patch.object(type(self.env['product.product']), 'enviarWS', explotar):
            variante = self.env['product.product'].create({
                'name': 'Variante suelta marcada',
                'type': 'product',
                'integracion_wms': True,
            })

        self.assertTrue(self.Cola.search([('product_id', '=', variante.id),
                                          ('estado', '=', 'pendiente')]))

    def test_actualizar_una_integrada_sigue_yendo_en_el_momento(self):
        """La ACTUALIZACIÓN de lo ya integrado no cambia: son pocos registros
        y el usuario espera verlo reflejado."""
        variante = self.env['product.product'].create({
            'name': 'Variante ya integrada',
            'type': 'product',
        })
        variante.with_context(_avoid_wms=True).write({
            'integracion_wms': True, 'codigo_unico': 'PRD-TEST'})

        llamadas = []
        with patch.object(type(self.env['product.product']), 'enviarWS',
                          lambda self, *a, **k: llamadas.append(self.id)):
            variante.write({'name': 'Nombre nuevo'})

        self.assertEqual(llamadas, [variante.id])

    def test_encolar_dispara_el_cron(self):
        """Sin el trigger la cola espera hasta 5 minutos y encolar se parece
        demasiado a no haber hecho nada.

        Se verifica que se PIDE el disparo, no que aparezca el `ir.cron.trigger`:
        con el cron desactivado —como está en las bases locales, a propósito—
        `_trigger_list` descarta el pedido y no crea la fila. El test no puede
        depender de esa configuración.
        """
        cron = self.env.ref('integracion_wis.ir_cron_wis_procesar_cola_productos')
        disparos = []
        original = type(cron)._trigger

        def espiar(self, at=None):
            disparos.append(self.id)
            return original(self, at=at)

        template = self._template_con_variantes(2)
        with patch.object(type(cron), '_trigger', espiar):
            template.write({'integracion_wms': True})

        self.assertIn(cron.id, disparos)

    def test_el_fallo_del_disparo_no_rompe_el_guardado(self):
        """Encolar no puede depender de que el cron esté disponible."""
        cron = self.env.ref('integracion_wis.ir_cron_wis_procesar_cola_productos')

        def fallar(self, at=None):
            raise ValueError("cron no disponible")

        template = self._template_con_variantes(2)
        with patch.object(type(cron), '_trigger', fallar):
            template.write({'integracion_wms': True})

        entradas = self.Cola.search([
            ('product_id', 'in', template.product_variant_ids.ids),
            ('estado', '=', 'pendiente'),
        ])
        self.assertEqual(len(entradas), 2)

    def test_boton_integrar_ahora_si_manda(self):
        template = self._template_con_variantes(2)
        template.write({'integracion_wms': True})

        llamadas = []

        def fingir(self, motivo=''):
            llamadas.append(len(self))
            return {'enviados': len(self), 'errores': 0, 'errores_detalle': []}

        with patch.object(type(self.env['product.product']),
                          '_enviar_wms_en_lote', fingir):
            template.enviar_variantes_wms()

        self.assertEqual(llamadas, [2])
        pendientes = self.Cola.search([
            ('product_id', 'in', template.product_variant_ids.ids),
            ('estado', '=', 'pendiente'),
        ])
        self.assertFalse(pendientes, "Integrar ahora tiene que cerrar lo encolado.")

    def test_boton_integrar_ahora_propaga_el_error(self):
        """Antes se logueaba y la pantalla decía que había salido bien."""
        template = self._template_con_variantes(2)
        template.write({'integracion_wms': True})

        def fallar(*args, **kwargs):
            raise ValueError("WIS no responde")

        with patch.object(type(self.env['product.product']),
                          '_enviar_wms_en_lote', fallar):
            with self.assertRaises(ValueError):
                template.enviar_variantes_wms()

    def test_el_error_de_la_cola_dice_lo_que_dijo_wis(self):
        """La variante rechazada no tiene `codigo_unico` —se persiste recién
        cuando WIS acepta—, y buscar su error por ese campo tapaba el motivo."""
        template = self._template_con_variantes(2)
        template.write({'integracion_wms': True})
        variantes = template.product_variant_ids
        codigo = self.config._wis_codigo_producto(variantes[0])

        def fingir(self, motivo=''):
            return {'enviados': 1, 'errores': 1, 'barcodes_enviados': 0,
                    'errores_detalle': [f"{codigo}: Error en la API: 400 - producto rechazado"]}

        entradas = self.Cola.search([('product_id', 'in', variantes.ids)])
        with patch.object(type(self.env['product.product']),
                          '_enviar_wms_en_lote', fingir):
            entradas.procesar()

        fallada = entradas.filtered(lambda e: e.product_id == variantes[0])
        self.assertIn('producto rechazado', fallada.ultimo_error or '')
        self.assertEqual(fallada.intentos, 1)


@tagged('post_install', '-at_install', 'wis_productos')
class TestPayloadProductoSegunDoc(TransactionCase):
    """El payload del maestro, contrastado contra WIS - WMS API 10.2 §24.1."""

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
                'company_id': cls.env.company.id, 'comunicacion_activa': True,
            })
        cls.atributo = cls.env['product.attribute'].create({
            'name': 'Color prueba WIS',
            'value_ids': [(0, 0, {'name': 'Gris Claro Melange'}), (0, 0, {'name': 'Marino'})],
        })

    def _variante(self, nombre='Producto WIS', **vals):
        tmpl = self.env['product.template'].create(dict({
            'name': nombre, 'type': 'product',
            'attribute_line_ids': [(0, 0, {
                'attribute_id': self.atributo.id,
                'value_ids': [(6, 0, self.atributo.value_ids.ids)]})],
        }, **vals))
        return tmpl.product_variant_ids[0]

    # --- campos que la doc NO tiene, y los que sí ---------------------
    def test_no_manda_campos_inexistentes(self):
        """`codigoProducto`, `activo`, `familia` y `clase` no están en §24.1:
        WIS los ignoraba."""
        payload = self.config._build_producto_payload(self._variante())
        for campo in ('codigoProducto', 'activo', 'familia', 'clase'):
            self.assertNotIn(campo, payload)

    def test_situacion_reemplaza_a_activo(self):
        """15 activo / 16 inactivo. Con `activo` desactivar en Odoo no
        desactivaba en WIS."""
        variante = self._variante()
        self.assertEqual(self.config._build_producto_payload(variante)['situacion'], 15)
        variante.with_context(_avoid_wms=True).write({'active': False})
        self.assertEqual(self.config._build_producto_payload(variante)['situacion'], 16)

    def test_familia_y_clase_con_el_nombre_de_la_doc(self):
        payload = self.config._build_producto_payload(self._variante())
        self.assertEqual(payload['codigoFamilia'], 1)
        self.assertEqual(payload['codigoClase'], "1")

    # --- el código adicional -----------------------------------------
    def test_codigo_producto_empresa_lleva_el_barcode(self):
        """Por qué WIS repetía el PRD-: la doc dice que si no se manda, le
        asigna el mismo código de artículo."""
        variante = self._variante()
        variante.with_context(_avoid_wms=True).write({'barcode': '7790001234567'})
        payload = self.config._build_producto_payload(variante)
        self.assertEqual(payload['codigoProductoEmpresa'], '7790001234567')
        self.assertNotEqual(payload['codigoProductoEmpresa'], payload['codigo'])

    def test_codigo_producto_empresa_cae_en_default_code(self):
        variante = self._variante()
        variante.with_context(_avoid_wms=True).write({'default_code': 'SKU-123'})
        self.assertEqual(
            self.config._build_producto_payload(variante)['codigoProductoEmpresa'],
            'SKU-123')

    def test_sin_barcode_ni_sku_no_se_manda_el_campo(self):
        """Se omite y WIS hace lo de siempre, que es el comportamiento previo."""
        variante = self._variante()
        variante.with_context(_avoid_wms=True).write({'barcode': False,
                                                      'default_code': False})
        self.assertNotIn('codigoProductoEmpresa',
                         self.config._build_producto_payload(variante))

    # --- la descripción ----------------------------------------------
    def test_descripcion_lleva_los_atributos(self):
        variante = self._variante('Calzado Deportivo Dama')
        desc = self.config._build_producto_payload(variante)['descripcion']
        self.assertIn('Calzado Deportivo Dama', desc)
        self.assertIn(variante.product_template_attribute_value_ids._get_combination_name(), desc)

    def test_al_recortar_ceden_el_nombre_y_no_los_atributos(self):
        """🔴 Lo que distingue a la variante son los atributos: el recorte no
        puede llevárselos, que es lo que pasaba mandando el display_name."""
        variante = self._variante('N' * 90)
        desc = self.config._build_producto_payload(variante)['descripcion']
        atributos = variante.product_template_attribute_value_ids._get_combination_name()
        self.assertLessEqual(len(desc), 65)
        self.assertTrue(desc.endswith('(%s)' % atributos), desc)

    def test_descripcion_display_lleva_el_nombre_completo(self):
        variante = self._variante('Buzo de Felpa Cuello Base Estampado Sidney')
        payload = self.config._build_producto_payload(variante)
        self.assertEqual(payload['descripcionDisplay'],
                         variante.display_name[:100])
        self.assertIn(variante.display_name[:60], payload['descripcionDisplay'])

    # --- los dos armadores, uno solo ---------------------------------
    def test_insertar_producto_usa_el_mismo_payload(self):
        """Tenían copias separadas y se fueron separando: la individual mandaba
        siempre manejoIdentificador, que es campo de creación."""
        variante = self._variante()
        variante.with_context(_avoid_wms=True).write({'codigo_unico': 'PRD-TEST-1'})
        capturado = {}

        def fingir(self, link, body=None, params=None, method='GET'):
            capturado['body'] = body
            return {'numeroInterfaz': 'IF-1'}

        with patch(f'{MODELO_API}.consultarAPI', fingir):
            self.config.insertarProducto(variante)

        enviado = capturado['body']['productos'][0]
        self.assertEqual(enviado, self.config._build_producto_payload(variante))
        self.assertNotIn('manejoIdentificador', enviado)

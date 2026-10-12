# -*- coding: utf-8 -*-
"""Cola de productos: orden de salida, errores de WIS y el botón en segundo plano.

El orden es: lo pedido con el botón de la ficha (en el orden en que se apretó),
después lo que WIS rechazó, después lo que todavía no se mandó. Una variante
rechazada nunca queda «Integrado»: queda Pendiente con error. Un error general
(credenciales, conexión) no es culpa de ninguna variante y no suma intentos.
No golpean la red: se mockea el envío.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models.models import WisErrorGeneral

MODELO_API = 'odoo.addons.integracion_wis.models.models.IntegracionWIS'


@tagged('post_install', '-at_install', 'wis_productos')
class TestColaProductosPrioridad(TransactionCase):

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
        cls.config.write({'comunicacion_activa': True, 'cola_max_intentos': 5,
                          'chunk_size_productos': 200, 'cola_productos_por_corrida': 500,
                          'verificar_barcode_existente': False,
                          # Estos tests fijan el reintento automático de los rechazos;
                          # el comportamiento por defecto (sin reintento) está en
                          # test_reintentos_wis.
                          'reintentar_errores_datos': True,
                          'pausa_hasta': False, 'pausa_fallos': 0})
        cls.Cola = cls.env['wis.sync.queue']
        cls.Producto = cls.env['product.product']

    def _variantes(self, cantidad, nombre='Cola prioridad'):
        return self.Producto.create([{'name': f'{nombre} {i}', 'type': 'product'}
                                     for i in range(cantidad)])

    def _template(self, cantidad=2, nombre='Template botón'):
        atributo = self.env['product.attribute'].create({
            'name': f'Talle {nombre}',
            'value_ids': [(0, 0, {'name': f'T{i}'}) for i in range(cantidad)],
        })
        return self.env['product.template'].create({
            'name': nombre, 'type': 'product',
            'attribute_line_ids': [(0, 0, {'attribute_id': atributo.id,
                                           'value_ids': [(6, 0, atributo.value_ids.ids)]})],
        })

    def _envio_ok(self):
        """Un envío que WIS acepta: guarda el código como lo haría el real."""
        config = self.config

        def enviar(variantes, motivo=''):
            for v in variantes:
                if not v.codigo_unico:
                    v.with_context(_avoid_wms=True).codigo_unico = config._wis_codigo_producto(v)
            return {'enviados': len(variantes), 'errores': 0, 'errores_detalle': [],
                    'errores_por_codigo': {}, 'barcodes_enviados': 0}
        return patch.object(type(self.Producto), '_enviar_wms_en_lote', enviar)

    # ------------------------------------------------------------------
    # Orden de salida
    # ------------------------------------------------------------------
    def test_orden_boton_despues_error_despues_sin_enviar(self):
        sin_enviar = self.Cola._encolar(self._variantes(2, 'Sin enviar'), prioridad=1)
        con_error = self.Cola._encolar(self._variantes(2, 'Con error'), prioridad=10)
        con_error._registrar_fallo('rechazada')
        con_error.write({'proximo_intento': False})  # ya toca reintentar
        boton_a = self.Cola._encolar(self._variantes(1, 'Botón A'), origen='boton', prioridad=0)
        boton_b = self.Cola._encolar(self._variantes(1, 'Botón B'), origen='boton', prioridad=0)

        self.assertEqual(self.Cola._siguiente_lote(10), boton_a | boton_b)
        self.assertEqual(self.Cola._siguiente_lote(1), boton_a,
                         "Entre botones sale primero el que se apretó antes.")
        (boton_a | boton_b).write({'estado': 'hecho'})
        self.assertEqual(self.Cola._siguiente_lote(10), con_error)
        con_error.write({'estado': 'hecho'})
        self.assertEqual(self.Cola._siguiente_lote(10), sin_enviar)

    def test_con_error_espera_antes_de_reintentar(self):
        entrada = self.Cola._encolar(self._variantes(1))
        entrada._registrar_fallo('rechazada')
        self.assertGreater(entrada.proximo_intento, fields.Datetime.now())
        self.assertFalse(self.Cola._siguiente_lote(10) & entrada,
                         "Antes de los 15 minutos no se reintenta sola.")

    def test_agotados_los_intentos_sale_solo_con_reintentar(self):
        entrada = self.Cola._encolar(self._variantes(1))
        entrada.write({'subestado': 'error', 'intentos': 5, 'proximo_intento': False})
        self.assertFalse(self.Cola._siguiente_lote(10) & entrada)
        self.assertEqual(entrada.estado, 'pendiente')

        entrada.action_reintentar()
        self.assertEqual((entrada.intentos, entrada.subestado), (0, 'error'))
        self.assertEqual(self.Cola._siguiente_lote(10), entrada)

    def test_el_boton_adelanta_una_con_error_y_el_cron_cancela_la_vieja(self):
        variante = self._variantes(1)
        vieja = self.Cola._encolar(variante)
        vieja._registrar_fallo('rechazada')
        nueva = self.Cola._encolar(variante, origen='boton', prioridad=0)
        self.assertTrue(nueva)

        self.Cola._depurar_pendientes()
        self.assertEqual(vieja.estado, 'cancelado')
        self.assertEqual(nueva.estado, 'pendiente')

    def test_encolar_no_modifica_filas_existentes(self):
        """El cron puede estar escribiendo esas filas: tocarlas desde el pedido del
        usuario era el «could not serialize» que hacía repetir todo el botón."""
        variantes = self._variantes(2)
        viejas = self.Cola._encolar(variantes, prioridad=20)
        self.env.flush_all()
        sql = "SELECT id, ctid FROM wis_sync_queue WHERE id IN %s ORDER BY id"
        self.env.cr.execute(sql, [tuple(viejas.ids)])
        antes = self.env.cr.fetchall()

        self.Cola._encolar(variantes, origen='boton', prioridad=0)
        self.env.flush_all()
        self.env.cr.execute(sql, [tuple(viejas.ids)])
        self.assertEqual(self.env.cr.fetchall(), antes)

    # ------------------------------------------------------------------
    # Resultado del envío
    # ------------------------------------------------------------------
    def test_rechazada_queda_pendiente_con_error_nunca_integrada(self):
        variantes = self._variantes(2)
        entradas = self.Cola._encolar(variantes)
        rechazada = variantes[0]
        codigo = self.config._wis_codigo_producto(rechazada)
        config = self.config

        def enviar(vs, motivo=''):
            variantes[1].with_context(_avoid_wms=True).codigo_unico = \
                config._wis_codigo_producto(variantes[1])
            return {'enviados': 1, 'errores': 1, 'barcodes_enviados': 0,
                    'errores_detalle': [f"{codigo}: 400 - rechazado"],
                    'errores_por_codigo': {codigo: f"{codigo}: 400 - rechazado"}}

        with patch.object(type(self.Producto), '_enviar_wms_en_lote', enviar):
            entradas.procesar()

        mala = entradas.filtered(lambda e: e.product_id == rechazada)
        buena = entradas - mala
        self.assertEqual((mala.estado, mala.subestado, mala.intentos), ('pendiente', 'error', 1))
        self.assertEqual(buena.estado, 'hecho')

    def test_codigo_de_barras_rechazado_no_cuenta_como_integrada(self):
        """Antes el error del código de barras se guardaba sin el código de la
        variante y la cola la daba por integrada."""
        variante = self._variantes(1)
        variante.barcode = '7790000000017'

        def api(cfg, link, params, body, method='GET'):
            if 'CodigoBarras' in link:
                raise ValidationError("Error en la API: 400 - código de barras inválido")
            return {'codigoInterfaz': 500, 'numeroInterfaz': 1}

        with patch(f'{MODELO_API}.consultarAPI', api), \
             patch(f'{MODELO_API}._existe_barcode_codigo', return_value=False):
            resultado = self.config.insertarProductosMasivo(variante)

        codigo = variante.codigo_unico
        self.assertTrue(codigo, "El producto sí entró: tiene su código.")
        self.assertIn(codigo, resultado['errores_por_codigo'])
        self.assertIn('7790000000017', resultado['errores_por_codigo'][codigo])

    def test_error_general_no_reintenta_de_a_uno(self):
        """Credenciales o timeout: fallan todos igual; reintentar de a uno eran
        85 s de pantalla esperando en support (12-10-2026)."""
        variantes = self._variantes(3)
        with patch(f'{MODELO_API}.consultarAPI',
                   side_effect=WisErrorGeneral("Read timed out")) as api:
            with self.assertRaises(WisErrorGeneral):
                self.config.insertarProductosMasivo(variantes, enviar_barcodes=False)
        self.assertEqual(api.call_count, 1)

    def test_error_general_no_marca_ni_suma_intentos(self):
        entradas = self.Cola._encolar(self._variantes(2))

        def caido(*a, **k):
            raise WisErrorGeneral('invalid_client')

        with patch.object(type(self.Producto), '_enviar_wms_en_lote', caido):
            self.assertEqual(entradas.procesar(), 'general')

        self.assertEqual(set(entradas.mapped('estado')), {'pendiente'})
        self.assertEqual(set(entradas.mapped('subestado')), {'sin_enviar'})
        self.assertEqual(set(entradas.mapped('intentos')), {0})

    def test_credenciales_rechazadas_son_error_general(self):
        class Respuesta:
            status_code = 400
            text = '{"error": "invalid_client"}'

        self.config.write({'token': False})
        with patch('odoo.addons.integracion_wis.models.models.requests.post',
                   return_value=Respuesta()):
            with self.assertRaises(WisErrorGeneral):
                self.config.renovarToken()

    def test_log_de_variante_nueva_rechazada_dice_error(self):
        """Antes el log decía «success» porque buscaba el error por un código que
        la variante nueva todavía no tiene."""
        variante = self._variantes(1)
        codigo = self.config._wis_codigo_producto(variante)
        with patch(f'{MODELO_API}.insertarProductosMasivo', return_value={
                'enviados': 0, 'errores': 1, 'barcodes_enviados': 0,
                'errores_detalle': [f"{codigo}: 400 - rechazado"],
                'errores_por_codigo': {codigo: f"{codigo}: 400 - rechazado"}}):
            variante._enviar_wms_en_lote(motivo='prueba')
        log = self.env['product.wms.log'].search([('product_id', '=', variante.id)],
                                                 order='id desc', limit=1)
        self.assertEqual(log.resultado, 'error')

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    def test_el_cron_toma_lo_del_boton_entre_tanda_y_tanda(self):
        """Un botón apretado a mitad de una corrida sale en la tanda siguiente,
        no cuando termina todo lo que ya estaba en cola."""
        self.config.chunk_size_productos = 2
        masivas = self._variantes(6, 'Masiva')
        self.Cola._encolar(masivas, origen='masiva', prioridad=10)
        del_boton = self._variantes(1, 'Apretada en el medio')
        tandas = []
        cola = self.Cola
        config = self.config

        def enviar(variantes, motivo=''):
            tandas.append(variantes)
            if len(tandas) == 1:
                cola._encolar(del_boton, origen='boton', prioridad=0)
            for v in variantes:
                if not v.codigo_unico:
                    v.with_context(_avoid_wms=True).codigo_unico = config._wis_codigo_producto(v)
            return {'enviados': len(variantes), 'errores': 0, 'errores_detalle': [],
                    'errores_por_codigo': {}, 'barcodes_enviados': 0}

        with patch.object(type(self.Producto), '_enviar_wms_en_lote', enviar):
            self.Cola._cron_procesar_cola()

        self.assertEqual(tandas[1], del_boton)
        self.assertFalse(self.Cola.search_count([('product_id', 'in', masivas.ids),
                                                 ('estado', '=', 'pendiente')]))

    def test_el_cron_corta_ante_un_error_general(self):
        self.config.chunk_size_productos = 1
        self.Cola._encolar(self._variantes(3))
        llamadas = []

        def caido(variantes, motivo=''):
            llamadas.append(1)
            raise WisErrorGeneral('invalid_client')

        with patch.object(type(self.Producto), '_enviar_wms_en_lote', caido):
            self.Cola._cron_procesar_cola()
        self.assertEqual(len(llamadas), 1)

    def test_procesando_de_una_corrida_cortada_vuelve_a_pendiente(self):
        entrada = self.Cola._encolar(self._variantes(1))
        entrada.write({'estado': 'procesando'})
        self.env.flush_all()
        self.env.cr.execute("UPDATE wis_sync_queue SET write_date = %s WHERE id = %s",
                            [fields.Datetime.now() - timedelta(hours=1), entrada.id])
        entrada.invalidate_recordset()
        self.Cola._depurar_pendientes()
        self.assertEqual(entrada.estado, 'pendiente')

    def test_el_resultado_del_boton_queda_en_el_chatter(self):
        template = self._template(2, 'Template con chatter')
        template.with_context(_avoid_wms=True).write({'integracion_wms': True})
        template.enviar_variantes_wms()
        antes = len(template.message_ids)
        with self._envio_ok():
            self.Cola._cron_procesar_cola()
        cuerpos = template.message_ids[:len(template.message_ids) - antes].mapped('body')
        self.assertTrue(any('integradas' in (c or '') for c in cuerpos))

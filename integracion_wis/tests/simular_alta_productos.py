# -*- coding: utf-8 -*-
"""Simulación del circuito de alta de productos contra un WIS FALSO.

Qué prueba, de punta a punta y sin tocar la red:

    tildar «Integración con WMS» → cola → cron → envío por lote → codigo_unico

Se intercepta `requests` **dentro del módulo**, así que corre de verdad el
armado del payload, el troceado en chunks, la persistencia del `codigo_unico`,
el envío de códigos de barras y toda la contabilidad de la cola. Lo único que
no es real es el servidor.

No es un test unitario: es un ensayo a escala, para mirar los números. Los
tests están en `test_integracion_productos.py` (`--test-tags wis_productos`).

    .venv/bin/python3.11 <odoo-bin> shell -c forum.conf -d <base> \\
        --no-http --log-level=warn --max-cron-threads=0 \\
        < tests/simular_alta_productos.py

🔴 **Termina en `rollback`: no deja nada en la base.** El `commit` del cron se
neutraliza para eso; todo el resto del camino es el real. Aun así, corrélo en
una base descartable o de pruebas, nunca en producción.

Medido en `o17_support_forum` con 250 variantes (84 con código de barras):

    guardar                      0,03 s y 0 llamadas a WIS
    cron, sin rechazos           2 llamadas de producto (200 + 50) + 2 de barcodes
    cron, con 1 rechazo          202 llamadas: el lote de 200 cae y se reintenta
                                 uno por uno para aislar al que falla

Ese último número es el comportamiento deliberado de `_enviar_chunk_productos`:
un producto rechazado no puede hacer perder a los otros 199. Antes esos 202
requests ocurrían adentro del guardado del usuario; ahora ocurren en la cola.
"""
import json
import time

from odoo.addons.integracion_wis.models import models as mod_models
from odoo.addons.integracion_wis.models import Product as mod_product

N_VARIANTES = 250       # con chunk_size 200 son 2 lotes
SIMULAR_RECHAZO = True  # que WIS rechace una variante, para ver la contabilidad


class RespuestaFalsa:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class WisFalso:
    """Contesta como WIS y anota todo lo que recibe."""

    def __init__(self):
        self.llamadas = []
        self.productos_recibidos = []
        self.barcodes_recibidos = []
        self.codigo_que_rechaza = None

    def post(self, url, headers=None, data=None, timeout=None, **kw):
        self.llamadas.append(('token', 1))
        return RespuestaFalsa(200, {'access_token': 'token-falso', 'expires_in': 3600})

    def get(self, url, headers=None, timeout=None, **kw):
        return RespuestaFalsa(404, {})       # el barcode todavía no existe

    def request(self, url=None, method='GET', json=None, headers=None,
                params=None, timeout=None, **kw):
        cuerpo = json or {}
        if 'Producto/CreateOrUpdate' in url:
            codigos = [p.get('codigo') for p in cuerpo.get('productos', [])]
            self.llamadas.append(('Producto/CreateOrUpdate', len(codigos)))
            if self.codigo_que_rechaza in codigos:
                return RespuestaFalsa(
                    400, {'mensaje': f"Producto {self.codigo_que_rechaza} rechazado"})
            self.productos_recibidos.extend(codigos)
            return RespuestaFalsa(200, {'ok': True})
        if 'CodigoBarras' in url:
            barras = cuerpo.get('codigosDeBarras', [])
            self.llamadas.append(('CodigoBarras/CreateUpdateOrDelete', len(barras)))
            self.barcodes_recibidos.extend(barras)
            return RespuestaFalsa(200, {'ok': True})
        self.llamadas.append((url, 0))
        return RespuestaFalsa(200, {'ok': True})


def resumir(llamadas):
    resumen = {}
    for endpoint, cantidad in llamadas:
        r = resumen.setdefault(endpoint, {'llamadas': 0, 'items': 0, 'tamaños': set()})
        r['llamadas'] += 1
        r['items'] += cantidad
        r['tamaños'].add(cantidad)
    return resumen


def correr(env):
    wis = WisFalso()
    mod_models.requests = wis
    mod_product.requests = wis

    config = env['integracion_wis.integracion_wis'].search(
        [('company_id', '=', env.company.id)], limit=1)
    if not config:
        print("No hay configuración de WIS en esta base.")
        return
    config.write({'comunicacion_activa': True, 'token': False, 'expiracionToken': False})
    print("chunk_size=%s | por corrida=%s | verificar_barcode=%s | max_intentos=%s"
          % (config.chunk_size_productos, config.cola_productos_por_corrida,
             config.verificar_barcode_existente, config.cola_max_intentos))

    atributo = env['product.attribute'].create({
        'name': 'Talle simulación WIS',
        'value_ids': [(0, 0, {'name': 'S%03d' % i}) for i in range(N_VARIANTES)],
    })
    template = env['product.template'].create({
        'name': 'Plantilla simulación WIS',
        'type': 'product',
        'attribute_line_ids': [(0, 0, {'attribute_id': atributo.id,
                                       'value_ids': [(6, 0, atributo.value_ids.ids)]})],
    })
    variantes = template.product_variant_ids
    for i, v in enumerate(variantes):
        if i % 3 == 0:
            v.with_context(_avoid_wms=True).write({'barcode': '779%010d' % v.id})
    env.flush_all()
    print("plantilla con %d variantes (%d con barcode)"
          % (len(variantes), len(variantes.filtered('barcode'))))

    # 1. El guardado: tiene que volver en el acto y sin tocar la red.
    antes = len(wis.llamadas)
    t0 = time.time()
    template.write({'integracion_wms': True})
    env.flush_all()
    dt = time.time() - t0
    Cola = env['wis.sync.queue']
    pendientes = Cola.search([('product_id', 'in', variantes.ids),
                              ('estado', '=', 'pendiente')])
    print("\n1) GUARDAR: %.2fs | llamadas a WIS durante el guardado: %d | en cola: %d"
          % (dt, len(wis.llamadas) - antes, len(pendientes)))
    if len(wis.llamadas) != antes:
        print("   🔴 FALLA: el guardado llamó a WIS.")

    # 2. El cron.
    if SIMULAR_RECHAZO:
        wis.codigo_que_rechaza = config._wis_codigo_producto(variantes[7])
        print("   (WIS va a rechazar %s, a propósito)" % wis.codigo_que_rechaza)
    env.cr.commit = lambda: None        # el cron commitea por grupo; acá no
    t0 = time.time()
    Cola._cron_procesar_cola()
    print("\n2) CRON: %.1fs" % (time.time() - t0))
    for endpoint, r in resumir(wis.llamadas).items():
        print("   %-34s %4d llamada(s), %4d item(s)  tamaños: %s"
              % (endpoint, r['llamadas'], r['items'],
                 ', '.join(str(t) for t in sorted(r['tamaños'], reverse=True))))

    # 3. Qué quedó.
    entradas = Cola.search([('product_id', 'in', variantes.ids)])
    por_estado = {}
    for e in entradas:
        por_estado[e.estado] = por_estado.get(e.estado, 0) + 1
    variantes.invalidate_recordset()
    template.invalidate_recordset(['wis_variantes_en_cola'])
    print("\n3) RESULTADO")
    print("   cola: %s" % por_estado)
    print("   variantes con codigo_unico: %d de %d"
          % (len(variantes.filtered('codigo_unico')), len(variantes)))
    print("   recibió WIS: %d producto(s), %d barcode(s)"
          % (len(wis.productos_recibidos), len(wis.barcodes_recibidos)))
    print("   contador de la ficha: %d" % template.wis_variantes_en_cola)
    print("   logs WIS: %d"
          % env['product.wms.log'].search_count([('product_id', 'in', variantes.ids)]))

    if SIMULAR_RECHAZO:
        fallada = Cola.search([('product_id', '=', variantes[7].id)])
        print("   la rechazada -> estado=%s intentos=%s\n     error: %s"
              % (fallada.estado, fallada.intentos, (fallada.ultimo_error or '')[:90]))
        print("\n4) REINTENTO (ya sin el rechazo)")
        wis.codigo_que_rechaza = None
        fallada.action_reintentar()
        Cola._cron_procesar_cola()
        fallada.invalidate_recordset()
        variantes[7].invalidate_recordset()
        print("   estado=%s | codigo_unico=%s"
              % (fallada.estado, variantes[7].codigo_unico))

    env.cr.rollback()
    print("\nrollback hecho: la base queda como estaba")


correr(env)  # noqa: F821  (`env` lo inyecta `odoo-bin shell`)

# -*- coding: utf-8 -*-
"""Simulación de la conciliación de stock por fases, contra un WIS FALSO.

    armar tabla → consultar WIS por tandas → resumen de diferencias

Se intercepta `requests` dentro del módulo, así que corre de verdad el armado
de la tabla, el troceado en tandas, el puntero y la contabilidad. Lo único que
no es real es el servidor.

    .venv/bin/python3.11 <odoo-bin> shell -c forum.conf -d <base> \\
        --no-http --log-level=warn --max-cron-threads=0 \\
        < tests/simular_conciliacion_stock.py

🔴 Termina en rollback: no deja nada. Aun así, corrélo en una base de pruebas.

Lo que hay que mirar: que las variantes que WIS **no contesta** queden fuera
del ajuste. Un silencio tomado como cero pone en cero stock de verdad.
"""
import json
import random
import time

from odoo.addons.integracion_wis.models import models as mod_models

TANDA = 200
PCT_SIN_RESPUESTA = 3       # % de variantes que WIS no contesta, a propósito
MS_POR_REQUEST = 0          # subilo para simular latencia real de WIS


class RespuestaFalsa:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class WisFalso:
    def __init__(self):
        self.requests = 0
        self.sin_respuesta = 0

    def post(self, url, **kw):
        return RespuestaFalsa(200, {'access_token': 'falso', 'expires_in': 3600})

    def get(self, url, **kw):
        return RespuestaFalsa(404, {})

    def request(self, url=None, method='GET', json=None, params=None, **kw):
        if 'Producto/GetProducto' in url:
            self.requests += 1
            if MS_POR_REQUEST:
                time.sleep(MS_POR_REQUEST / 1000.0)
            if random.random() < PCT_SIN_RESPUESTA / 100.0:
                self.sin_respuesta += 1
                return RespuestaFalsa(500, {'detail': 'WIS no disponible'})
            # Stock cualquiera, con diferencias contra Odoo.
            return RespuestaFalsa(200, {'cantidadGenerica': random.choice(
                [0, 0, 1, 5, 12, 40])})
        return RespuestaFalsa(200, {'ok': True})


def correr(env):
    random.seed(42)
    wis = WisFalso()
    mod_models.requests = wis

    config = env['integracion_wis.integracion_wis']._get_config()
    if not config:
        print("No hay configuración de WIS en esta base.")
        return
    if not config.ubicacionReponerStock:
        ubic = env['stock.location'].search([('usage', '=', 'internal')], limit=1)
        config.ubicacionReponerStock = ubic.id
        print("⚠ No había ubicación de reposición; para la prueba se usa %s" % ubic.display_name)
    config.write({'comunicacion_activa': True, 'token': False, 'expiracionToken': False})

    conc = env['conciliacion.stock'].create({'name': 'Simulación por fases'})

    t0 = time.time()
    conc.action_armar_tabla()
    print("\n1) ARMAR TABLA: %.2fs | %d variantes | ubicación: %s"
          % (time.time() - t0, conc.cs_total, config.ubicacionReponerStock.display_name))

    conc.cs_batch_size = TANDA
    conc.write({'fase': 'consultando'})
    env.cr.commit = lambda: None          # el cron commitea por tanda; acá no
    t0 = time.time()
    tandas = 0
    while True:
        quedan = conc._cs_consultar_tanda()
        tandas += 1
        if not quedan:
            break
    dt = time.time() - t0
    print("\n2) CONSULTA A WIS: %.1fs en %d tanda(s) de %d | %d requests | %.1f ms/variante"
          % (dt, tandas, TANDA, wis.requests, 1000.0 * dt / max(conc.cs_total, 1)))

    conc._cs_finalizar_consulta()
    r = conc._cs_resumen()
    print("\n3) RESUMEN")
    print("   consultadas OK      : %d" % r['consultadas'])
    print("   con diferencia      : %d  (mínimo configurado: %s)"
          % (r['con_diferencia'], config.diferenciaMinima))
    print("   SIN respuesta de WIS: %d  -> quedan FUERA del ajuste" % r['sin_respuesta'])
    print("   quedarían en cero   : %d  <- el número a mirar antes de aplicar" % r['a_cero'])

    env.cr.execute("""SELECT estado, count(*) FROM %s GROUP BY estado ORDER BY 2 DESC"""
                   % conc._cs_tabla())
    print("   estados en la tabla : %s" % dict(env.cr.fetchall()))

    env.cr.execute("""SELECT count(*) FROM %s
                       WHERE estado = 'sin_respuesta'
                         AND (cantidad_wis IS NOT NULL OR diferencia IS NOT NULL)"""
                   % conc._cs_tabla())
    fugas = env.cr.fetchone()[0]
    print("\n   INVARIANTE · filas sin respuesta con cantidad asumida: %d %s"
          % (fugas, "✔" if fugas == 0 else "🔴 FALLA"))

    env.cr.rollback()
    print("\nrollback hecho: la base queda como estaba")


correr(env)  # noqa: F821

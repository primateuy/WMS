# -*- coding: utf-8 -*-
"""Prueba de aceptación del requerimiento "Integración de Productos con WIS".

Recorre el circuito completo del documento:

    Orden de Compra
    -> Detección de variantes no integradas
    -> Advertencia al operador
    -> Integración prioritaria de las variantes necesarias
    -> Confirmación de la OC
    -> Integración del resto de variantes sin bloquear la operatoria

Se corre con `odoo-bin shell`; la capa HTTP se mockea, no se golpea a WIS:

    .venv/bin/python odoo-bin shell --config forum.conf \
        -d <BD> --log-level=warn < tests/run_e2e_oc_productos.py

IMPORTANTE: el cron de la cola hace `env.cr.commit()` —como corresponde a un
cron—, así que el rollback final NO alcanza para deshacer todo. Correr contra
una BD descartable, o limpiar después el template "Remera E2E WIS", el partner
"Proveedor E2E WIS", el atributo "Talle E2E WIS" y su OC.
"""
from unittest.mock import patch
MODELO_API = 'odoo.addons.integracion_wis.models.models.IntegracionWIS'
llamadas = {'n': 0}

def _fake(self, link, body, params=None, method='GET'):
    llamadas['n'] += 1
    print(f"   [HTTP] {method} {link} -> {len(body.get('productos', body.get('codigosDeBarras', [])))} item(s)")
    return {}

ok = lambda c: print(f"  OK  {c}")
def check(cond, msg):
    assert cond, f"FALLO: {msg}"
    ok(msg)

cfg = env['integracion_wis.integracion_wis']._get_config()
if not cfg:
    cfg = env['integracion_wis.integracion_wis'].create({
        'client_id': 'x', 'client_secret': 'x', 'empresa_id': 1,
        'url_access_token': 'https://e.invalid/t', 'apiLink': 'https://e.invalid/api',
        'company_id': env.company.id})
cfg.write({'comunicacion_activa': True, 'exigir_integracion_oc': False,
           'bloquear_oc_sin_integrar': False, 'chunk_size_productos': 200})

print("\n== Escenario del requerimiento: OC con 5 de 100 variantes sin integrar ==")

# Template con 100 variantes via atributo
attr = env['product.attribute'].create({'name': 'Talle E2E WIS'})
vals = env['product.attribute.value'].create([
    {'name': f'T{i}', 'attribute_id': attr.id} for i in range(100)])

with patch(f'{MODELO_API}.consultarAPI', _fake):
    tmpl = env['product.template'].create({
        'name': 'Remera E2E WIS',
        'type': 'product',
        'attribute_line_ids': [(0, 0, {
            'attribute_id': attr.id,
            'value_ids': [(6, 0, vals.ids)],
        })],
    })
    tmpl.with_context(_avoid_wms=True).integracion_wms = True
    tmpl.product_variant_ids.with_context(_avoid_wms=True).write({'integracion_wms': True})

variantes = tmpl.product_variant_ids
# Punto de partida del escenario: producto marcado para WMS con sus 100
# variantes TODAVIA sin integrar en WIS.
variantes.with_context(_avoid_wms=True).write({'codigo_unico': False})
check(len(variantes) == 100, f"el template tiene {len(variantes)} variantes")
check(not any(variantes.mapped('codigo_unico')), "ninguna variante tiene codigo WIS todavia")

# Tipo de operacion integrado
pt = env['stock.picking.type'].search([('code', '=', 'incoming'),
                                       ('company_id', '=', env.company.id)], limit=1)
pt.integracion_wms = True
proveedor = env['res.partner'].create({'name': 'Proveedor E2E WIS'})

las_cinco = variantes[:5]
po = env['purchase.order'].create({
    'partner_id': proveedor.id,
    'picking_type_id': pt.id,
    'order_line': [(0, 0, {'product_id': p.id, 'name': p.name, 'product_qty': 1,
                           'price_unit': 10, 'date_planned': '2026-01-01 00:00:00'})
                   for p in las_cinco],
})

print("\n-- 1. Deteccion --")
check(po.wis_recepcion_integrada, "la recepcion de la OC va a WIS")
check(po.wis_cantidad_pendientes == 5, f"detecta {po.wis_cantidad_pendientes} variantes pendientes (esperado 5)")

print("\n-- 2. Advertencia al confirmar --")
accion = po.button_confirm()
check(isinstance(accion, dict) and accion.get('res_model') == 'wis.integrar.variantes.wizard',
      "confirmar abre el wizard de advertencia en vez de seguir de largo")
check(po.state in ('draft', 'sent'), f"la OC NO se confirmo (state={po.state})")

wiz = env['wis.integrar.variantes.wizard'].browse(accion['res_id'])
check(wiz.cantidad_variantes == 5, "el wizard ofrece integrar las 5 de la orden")
check(wiz.cantidad_resto == 95, f"y deja {wiz.cantidad_resto} para segundo plano (esperado 95)")

print("\n-- 3. Integrar ahora --")
llamadas['n'] = 0
with patch(f'{MODELO_API}.consultarAPI', _fake):
    wiz.action_integrar_ahora()

check(llamadas['n'] <= 2, f"las 5 variantes se integraron en {llamadas['n']} llamada(s) HTTP, no 5+")
check(all(las_cinco.mapped('codigo_unico')), "las 5 variantes de la OC quedaron con codigo WIS")
check(po.state == 'purchase', f"la OC quedo confirmada (state={po.state})")

print("\n-- 4. El resto no bloquea --")
cola = env['wis.sync.queue'].search([('product_id', 'in', variantes.ids),
                                     ('estado', '=', 'pendiente')])
check(len(cola) == 95, f"las otras {len(cola)} variantes quedaron en cola (esperado 95)")

print("\n-- 5. El cron las procesa por lote --")
llamadas['n'] = 0
with patch(f'{MODELO_API}.consultarAPI', _fake):
    env['wis.sync.queue']._cron_procesar_cola()

restantes = variantes - las_cinco
check(all(restantes.mapped('codigo_unico')), "las 95 restantes quedaron integradas")
check(llamadas['n'] <= 2, f"y se hizo en {llamadas['n']} llamada(s) HTTP, no 95")
check(not env['wis.sync.queue'].search([('product_id', 'in', variantes.ids),
                                        ('estado', '=', 'pendiente')]),
      "la cola quedo vacia")

print("\n-- 6. Comparativa --")
print(f"   Circuito anterior: 100 variantes x 1 request = 100+ llamadas HTTP, bloqueando al usuario.")
print(f"   Circuito nuevo:    5 urgentes en 1 llamada + 95 en segundo plano.")

print("\n== TODO OK ==")
env.cr.rollback()
print("(rollback hecho: no queda nada en la base)")

# Arma log/video_casos_originales.json con las operaciones de los casos C1-C12 (por su origen).
import json
import os

CASOS_VIDEO = [
    ('C1', 'Reenvío de la misma preparación: se aplica una sola vez', ['OP/C1-1-']),
    ('C1T', '15 pedidos: WIS recibe respuesta enseguida y la cola procesa', ['OP/C1T-0-', 'OP/C1T-1-']),
    ('C2', 'Entrega unificada de tres pedidos: avanza sólo lo despachado', ['OP/C2-1-', 'OP/C2-2-', 'OP/C2-3-']),
    ('C3', 'Crossdock: caja compartida entre dos pedidos', ['P-C3-A', 'P-C3-B']),
    ('C4', 'Despacho sin preparación previa', ['OP/C4-1-']),
    ('C5', 'Mayorista con teléfono: valida y despacha', ['SO_MAY-C5-']),
    ('C6', 'Pedido que sólo figura en los contenedores', ['OP/C6-A-', 'OP/C6-B-']),
    ('C7', 'Sucursal sin «Es receptor»: error claro y reproceso', ['OP/C7-1-']),
    ('C7B', 'Stock en dos paquetes: sin líneas en 0', ['OP/C7B-1-']),
    ('C8', 'Dos notificaciones simultáneas', ['OP/C8-A-', 'OP/C8-B-']),
    ('C9', 'Recepción cancelada desde la compra: se anula en WIS', []),
    ('C10', 'Fecha de entrega vencida: WIS acepta', ['OP/C10-1-']),
    ('C12', 'Dos tandas en dos camiones', ['OP/C12-1-']),
]
salida = []
for caso, titulo, origenes in CASOS_VIDEO:
    pks = env['stock.picking']
    for o in origenes:
        pks |= env['stock.move'].search([('origin', '=like', o + '%'), ('picking_type_id.integracion_wms', '=', True)]).picking_id
        pks |= env['stock.picking'].search([('origin', '=like', o + '%'), ('picking_type_id.integracion_wms', '=', True)])
    if caso == 'C9':
        pks = env['stock.picking'].search([('picking_type_id', '=', 1), ('wms_estado', '=', 'anulado')], limit=1, order='id desc')
    codigos = set(pks.mapped('codigo_unico'))
    pks |= env['stock.picking'].search([('codigo_unico', 'in', list(codigos)), ('wms_estado', '!=', 'no_integrado')])
    salida.append({'caso': caso, 'titulo': titulo, 'pickings': pks.ids[:6]})
ruta = os.path.join(os.path.dirname(BASE_SH), 'log', 'video_casos_originales.json')
json.dump(salida, open(ruta, 'w'), ensure_ascii=False, indent=1)
print(ruta, [(s['caso'], len(s['pickings'])) for s in salida])

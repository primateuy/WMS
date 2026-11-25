# -*- coding: utf-8 -*-
"""Recupera las líneas que WIS nunca recibió (relevamiento de producción, 24-11-2026).

Ver `models/stock_picking_recuperar.py`: las líneas se mueven a un pedido nuevo por sucursal,
que queda SIN ENVIAR a WIS hasta que Forum decida con cada uno: «Integrar con WMS» lo manda
(con su propio código W-P), «Cancelar» lo cancela junto con la entrega a la tienda, y una vez
cancelado se puede borrar. No se borran ni se duplican líneas.

Se corre con `odoo-bin shell` DESPUÉS de actualizar integracion_wis a 0.14.7. Sin `APLICAR`
sólo informa y deja el listado en CSV; no cambia nada:

    echo "APLICAR = False; exec(open('.../recuperar_lineas_no_enviadas.py').read())" \\
        | odoo-bin shell -c ... -d ... --no-http

Variables (todas opcionales, se definen antes del exec):

    GRUPO          'no_enviadas' (por defecto): líneas que WIS nunca recibió (grupo 3).
                   'no_preparadas': líneas de saldos que WIS recibió y no preparó (grupo 2).
                   'sin_registro': operaciones enviadas sin registro del envío (grupo 4).
                   Cada grupo arma sus propios pedidos. Nunca toma operaciones preparadas o
                   despachadas: ésas emiten e-Remito y van con la regularización.
    APLICAR        False: sólo informa. True: arma los pedidos sin enviar, grabando sucursal
                   por sucursal. No manda nada a WIS.
    SUCURSALES     ids o nombres de la ubicación destino (la salida de cada sucursal).
                   Vacío: todas. Para la prueba con una sola sucursal.
    DESDE, HASTA   'AAAA-MM-DD': sólo líneas creadas en ese período.
    SOLO_REP       sólo operaciones de la reposición en segundo plano. Por defecto True para
                   'no_enviadas' y False para los otros dos (el grupo 4 no tiene ninguna).
    TIPO_PEDIDO    'REP' (por defecto): sólo operaciones de tipos con ese tipo de pedido WIS
                   (en producción, «Preparo de Pedidos»). Vacío: todos los tipos integrados
                   que salen como pedido (crossdock, web, mayoristas...).
    MAX_LINEAS     tope de líneas por pedido nuevo (0: sin tope).
    DECISION_CSV   el listado devuelto por Forum: sólo se recuperan las filas con
                   `reenviar` = si/sí/x/1. Sin archivo: todas las del filtro.
    LISTADO_CSV    dónde dejar el listado (por defecto /tmp/recuperar_lineas_wis_<GRUPO>.csv).
    BORRAR_VACIAS  False (por defecto): las operaciones que quedan sin líneas sólo se listan.
                   True: se borran. 🔴 Es borrar registros: sólo con autorización explícita.
"""
import csv
import json

GRUPO = globals().get('GRUPO', 'no_enviadas')
APLICAR = globals().get('APLICAR', False)
SUCURSALES = globals().get('SUCURSALES', [])
DESDE = globals().get('DESDE')
HASTA = globals().get('HASTA')
SOLO_REP = globals().get('SOLO_REP', GRUPO == 'no_enviadas')
TIPO_PEDIDO = globals().get('TIPO_PEDIDO', 'REP')
MAX_LINEAS = globals().get('MAX_LINEAS', 0)
DECISION_CSV = globals().get('DECISION_CSV')
LISTADO_CSV = globals().get('LISTADO_CSV', '/tmp/recuperar_lineas_wis_%s.csv' % GRUPO)
BORRAR_VACIAS = globals().get('BORRAR_VACIAS', False)

Picking = env['stock.picking'].sudo()
Location = env['stock.location'].sudo()
GRUPOS = ('no_enviadas', 'no_preparadas', 'sin_registro')
assert GRUPO in GRUPOS, "GRUPO tiene que ser uno de %s" % (GRUPOS,)
RESUMEN = {'grupo': GRUPO, 'tipo_pedido': TIPO_PEDIDO, 'aplicar': APLICAR}


def _sucursales():
    if not SUCURSALES:
        return None
    ubicaciones = Location.browse()
    for s in SUCURSALES:
        if isinstance(s, int):
            ubicaciones |= Location.browse(s)
        else:
            ubicaciones |= Location.search([('complete_name', 'ilike', s)])
    return ubicaciones


def _decision():
    if not DECISION_CSV:
        return None
    elegidas = set()
    with open(DECISION_CSV, newline='', encoding='utf-8-sig') as f:
        for fila in csv.DictReader(f):
            if (fila.get('reenviar') or '').strip().lower() in ('si', 'sí', 's', 'x', '1'):
                elegidas.add(int(fila['move_id']))
    return elegidas


dominio = [('wms_estado', '=', 'enviado'), ('state', 'not in', ('done', 'cancel')),
           ('codigo_unico', '!=', False), ('picking_type_id.integracion_wms', '=', True)]
if SOLO_REP:
    dominio.append(('origin', '=like', 'REP/%'))
if TIPO_PEDIDO:
    dominio.append(('picking_type_id.tipo_pedido_wis', '=', TIPO_PEDIDO))
operaciones = Picking.search(dominio, order='id')
grupos, sin_revisar = operaciones._wis_clasificar_lineas()
moves = grupos[GRUPO]

ubicaciones = _sucursales()
if ubicaciones is not None:
    moves = moves.filtered(lambda m: m.location_dest_id in ubicaciones)
if DESDE:
    moves = moves.filtered(lambda m: str(m.create_date) >= DESDE)
if HASTA:
    moves = moves.filtered(lambda m: str(m.create_date)[:10] <= HASTA)
elegidas = _decision()
if elegidas is not None:
    moves = moves.filtered(lambda m: m.id in elegidas)

with open(LISTADO_CSV, 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['sucursal', 'fecha_linea', 'operacion', 'codigo_wis', 'situacion', 'proceso',
                'producto', 'descripcion', 'cantidad', 'reservada', 'grupo', 'move_id', 'reenviar'])
    for m in moves.sorted(lambda m: (m.location_dest_id.complete_name or '', m.id)):
        w.writerow([m.location_dest_id.complete_name, str(m.create_date)[:10], m.picking_id.name,
                    m.picking_id.codigo_unico,
                    'saldo' if m.picking_id.backorder_id else 'pedido abierto',
                    m.picking_id.origin, m.product_id.codigo_unico, m.product_id.display_name,
                    m.product_uom_qty, 'si' if m.move_line_ids else '', GRUPO, m.id, ''])

por_sucursal = {}
for m in moves:
    por_sucursal.setdefault(m.location_dest_id, env['stock.move'])
    por_sucursal[m.location_dest_id] |= m
RESUMEN.update({
    'operaciones_revisadas': len(operaciones),
    'operaciones_sin_poder_revisar': len(sin_revisar),
    'lineas_a_recuperar': len(moves),
    'unidades_a_recuperar': sum(moves.mapped('product_uom_qty')),
    'operaciones_de_origen': len(moves.picking_id),
    'sucursales': {u.complete_name: {'lineas': len(ms), 'unidades': sum(ms.mapped('product_uom_qty'))}
                   for u, ms in sorted(por_sucursal.items(), key=lambda x: x[0].complete_name or '')},
    'listado': LISTADO_CSV,
})

if APLICAR:
    hechas, fallidas = {}, {}
    origenes = moves.picking_id
    for ubicacion, ms in sorted(por_sucursal.items(), key=lambda x: x[0].complete_name or ''):
        nombre = ubicacion.complete_name
        try:
            nuevas = Picking._wis_recuperar_lineas(ms, max_lineas=MAX_LINEAS, grupo=GRUPO)
            env.cr.commit()
            hechas[nombre] = ["%s (%s, %s líneas)" % (n.name, n.codigo_unico or 'sin enviar',
                                                       len(n.move_ids)) for n in nuevas]
        except Exception as e:
            env.cr.rollback()
            env.invalidate_all()
            fallidas[nombre] = str(e)[:300]
    vacias = origenes.exists().filtered(lambda p: not p.move_ids)
    RESUMEN.update({'pedidos_nuevos': hechas, 'sucursales_con_error': fallidas,
                    'operaciones_que_quedaron_vacias': vacias.mapped('name')})
    if BORRAR_VACIAS and vacias:
        nombres = vacias.mapped('name')
        vacias.with_context(skip_wms_integration=True).unlink()
        env.cr.commit()
        RESUMEN['operaciones_vacias_borradas'] = nombres
else:
    env.cr.rollback()

print(json.dumps(RESUMEN, ensure_ascii=False, indent=1, default=str))

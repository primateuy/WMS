# Casos de producción WIS (08 al 22-10-2026) replicados sobre una copia local de la base.
# Se ejecuta dentro de `odoo-bin shell` con la variable CASOS (lista de casos a correr).
# Cada caso arma su escenario con el flujo real de Odoo, manda a /webhook/wis/callback
# notificaciones con la forma de las reales de producción y verifica el resultado CORRECTO
# (el que debería dar). Con integracion_wis 0.9.7 los casos dan ROJO como en producción.
import itertools
import json
import os
import threading
import time

import requests

from odoo import fields

BASE = os.path.dirname(BASE_SH)  # BASE_SH lo define run.py / correr.sh
URL = os.environ.get('CASOS_WIS_URL', 'http://127.0.0.1:8099/webhook/wis/callback')
UCFE_LOG = BASE + '/log/ucfe_requests.jsonl'
WIS_LOG = BASE + '/log/wis_requests.jsonl'
HOY = fields.Date.today().strftime('%d/%m/%Y')

POLO = env['stock.warehouse'].search([('code', '=', 'POLO')], limit=1)
POLO_STOCK = POLO.lot_stock_id
_nie = itertools.count(int(time.time()) % 1000000 * 100)
_cont = itertools.count(int(time.time()) % 100000 * 10)
_prep = itertools.count(int(time.time()) % 100000 * 10)
RESULTADOS = []
CORRIDA = time.strftime('%H%M%S')


# ---------------------------------------------------------------- utilidades
def refrescar():
    env.cr.commit()
    env.invalidate_all()


def productos(n, offset):
    return env['product.product'].search([('codigo_unico', 'like', 'PRD-%'), ('type', '=', 'product'),
                                          ('active', '=', True)], order='id', limit=n, offset=offset)


def stock(prods, qty=500, ubic=None):
    Q = env['stock.quant'].sudo()
    for p in prods:
        Q._update_available_quantity(p, ubic or POLO_STOCK, qty)


def tienda(codigo):
    return env['stock.warehouse'].search([('code', '=', codigo)], limit=1)


def reponer(wh, lineas, origen):
    origen = f'{origen}-{CORRIDA}'
    PG = env['procurement.group']
    regla = env['stock.rule'].search([('location_dest_id', '=', wh.lot_stock_id.id),
                                      ('route_id.product_selectable', '=', True),
                                      ('route_id.name', 'ilike', 'suministrar')], limit=1)
    PG.run([PG.Procurement(p, q, p.uom_id, wh.lot_stock_id, origen, origen, env.company,
                           {'warehouse_id': wh, 'route_ids': regla.route_id,
                            'date_planned': fields.Datetime.now()}) for p, q in lineas])
    refrescar()
    moves = env['stock.move'].search([('origin', '=', origen), ('picking_type_id', '=', 3)])
    return moves.picking_id


def ruta_tienda(wh):
    return env['stock.rule'].search([('location_dest_id', '=', wh.lot_stock_id.id), ('route_id.product_selectable', '=', True),
                                     ('route_id.name', 'ilike', 'suministrar')], limit=1).route_id


def ubic_tienda(wh):
    """(Salida X en POLO, Tránsito a X) según la ruta real de la tienda."""
    reglas = ruta_tienda(wh).rule_ids
    return (reglas.filtered(lambda r: r.picking_type_id.id == 3).location_dest_id[:1],
            reglas.filtered(lambda r: r.picking_type_id.id == 2).location_dest_id[:1])


def siguiente(picking):
    return picking.move_ids.move_dest_ids.picking_id


def contenedor(pedido_qtys, prep=None, barras=None):
    """pedido_qtys: [(picking, producto, cantidad), ...] -> contenedor con la forma de WIS."""
    n = next(_cont)
    return {'Preparacion': prep or next(_prep), 'CodigoBarras': barras or f'WISCO-L{n}',
            'NumeroContenedor': n, 'IdExternoContenedor': str(n), 'TipoContenedor': 'W',
            'CantidadBultos': None, 'PesoReal': None, 'Precinto1': None, 'Precinto2': None,
            'Detalles': [{'Pedido': pk.codigo_unico, 'Producto': p.codigo_unico, 'Identificador': '*',
                          'CantidadPreparada': float(q), 'TipoAgente': 'CLI'} for pk, p, q in pedido_qtys]}


def _pedidos(pickings):
    return [{'Empresa': 6005, 'Pedido': p.codigo_unico, 'TipoPedido': 'REP', 'CodigoOrigen': 'API',
             'Detalles': [{'Producto': m.product_id.codigo_unico, 'CantidadProducto': m.product_uom_qty}
                          for m in p.move_ids]} for p in pickings]


def enviar(tipo, cuerpo, nie=None, timeout=900, esperar=True):
    nie = nie or next(_nie)
    payload = {'Id': tipo, 'NumeroInterfazEjecucion': nie, tipo: cuerpo}
    t0 = time.time()
    try:
        r = requests.post(URL, json=payload, timeout=timeout)
        res = r.json().get('result') if r.headers.get('Content-Type', '').startswith('application/json') else r.status_code
    except requests.exceptions.RequestException as e:
        res = f'sin respuesta: {e.__class__.__name__}'
    seg = time.time() - t0
    refrescar()
    if 'wis.notificacion' in env and esperar:
        res = esperar_notificacion(tipo, nie) or res
    return nie, res, seg


def esperar_notificacion(tipo, nie, limite=900):
    """Con el procesamiento en segundo plano, espera a que la cola termine la notificación."""
    tipo_c = tipo[:1].lower() + tipo[1:]
    t0 = time.time()
    while time.time() - t0 < limite:
        refrescar()
        n = env['wis.notificacion'].sudo().search([('tipo', '=', tipo_c), ('numero_interfaz', '=', nie)], limit=1)
        if n and n.estado not in ('pendiente', 'procesando'):
            return {'status': 500 if n.estado in ('con_errores', 'error') else 200, 'estado': n.estado,
                    'detail': n.errores or ''}
        time.sleep(2)
    return {'status': 'sin terminar'}


def preparada(pickings, contenedores, nie=None, pedidos=None):
    return enviar('ConfirmacionMercaderiaPreparada', {
        'FechaFacturacion': HOY, 'DescripcionCamion': 'Camión local',
        'Pedidos': _pedidos(pedidos if pedidos is not None else pickings), 'Contenedores': contenedores}, nie)


def despacho(pickings, contenedores, nie=None):
    return enviar('ConfirmacionPedido', {
        'FechaCierre': HOY, 'FechaFacturacion': HOY, 'DescripcionCamion': 'Camión local', 'Matricula': '1',
        'Transportadora': 1, 'Pedidos': _pedidos(pickings), 'Contenedores': contenedores}, nie)


def remitos_emitidos():
    if not os.path.exists(UCFE_LOG):
        return 0
    return sum(1 for l in open(UCFE_LOG) if '"tipo_mensaje": 310' in l and '"cod": "00"' in l)


def wis_llamadas(fragmento):
    if not os.path.exists(WIS_LOG):
        return []
    return [json.loads(l) for l in open(WIS_LOG) if fragmento in l]


def done_qty(pickings):
    return sum(m.quantity for m in pickings.move_ids if m.state == 'done')


def logs(picking=None, codigo=None, proceso=None):
    dom = []
    if picking:
        dom.append(('picking_id', 'in', picking.ids))
    if codigo:
        dom.append(('codigo_unico', '=', codigo))
    if proceso:
        dom.append(('proceso', '=', proceso))
    return env['wms.integracion.log'].sudo().search(dom)


def chequear(caso, descripcion, condicion, detalle=''):
    RESULTADOS.append((caso, descripcion, bool(condicion), detalle))
    print(f"  [{'OK ' if condicion else 'MAL'}] {caso} · {descripcion}" + (f" — {detalle}" if detalle else ''))


def por_codigo(codigo):
    return env['stock.picking'].search([('codigo_unico', '=', codigo), ('wms_estado', '!=', 'no_integrado')])

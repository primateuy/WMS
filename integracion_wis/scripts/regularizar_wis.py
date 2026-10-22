# -*- coding: utf-8 -*-
"""Regularización de lo que quedó trabado con integracion_wis <= 0.9.7 (relevamiento 22-10-2026).

Sin APLICAR sólo lista: no emite e-Remitos ni llama a WIS. Se corre con `odoo-bin shell` DESPUÉS de actualizar el módulo a 0.10.0, con la variable
`APLICAR` (False por defecto: sólo informa qué haría):

    echo "APLICAR = False; exec(open('.../regularizar_wis.py').read())" | odoo-bin shell -c ... -d ...

Pasos, en orden (cada uno se puede saltear con PASOS = {...}):

1. `preparados`  Operaciones que WIS preparó y Odoo no validó (receptor, línea en 0, asistente
                 de SMS): se validan con lo que tienen cargado. Lo que todavía no se puede
                 validar (por ejemplo, la sucursal sin «Es receptor») queda listado.
2. `despachos`   Despachos de WIS que Odoo no pudo registrar («No se encontró picking»,
                 «múltiples pickings», choque de transacciones): se reprocesan desde el log de
                 webhooks con la lógica nueva. Lo ya aplicado no se repite.
3. `cadenas`     Lo despachado que no llegó a la entrega a tienda/cliente (POLO/OUT unificados,
                 OUTCROSS con caja compartida): se avanza sólo lo despachado.
4. `recepciones` Recepciones canceladas en Odoo que WIS sigue teniendo abiertas: se anulan en WIS.
5. `duplicados`  SOLO INFORMA las operaciones hechas que duplican una preparación de WIS (e-Remitos
                 duplicados). Revertirlas requiere decisión contable (DGI).
"""
import json

APLICAR = globals().get('APLICAR', False)
PASOS = globals().get('PASOS', {'preparados', 'despachos', 'cadenas', 'recepciones', 'duplicados'})
Picking = env['stock.picking'].sudo()
Log = env['wis.webhook.log'].sudo()
Notif = env['wis.notificacion'].sudo()
RESUMEN = {}


def _commit():
    if APLICAR:
        env.cr.commit()
    else:
        env.cr.rollback()
    env.invalidate_all()


def paso_preparados():
    pend = Picking.search([('wms_estado', '=', 'preparado'), ('state', 'not in', ('done', 'cancel')),
                           ('codigo_unico', 'like', 'W-%')])
    ok, mal = [], []
    if not APLICAR:
        RESUMEN['preparados'] = {'se_validarian': pend.mapped('name')}
        return
    for p in pend:
        try:
            p.action_wis_reprocesar()
            ok.append(p.name)
            _commit()
        except Exception as e:
            env.cr.rollback()
            env.invalidate_all()
            mal.append((p.name, str(e)[:160]))
    RESUMEN['preparados'] = {'validados': ok, 'siguen_pendientes': mal}


def paso_despachos():
    fallidos = Log.search([('tipo', '=', 'confirmacionPedido'), ('estado', '=', 'error')], order='id')
    hechos = []
    for log in fallidos:
        try:
            payload = json.loads(log.request or '')
        except ValueError:
            continue
        nie = log.numero_interfaz_ejecucion or 0
        existente = Notif.search([('tipo', '=', 'confirmacionPedido'), ('numero_interfaz', '=', nie)], limit=1) if nie else Notif
        if existente and existente.estado == 'hecho':
            continue
        if not APLICAR:
            hechos.append((nie, 'se reprocesaría', (log.codigo_unico or '')[:200]))
            continue
        notif = existente or Notif._registrar('confirmacionPedido', nie, payload)[0]
        if existente:
            notif.action_reprocesar()
        notif._procesar(10 ** 12)
        hechos.append((nie, notif.estado, (notif.errores or '')[:200]))
        _commit()
    RESUMEN['despachos'] = hechos


def paso_cadenas():
    despachados = Picking.search([('wms_estado', '=', 'despachado'), ('state', '=', 'done')])
    trabados = despachados.filtered(lambda p: p.move_ids.filtered(lambda m: m.state == 'done').move_dest_ids.filtered(
        lambda d: d.state not in ('done', 'cancel') and d.picking_id
        and not d.picking_id.picking_type_id.integracion_wms))
    entregas = trabados.move_ids.move_dest_ids.picking_id.filtered(lambda d: d.state not in ('done', 'cancel'))
    errores = []
    if APLICAR and trabados:
        errores = trabados._wis_avanzar_cadena(origen='Regularización 0.10.0')
    _commit()
    RESUMEN['cadenas'] = {'despachados_con_entrega_pendiente': len(trabados),
                          'entregas': entregas.mapped('name'), 'errores': errores}


def paso_recepciones():
    canceladas = Picking.search([('state', '=', 'cancel'), ('wms_estado', '=', 'enviado'),
                                 ('picking_type_id.code', '=', 'incoming'), ('codigo_unico', '!=', False)])
    hechas, mal = [], []
    if not APLICAR:
        RESUMEN['recepciones'] = {'se_anularian_en_wis': canceladas.mapped(lambda r: f'{r.name} ({r.codigo_unico})')}
        return
    for r in canceladas:
        try:
            r._wis_anular_recepciones_en_wis()
            hechas.append(r.name)
            _commit()
        except Exception as e:
            env.cr.rollback()
            mal.append((r.name, str(e)[:160]))
    RESUMEN['recepciones'] = {'anuladas_en_wis': hechas, 'rechazadas': mal}


def paso_duplicados():
    """Compara, por pedido y producto, lo que WIS preparó (cajas del log) contra lo hecho en Odoo."""
    preparado = {}
    vistas = set()
    for log in Log.search([('tipo', '=', 'confirmacionMercaderiaPreparada')], order='id'):
        # Cada notificación cuenta una vez (un reenvío puede haber quedado registrado dos veces).
        if log.numero_interfaz_ejecucion and log.numero_interfaz_ejecucion in vistas:
            continue
        vistas.add(log.numero_interfaz_ejecucion)
        try:
            datos = json.loads(log.request or '').get('ConfirmacionMercaderiaPreparada') or {}
        except ValueError:
            continue
        for c in datos.get('Contenedores') or []:
            for d in c.get('Detalles') or []:
                k = (d.get('Pedido'), d.get('Producto'))
                preparado[k] = preparado.get(k, 0) + (d.get('CantidadPreparada') or 0)
    pedidos = {k[0] for k in preparado}
    excesos = []
    for pedido in pedidos:
        hechos = Picking.search([('codigo_unico', '=', pedido), ('wms_estado', '!=', 'no_integrado'),
                                 ('state', '=', 'done')], order='id')
        acumulado = {}
        for pk in hechos:
            exceso = 0
            for m in pk.move_ids.filtered(lambda m: m.state == 'done'):
                k = (pedido, m.product_id.codigo_unico)
                antes = acumulado.get(k, 0)
                acumulado[k] = antes + m.quantity
                exceso += max(0, min(m.quantity, acumulado[k] - preparado.get(k, 0)))
            if exceso:
                excesos.append((pedido, pk.name, pk.wms_estado, exceso))
    RESUMEN['duplicados'] = excesos


for paso in ('preparados', 'despachos', 'cadenas', 'recepciones', 'duplicados'):
    if paso in PASOS:
        globals()['paso_' + paso]()
print(json.dumps({'aplicar': APLICAR, **RESUMEN}, ensure_ascii=False, indent=1, default=str))

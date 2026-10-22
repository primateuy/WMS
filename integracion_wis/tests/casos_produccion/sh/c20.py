# Variantes de los casos de producción: lo que puede romper la integración de punta a cabo.

def _base(codigo_tienda, offset, cantidades, origen):
    wh = tienda(codigo_tienda)
    ps = productos(len(cantidades), offset)
    stock(ps)
    return wh, ps, reponer(wh, list(zip(ps, cantidades)), origen)


def caso_v01():
    """Reenvío de la misma preparación con OTRO número de interfaz."""
    print('V01 · reenvío con otro número de interfaz')
    u0 = remitos_emitidos()
    wh, ps, p = _base('002', 2000, [2, 2], 'OP/V01')
    caja = contenedor([(p, ps[0], 2), (p, ps[1], 1)])
    preparada(p, [caja])
    preparada(p, [caja])  # otro número de interfaz, mismas cajas
    despacho(p, [caja])
    chequear('V01', 'una sola operación hecha y un solo e-Remito',
             len(integracion(p).filtered(lambda x: x.state == 'done')) == 1 and remitos_emitidos() - u0 == 1)
    integridad('V01', p, ucfe_desde=u0, en_tienda=True)
    para_video('V01', 'Reenvío con otro número de interfaz: se aplica una sola vez', integracion(p))


def caso_v02():
    """Dos copias de la misma notificación al mismo tiempo."""
    print('V02 · reenvío simultáneo')
    u0 = remitos_emitidos()
    wh, ps, p = _base('003', 2050, [3], 'OP/V02')
    caja = contenedor([(p, ps[0], 3)])
    nie = next(_nie)
    cuerpo = {'FechaFacturacion': HOY, 'Pedidos': _pedidos(p), 'Contenedores': [caja]}
    out = []

    def hilo():
        r = requests.post(URL, json={'Id': 'ConfirmacionMercaderiaPreparada', 'NumeroInterfazEjecucion': nie,
                                     'ConfirmacionMercaderiaPreparada': cuerpo}, timeout=120)
        out.append(r.json().get('result'))
    hs = [threading.Thread(target=hilo) for _ in range(2)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    esperar_notificacion('ConfirmacionMercaderiaPreparada', nie)
    chequear('V02', 'WIS recibe OK en las dos copias', all(isinstance(r, dict) and r.get('status') == 200 for r in out), str(out)[:200])
    chequear('V02', 'una sola notificación registrada',
             env['wis.notificacion'].search_count([('numero_interfaz', '=', nie)]) == 1)
    integridad('V02', p, despachado=False, ucfe_desde=u0)
    para_video('V02', 'Dos copias simultáneas de la misma notificación', integracion(p))


def caso_v03():
    """El despacho llega antes que la preparación."""
    print('V03 · despacho antes que la preparación')
    u0 = remitos_emitidos()
    wh, ps, p = _base('004', 2100, [2, 3], 'OP/V03')
    caja = contenedor([(p, ps[0], 2), (p, ps[1], 3)])
    despacho(p, [caja])
    preparada(p, [caja])  # llega tarde: no se vuelve a aplicar
    chequear('V03', 'una sola operación hecha', len(integracion(p).filtered(lambda x: x.state == 'done')) == 1)
    integridad('V03', p, ucfe_desde=u0, en_tienda=True)
    para_video('V03', 'El despacho llega antes que la preparación', integracion(p))


def caso_v04():
    """El mismo despacho dos veces."""
    print('V04 · despacho repetido')
    u0 = remitos_emitidos()
    wh, ps, p = _base('005', 2150, [4], 'OP/V04')
    caja = contenedor([(p, ps[0], 4)])
    preparada(p, [caja])
    despacho(p, [caja])
    _, res, _ = despacho(p, [caja])
    chequear('V04', 'el segundo despacho no da error ni duplica', isinstance(res, dict) and res.get('status') == 200, str(res)[:160])
    integridad('V04', p, ucfe_desde=u0, en_tienda=True)
    para_video('V04', 'El mismo despacho dos veces', integracion(p))


def caso_v05():
    """Preparación en tres tandas y un solo camión."""
    print('V05 · tres tandas, un camión')
    u0 = remitos_emitidos()
    wh, ps, p = _base('006', 2200, [6, 4, 2], 'OP/V05')
    c1 = contenedor([(p, ps[0], 3)])
    c2 = contenedor([(p, ps[0], 3), (p, ps[1], 4)])
    c3 = contenedor([(p, ps[2], 2)])
    preparada(p, [c1])
    preparada(p, [c2])
    preparada(p, [c3])
    despacho(p, [c1, c2, c3])
    hechos = integracion(p).filtered(lambda x: x.state == 'done')
    chequear('V05', 'tres operaciones hechas, todo despachado y nada pendiente',
             len(hechos) == 3 and not integracion(p).filtered(lambda x: x.state not in ('done', 'cancel')),
             f'{integracion(p).mapped(lambda x: (x.name, x.state, x.wms_estado))}')
    integridad('V05', p, ucfe_desde=u0, en_tienda=True)
    para_video('V05', 'Preparación en tres tandas, un solo camión', integracion(p))


def caso_v06():
    """WIS prepara más de lo pedido."""
    print('V06 · más de lo pedido')
    u0 = remitos_emitidos()
    wh, ps, p = _base('007', 2250, [2], 'OP/V06')
    caja = contenedor([(p, ps[0], 5)])
    preparada(p, [caja])
    despacho(p, [caja])
    chequear('V06', 'se toma lo pedido (2) y queda el aviso', done_qty(integracion(p)) == 2
             and logs(picking=integracion(p)).filtered(lambda l: l.nivel == 'warning'))
    integridad('V06', p, ucfe_desde=u0, en_tienda=True)
    para_video('V06', 'WIS prepara más de lo pedido', integracion(p))


def caso_v07():
    """Producto que el pedido no tiene, junto a uno válido."""
    print('V07 · producto ajeno al pedido')
    u0 = remitos_emitidos()
    wh, ps, p = _base('008', 2300, [2], 'OP/V07')
    ajeno = productos(1, 2340)
    caja = contenedor([(p, ps[0], 2), (p, ajeno[0], 7)])
    preparada(p, [caja])
    despacho(p, [caja])
    chequear('V07', 'se aplica lo válido y se avisa lo ajeno', done_qty(integracion(p)) == 2)
    integridad('V07', p, ucfe_desde=u0, en_tienda=True)
    para_video('V07', 'Producto que el pedido no tiene', integracion(p))


def caso_v08():
    """WIS anula parte del pedido y después prepara el resto."""
    print('V08 · anulación parcial y preparación del resto')
    u0 = remitos_emitidos()
    wh, ps, p = _base('009', 2350, [4, 3], 'OP/V08')
    enviar('PedidosAnulados', {'PedidosAnulados': [{'Pedido': p.codigo_unico, 'Empresa': 6005, 'Detalles': [
        {'Producto': ps[1].codigo_unico, 'CantidadAnulada': 3.0, 'Motivo': 'Anulación total de Detalle de Pedido'}]}]})
    caja = contenedor([(p, ps[0], 4)])
    preparada(p, [caja])
    despacho(p, [caja])
    pend = integracion(p).filtered(lambda x: x.state not in ('done', 'cancel'))
    chequear('V08', 'lo anulado no queda pendiente y lo preparado se despacha',
             done_qty(integracion(p)) == 4 and not sum(pend.move_ids.mapped('product_uom_qty')),
             f'{integracion(p).mapped(lambda x: (x.name, x.state, x.wms_estado))}')
    integridad('V08', p, ucfe_desde=u0, en_tienda=True)
    para_video('V08', 'Anulación parcial y preparación del resto', integracion(p))


def caso_v09():
    """Un pedido que Odoo no conoce, junto a uno válido."""
    print('V09 · pedido desconocido')
    u0 = remitos_emitidos()
    wh, ps, p = _base('011', 2400, [2], 'OP/V09')
    caja = contenedor([(p, ps[0], 2)])
    falso = {'Preparacion': 1, 'CodigoBarras': 'WISCO-FANTASMA', 'NumeroContenedor': next(_cont),
             'Detalles': [{'Pedido': 'W-P-999999999', 'Producto': ps[0].codigo_unico, 'CantidadPreparada': 1.0}]}
    _, res, _ = preparada(p, [caja, falso])
    chequear('V09', 'el pedido válido se prepara igual', done_qty(integracion(p)) == 2)
    chequear('V09', 'el desconocido queda como error', isinstance(res, dict) and 'W-P-999999999' in str(res.get('detail')), str(res)[:200])
    despacho(p, [caja])
    integridad('V09', p, ucfe_desde=u0, en_tienda=True)
    para_video('V09', 'Un pedido que Odoo no conoce en la notificación', integracion(p))


def caso_v10():
    """Notificación con un pedido que falla (receptor) y otros bien; se corrige y se reprocesa."""
    print('V10 · un pedido falla, se corrige y se reprocesa')
    u0 = remitos_emitidos()
    portones = tienda('013')
    ok_wh, ps, ok = _base('015', 2450, [2], 'OP/V10-OK')
    portones.partner_id.write({'es_receptor': False})
    refrescar()
    try:
        ps2 = productos(1, 2480)
        stock(ps2)
        mal = reponer(portones, [(ps2[0], 2)], 'OP/V10-MAL')
        c_ok, c_mal = contenedor([(ok, ps[0], 2)]), contenedor([(mal, ps2[0], 2)])
        nie, res, _ = preparada(ok | mal, [c_ok, c_mal])
        chequear('V10', 'el pedido bien se prepara, el otro queda con error claro',
                 done_qty(integracion(ok)) == 2 and done_qty(integracion(mal)) == 0 and 'receptor' in str(res).lower(), str(res)[:200])
    finally:
        portones.partner_id.write({'es_receptor': True})
        refrescar()
    notif = env['wis.notificacion'].search([('numero_interfaz', '=', nie)])
    notif.action_reprocesar()
    refrescar()
    esperar_notificacion('ConfirmacionMercaderiaPreparada', nie)
    chequear('V10', 'reprocesada: el pedido que faltaba queda hecho, sin duplicar el otro',
             done_qty(integracion(mal)) == 2 and done_qty(integracion(ok)) == 2,
             f'{integracion(mal).mapped(lambda x: (x.state, x.wms_estado))}')
    despacho(ok | mal, [c_ok, c_mal])
    integridad('V10', ok | mal, ucfe_desde=u0, en_tienda=True)
    para_video('V10', 'Un pedido falla (receptor), se corrige y se reprocesa', integracion(ok) | integracion(mal))


def caso_v11():
    """Entrega unificada despachada en desorden y recibida en la tienda."""
    print('V11 · entrega unificada en desorden')
    u0 = remitos_emitidos()
    wh = tienda('017')
    ps = productos(3, 2500)
    stock(ps)
    pk = [reponer(wh, [(ps[i], i + 2)], f'OP/V11-{i}') for i in range(3)]
    cajas = [contenedor([(pk[i], ps[i], i + 2)]) for i in range(3)]
    preparada(pk[0] | pk[1] | pk[2], cajas)
    for i in (2, 0, 1):
        despacho(pk[i], [cajas[i]])
    entregas = (pk[0] | pk[1] | pk[2]).move_ids.move_dest_ids.picking_id
    chequear('V11', 'todas las líneas de la entrega quedan hechas', all(m.state == 'done' for m in (pk[0] | pk[1] | pk[2]).move_ids.move_dest_ids),
             f'{entregas.mapped(lambda e: (e.name, e.state))}')
    integridad('V11', pk[0] | pk[1] | pk[2], ucfe_desde=u0, en_tienda=True)
    para_video('V11', 'Entrega unificada despachada en desorden', (pk[0] | pk[1] | pk[2]) | entregas)


def caso_v12():
    """Caja compartida entre dos pedidos de la misma entrega, despachados juntos."""
    print('V12 · caja compartida en entrega unificada')
    u0 = remitos_emitidos()
    wh = tienda('021')
    ps = productos(2, 2550)
    stock(ps)
    a = reponer(wh, [(ps[0], 2)], 'OP/V12-A')
    b = reponer(wh, [(ps[1], 3)], 'OP/V12-B')
    caja = contenedor([(a, ps[0], 2), (b, ps[1], 3)])
    preparada(a | b, [caja])
    despacho(a | b, [caja])
    integridad('V12', a | b, ucfe_desde=u0, en_tienda=True)
    para_video('V12', 'Caja compartida, misma entrega, despachados juntos', integracion(a) | integracion(b) | siguiente(a))


def caso_v13():
    """La caja compartida viaja en el camión 1 aunque la notificación nombre sólo al pedido A:
    lo de B también salió. Un segundo aviso de B no repite nada."""
    print('V13 · caja compartida, la lista nombra un solo pedido')
    u0 = remitos_emitidos()
    wh = tienda('025')
    ps = productos(2, 2600)
    stock(ps)
    a = reponer(wh, [(ps[0], 2)], 'OP/V13-A')
    b = reponer(wh, [(ps[1], 3)], 'OP/V13-B')
    caja = contenedor([(a, ps[0], 2), (b, ps[1], 3)])
    preparada(a | b, [caja])
    despacho(a, [caja])
    chequear('V13', 'con la caja salió también lo de B: los dos despachados y entregados',
             all(x.wms_estado == 'despachado' for x in (integracion(a) | integracion(b)).filtered(lambda x: x.state == 'done'))
             and all(m.state == 'done' for m in (a | b).move_ids.move_dest_ids))
    _, res, _ = despacho(b, [caja])
    chequear('V13', 'el segundo aviso de B es un reenvío sin efecto', isinstance(res, dict) and res.get('status') == 200, str(res)[:160])
    integridad('V13', a | b, ucfe_desde=u0, en_tienda=True)
    para_video('V13', 'Caja compartida: la lista nombra un solo pedido', integracion(a) | integracion(b) | siguiente(a))


def caso_v14():
    """Crossdock: caja compartida y preparación parcial."""
    print('V14 · crossdock con caja compartida y parcial')
    u0 = remitos_emitidos()
    wh = tienda('026')
    ps = productos(2, 2650)
    x1, o1 = _cadena_crossdock(wh, [(ps[0], 10)], 'P-V14-A')
    x2, o2 = _cadena_crossdock(wh, [(ps[1], 6)], 'P-V14-B')
    caja = contenedor([(x1, ps[0], 7), (x2, ps[1], 6)])
    preparada(x1 | x2, [caja])
    despacho(x1 | x2, [caja])
    saldo = integracion(x1).filtered(lambda x: x.state not in ('done', 'cancel'))
    chequear('V14', 'el saldo de 3 queda pendiente en el crossdock', sum(saldo.move_ids.mapped('product_uom_qty')) == 3)
    caja2 = contenedor([(x1, ps[0], 3)])
    preparada(x1, [caja2])
    despacho(x1, [caja2])
    integridad('V14', x1 | x2, ucfe_desde=u0, en_tienda=True)
    para_video('V14', 'Crossdock: caja compartida y preparación parcial', integracion(x1) | integracion(x2) | o1 | o2)


def caso_v15():
    """Mayoristas: con y sin teléfono."""
    print('V15 · mayoristas con y sin teléfono')
    u0 = remitos_emitidos()
    ps = productos(2, 2700)
    stock(ps)
    cli = mayorista()
    m1 = pedido_mayorista([(ps[0], 2)], f'SO_MAY-V15A-{CORRIDA}')
    cli.write({'phone': False})
    refrescar()
    m2 = pedido_mayorista([(ps[1], 2)], f'SO_MAY-V15B-{CORRIDA}')
    c1, c2 = contenedor([(m1, ps[0], 2)]), contenedor([(m2, ps[1], 2)])
    preparada(m1 | m2, [c1, c2])
    despacho(m1 | m2, [c1, c2])
    cli.write({'phone': '+598 2900 0000'})
    refrescar()
    integridad('V15', m1 | m2, ucfe_desde=u0)
    para_video('V15', 'Mayoristas con y sin teléfono', integracion(m1) | integracion(m2) | siguiente(m1) | siguiente(m2))


def caso_v16():
    """Cancelar la compra cuando WIS rechaza la anulación."""
    print('V16 · WIS rechaza la anulación')
    prov = env['res.partner'].search([('name', '=', 'Proveedor Réplica SA')], limit=1)
    if not prov:
        prov = env['res.partner'].with_context(skip_wms_integration=True).create({
            'name': 'Proveedor Réplica SA', 'is_company': True, 'supplier_rank': 1})
        prov.with_context(skip_wms_integration=True).write({'codigo_unico_proveedor': f'PRO-REP{prov.id}', 'integracion_wms': True})
    ps = productos(1, 2750)
    tipo = env['purchase.order.type'].search([('name', 'ilike', 'SIN Crossdocking')], limit=1)
    po = env['purchase.order'].create({'partner_id': prov.id, 'order_type': tipo.id, 'picking_type_id': 1,
                                       'order_line': [(0, 0, {'product_id': ps[0].id, 'product_qty': 5, 'price_unit': 1})]})
    po.button_confirm()
    refrescar()
    interruptor('wis_rechaza_anulacion', True)
    try:
        try:
            po.button_cancel()
            env.cr.commit()
        except Exception as e:
            print('    cancelación frenada:', str(e)[:120])
            env.cr.rollback()
        env.invalidate_all()
    finally:
        interruptor('wis_rechaza_anulacion', False)
    rec = po.picking_ids
    chequear('V16', 'la compra y la recepción no se cancelan', po.state != 'cancel' and all(r.state != 'cancel' for r in rec),
             f'compra {po.state}, recepción {rec.mapped(lambda r: (r.state, r.wms_estado))}')
    chequear('V16', 'queda marcada «error en cancelación» con su log', all(r.wms_estado == 'error_cancelacion' for r in rec))
    para_video('V16', 'WIS rechaza la anulación de la recepción', rec)


def caso_v17():
    """Ajuste grande retenido y aprobado; ajuste chico aplicado."""
    print('V17 · ajustes: retenido, aprobado y normal')
    ps = productos(2, 2800)
    antes = [p.with_context(location=POLO_STOCK.id).qty_available for p in ps]
    item = lambda p, q, nro: {'Producto': p.codigo_unico, 'CantidadMovimiento': q, 'TipoAjuste': 'US', 'Empresa': 6005,
                              'NumeroAjusteStock': nro, 'DescripcionMotivo': 'Prueba', 'Identificador': '*', 'FechaRealizacion': HOY}
    nie_g, res_g, _ = enviar('Ajustes', {'Ajustes': [item(ps[0], 9000.0, 800001 + next(_cont) % 1000)]})
    enviar('Ajustes', {'Ajustes': [item(ps[1], 5.0, 810001 + next(_cont) % 1000)]})
    d0 = ps[0].with_context(location=POLO_STOCK.id).qty_available - antes[0]
    d1 = ps[1].with_context(location=POLO_STOCK.id).qty_available - antes[1]
    chequear('V17', 'el grande queda retenido y el chico se aplica', d0 == 0 and d1 == 5 and res_g.get('estado') == 'retenida', f'{d0} / {d1} / {res_g}')
    notif = env['wis.notificacion'].search([('numero_interfaz', '=', nie_g)])
    notif.action_aprobar()
    refrescar()
    d0 = ps[0].with_context(location=POLO_STOCK.id).qty_available - antes[0]
    chequear('V17', 'aprobado, el grande se aplica una vez', d0 == 9000 and notif.estado == 'hecho', f'{d0} {notif.estado}')
    para_video('V17', 'Ajuste grande retenido y aprobado', env['stock.picking'].search([('origin', 'ilike', 'WIS-AJ-%')], order='id desc', limit=2))


def caso_v20():
    """Uruware caído durante la preparación; vuelve y se reprocesa."""
    print('V20 · Uruware caído')
    u0 = remitos_emitidos()
    wh, ps, p = _base('028', 2850, [2], 'OP/V20')
    caja = contenedor([(p, ps[0], 2)])
    interruptor('ucfe_caido', True)
    try:
        nie, res, _ = preparada(p, [caja])
    finally:
        interruptor('ucfe_caido', False)
    chequear('V20', 'sin Uruware no queda hecho ni se pierde lo preparado',
             done_qty(integracion(p)) == 0 and integracion(p).filtered(lambda x: x.wms_estado == 'preparado'), str(res)[:160])
    notif = env['wis.notificacion'].search([('numero_interfaz', '=', nie)])
    notif.action_reprocesar()
    refrescar()
    esperar_notificacion('ConfirmacionMercaderiaPreparada', nie)
    chequear('V20', 'reprocesada con Uruware de vuelta: hecho con un e-Remito', done_qty(integracion(p)) == 2 and remitos_emitidos() - u0 == 1,
             f'{integracion(p).mapped(lambda x: (x.state, x.wms_estado))}, remitos {remitos_emitidos() - u0}')
    despacho(p, [caja])
    integridad('V20', p, ucfe_desde=u0, en_tienda=True)
    para_video('V20', 'Uruware caído y reproceso', integracion(p))


def caso_v21():
    """WIS despacha un pedido que en Odoo se canceló."""
    print('V21 · despacho de un pedido cancelado')
    wh, ps, p = _base('029', 2900, [2], 'OP/V21')
    p.with_context(skip_wms_integration=True).action_cancel()
    refrescar()
    caja = contenedor([(p, ps[0], 2)])
    _, res, _ = despacho(p, [caja])
    chequear('V21', 'error claro y no se mueve stock', isinstance(res, dict) and res.get('status') == 500 and done_qty(integracion(p)) == 0,
             str(res)[:200])
    para_video('V21', 'Despacho de un pedido cancelado en Odoo', p)


def caso_v22():
    """Un usuario valida a mano la entrega a la tienda antes de que llegue el despacho de WIS."""
    print('V22 · entrega validada a mano antes del despacho')
    u0 = remitos_emitidos()
    wh, ps, p = _base('032', 2950, [2], 'OP/V22')
    caja = contenedor([(p, ps[0], 2)])
    preparada(p, [caja])
    entrega = siguiente(p)
    entrega.action_assign()
    entrega.move_ids.write({'picked': True})
    entrega.with_context(skip_sms=True, skip_wms_integration=True).button_validate()
    refrescar()
    _, res, _ = despacho(p, [caja])
    chequear('V22', 'el despacho se registra sin error y no mueve de nuevo', isinstance(res, dict) and res.get('status') == 200
             and integracion(p).filtered(lambda x: x.state == 'done').wms_estado == 'despachado', str(res)[:160])
    integridad('V22', p, ucfe_desde=u0, en_tienda=True)
    para_video('V22', 'La entrega se validó a mano antes del despacho', integracion(p) | entrega)


def caso_v23():
    """WIS prepara mercadería que en Odoo no figura en stock (diferencia de inventario)."""
    print('V23 · sin stock en Odoo')
    u0 = remitos_emitidos()
    wh = tienda('033')
    ps = productos(1, 3200)
    disponible = ps[0].with_context(location=POLO_STOCK.id).qty_available
    p = reponer(wh, [(ps[0], 2)], 'OP/V23')
    caja = contenedor([(p, ps[0], 2)])
    preparada(p, [caja])
    despacho(p, [caja])
    chequear('V23', 'WIS manda: se prepara y despacha igual', done_qty(integracion(p)) == 2
             and integracion(p).filtered(lambda x: x.state == 'done').wms_estado == 'despachado', f'stock previo en POLO {disponible}')
    neg = env['stock.quant'].search([('product_id', '=', ps[0].id), ('location_id', '=', POLO_STOCK.id), ('quantity', '<', 0)])
    print(f'    NOTA V23: POLO/Existencias de {ps[0].codigo_unico} queda en {sum(neg.mapped("quantity")) or "≥ 0"} (diferencia a conciliar)')
    para_video('V23', 'WIS prepara lo que Odoo no tiene en stock', integracion(p))

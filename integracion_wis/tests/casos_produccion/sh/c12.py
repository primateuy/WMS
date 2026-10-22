def caso_c12():
    """Pedido preparado en dos tandas y despachado en dos camiones (producción: 88 despachados sin log propio)."""
    print('C12 · dos preparaciones, dos camiones')
    wh = tienda('001')  # Gaucho
    ps = productos(2, 1300)
    stock(ps)
    p = reponer(wh, [(ps[0], 4), (ps[1], 4)], 'OP/C12-1')
    prep1, prep2 = next(_prep), next(_prep)
    c1 = contenedor([(p, ps[0], 4)], prep=prep1)
    preparada(p, [c1])
    c2 = contenedor([(p, ps[1], 4)], prep=prep2)
    preparada(p, [c2])
    todos = por_codigo(p.codigo_unico)
    t1 = todos.filtered(lambda x: x.state == 'done' and ps[0] in x.move_ids.product_id)
    t2 = todos.filtered(lambda x: x.state == 'done' and ps[1] in x.move_ids.product_id)
    chequear('C12', 'cada preparación guarda su número de WIS', t1.wms_nro_preparacion == str(prep1) and t2.wms_nro_preparacion == str(prep2),
             f'{t1.wms_nro_preparacion!r} / {t2.wms_nro_preparacion!r}')
    # El camión 1 lleva sólo la primera tanda.
    despacho(p, [dict(c1, FechaExpedicion=HOY)])
    chequear('C12', 'el camión 1 despacha sólo la tanda 1', t1.wms_estado == 'despachado' and t2.wms_estado == 'preparado',
             f'tanda1 {t1.wms_estado}, tanda2 {t2.wms_estado}')
    despacho(p, [dict(c2, FechaExpedicion=HOY)])
    chequear('C12', 'el camión 2 despacha la tanda 2', t2.wms_estado == 'despachado', f'tanda2 {t2.wms_estado}')
    sin_log = (t1 | t2).filtered(lambda x: not logs(picking=x, proceso='confirmacionPedido'))
    chequear('C12', 'cada operación despachada tiene su propio log', not sin_log, f'sin log: {sin_log.mapped("name")}')
    avanzan = (t1 | t2).move_ids.move_dest_ids
    chequear('C12', 'la entrega a la tienda avanza con cada camión', all(m.state == 'done' for m in avanzan),
             ', '.join(sorted(set(avanzan.mapped('state')))))

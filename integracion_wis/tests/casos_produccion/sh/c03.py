def _cadena_crossdock(wh, prods_qtys, nombre):
    """Una OC de crossdock para una tienda: CROSS (402, se integra) -> OUTCROSS (313), encadenados."""
    entrada = env['stock.picking.type'].browse(402).default_location_src_id
    salida, transito = ubic_tienda(wh)
    grupo = env['procurement.group'].create({'name': nombre})
    stock([p for p, q in prods_qtys], 100, entrada)
    Picking = env['stock.picking']
    cross = Picking.create({
        'picking_type_id': 402, 'location_id': entrada.id, 'location_dest_id': salida.id,
        'partner_id': wh.partner_id.id, 'group_id': grupo.id, 'origin': f'{nombre} - Crossdock',
        'move_ids': [(0, 0, {'name': p.name, 'product_id': p.id, 'product_uom_qty': q, 'product_uom': p.uom_id.id,
                             'location_id': entrada.id, 'location_dest_id': salida.id, 'group_id': grupo.id})
                     for p, q in prods_qtys]})
    outx = Picking.with_context(skip_wms_integration=True).create({
        'picking_type_id': 313, 'location_id': salida.id, 'location_dest_id': transito.id,
        'partner_id': wh.partner_id.id, 'group_id': grupo.id, 'origin': f'{nombre} - Entrega',
        'move_ids': [(0, 0, {'name': m.product_id.name, 'product_id': m.product_id.id, 'product_uom_qty': m.product_uom_qty,
                             'product_uom': m.product_uom.id, 'location_id': salida.id, 'location_dest_id': transito.id,
                             'group_id': grupo.id, 'move_orig_ids': [(6, 0, m.ids)], 'procure_method': 'make_to_order'})
                     for m in cross.move_ids]})
    outx.with_context(skip_wms_integration=True).action_confirm()
    cross.action_confirm()
    cross.action_assign()
    refrescar()
    return cross, outx


def caso_c3():
    """Caja de WIS compartida entre pedidos de crossdock de la misma tienda (producción: 69 OUTCROSS)."""
    print('C3 · caja compartida en crossdock')
    wh = tienda('042')  # Carmelo
    ps = productos(2, 400)
    x1, o1 = _cadena_crossdock(wh, [(ps[0], 4)], 'P-C3-A')
    x2, o2 = _cadena_crossdock(wh, [(ps[1], 6)], 'P-C3-B')
    print('   ', x1.name, x1.codigo_unico, x1.wms_estado, '|', x2.name, x2.codigo_unico, x2.wms_estado, '|', o1.codigo_unico, o2.codigo_unico)
    caja = contenedor([(x1, ps[0], 4), (x2, ps[1], 6)], barras=f'WISCO-C3-{next(_cont)}')
    preparada(x1 | x2, [caja])
    _, res, _ = despacho(x1 | x2, [caja])
    chequear('C3', 'las dos preparaciones quedan despachadas', all(x.wms_estado == 'despachado' for x in por_codigo(x1.codigo_unico) | por_codigo(x2.codigo_unico)),
             str(res)[:120])
    chequear('C3', 'las dos entregas a tienda (OUTCROSS) quedan hechas', o1.state == 'done' and o2.state == 'done',
             f'{o1.name}:{o1.state}, {o2.name}:{o2.state}')
    err = logs(picking=o1 | o2).filtered(lambda l: l.resultado == 'error')
    chequear('C3', 'sin error «same package content»', not err.filtered(lambda l: 'same package' in (l.detalle or '')),
             (err[:1].detalle or '')[:120])

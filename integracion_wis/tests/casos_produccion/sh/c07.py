def caso_c7():
    """Receptor desmarcado en la sucursal (producción: Portones, 23 e-Remitos rechazados)."""
    print('C7 · sucursal sin «Es receptor»')
    wh = tienda('013')  # Portones
    wh.partner_id.write({'es_receptor': False})
    refrescar()
    try:
        ps = productos(1, 800)
        stock(ps)
        p = reponer(wh, [(ps[0], 2)], 'OP/C7-1')
        c = contenedor([(p, ps[0], 2)])
        preparada(p, [c])
        todos = por_codigo(p.codigo_unico)
        lg = logs(picking=todos).filtered(lambda l: l.resultado == 'error')
        txt = ' '.join(lg.mapped(lambda l: f'{l.texto} {l.detalle}'))
        chequear('C7', 'el error dice qué corregir (el contacto sin «Es receptor»)', 'receptor' in txt.lower() and wh.partner_id.name in txt,
                 txt[:160])
        chequear('C7', 'no se intentó emitir un e-Remito que UCFE iba a rechazar',
                 not any('omite encabezado' in l for l in open(UCFE_LOG).readlines()[-3:]) if os.path.exists(UCFE_LOG) else True)
        # Corregido el contacto, se reprocesa desde Odoo y queda bien.
        wh.partner_id.write({'es_receptor': True})
        refrescar()
        pend = todos.filtered(lambda x: x.state != 'done' and x.wms_estado == 'preparado')
        if pend and hasattr(pend, 'action_wis_reprocesar'):
            pend.action_wis_reprocesar()
            refrescar()
        chequear('C7', 'corregido el contacto, el pedido se valida desde Odoo', done_qty(por_codigo(p.codigo_unico)) == 2,
                 f'{por_codigo(p.codigo_unico).mapped(lambda x: (x.state, x.wms_estado))}')
    finally:
        wh.partner_id.write({'es_receptor': True})
        refrescar()


def caso_c7b():
    """Línea suelta en 0 junto a la buena (producción: CROSS/00375, 00384, 00389 y PICK/00061)."""
    print('C7B · línea en 0')
    wh = tienda('019')  # Canelones
    ps = productos(2, 1500)
    # El stock del producto A está repartido en dos paquetes (como lo deja una recepción en cajas).
    Q = env['stock.quant'].sudo()
    for n in (1, 2):
        paq = env['stock.quant.package'].create({'name': f'C7B-PAQ-{n}-{next(_cont)}'})
        Q._update_available_quantity(ps[0], POLO_STOCK, 6, package_id=paq)
    stock(ps[1:])
    p = reponer(wh, [(ps[0], 10), (ps[1], 4)], 'OP/C7B-1')
    p.action_assign()
    refrescar()
    lineas_a = p.move_line_ids.filtered(lambda l: l.product_id == ps[0])
    print('    líneas reservadas del producto A:', len(lineas_a), lineas_a.mapped('quantity'))
    c1 = contenedor([(p, ps[0], 10), (p, ps[1], 4)])
    preparada(p, [c1])
    todos = por_codigo(p.codigo_unico)
    lg = logs(picking=todos).filtered(lambda l: l.resultado == 'error')
    chequear('C7B', 'el pedido se valida (una línea en 0 no bloquea el e-Remito)', done_qty(todos) == 14,
             f'{todos.mapped(lambda x: (x.state, x.wms_estado))} {(lg[:1].detalle or "")[:90]}')

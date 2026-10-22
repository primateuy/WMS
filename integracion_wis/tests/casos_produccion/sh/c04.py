def caso_c4():
    """WIS despacha un pedido sin preparación previa en Odoo (producción: W-P-26167, «No se encontró picking»)."""
    print('C4 · despacho sin preparación')
    wh = tienda('035')  # Punta del Este
    ps = productos(2, 500)
    stock(ps)
    p = reponer(wh, [(ps[0], 3), (ps[1], 2)], 'OP/C4-1')
    remitos_antes = remitos_emitidos()
    c = contenedor([(p, ps[0], 3), (p, ps[1], 2)])
    _, res, _ = despacho(p, [c])
    todos = por_codigo(p.codigo_unico)
    chequear('C4', 'WIS recibe OK', isinstance(res, dict) and res.get('status') == 200, str(res)[:160])
    chequear('C4', 'el pedido queda hecho y despachado con lo que informó WIS',
             done_qty(todos) == 5 and all(x.wms_estado == 'despachado' for x in todos.filtered(lambda x: x.state == 'done')),
             f'hecho {done_qty(todos)}, estados {todos.mapped(lambda x: (x.state, x.wms_estado))}')
    chequear('C4', 'se emitió el e-Remito', remitos_emitidos() - remitos_antes == 1, f'{remitos_emitidos() - remitos_antes}')
    chequear('C4', 'la entrega a la tienda avanza', all(m.state == 'done' for m in p.move_ids.move_dest_ids),
             ', '.join(sorted(set(p.move_ids.move_dest_ids.mapped('state')))))

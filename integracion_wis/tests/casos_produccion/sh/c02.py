def caso_c2():
    """Entrega POLO/OUT que junta los pedidos de una tienda (producción: 39 entregas trabadas)."""
    print('C2 · entrega unificada')
    wh = tienda('039')  # Minas
    ps = productos(6, 100)
    stock(ps)
    p1 = reponer(wh, [(ps[0], 2), (ps[1], 2)], 'OP/C2-1')
    p2 = reponer(wh, [(ps[2], 2), (ps[3], 2)], 'OP/C2-2')
    p3 = reponer(wh, [(ps[4], 2), (ps[5], 2)], 'OP/C2-3')
    out = siguiente(p1) | siguiente(p2) | siguiente(p3)
    chequear('C2', 'los 3 pedidos caen en una sola entrega (igual que producción)', len(out) == 1,
             f'{len(out)} entrega(s), código {out[:1].codigo_unico}')
    # WIS prepara y despacha el pedido 2: no es el que le dio el código a la entrega.
    c2 = contenedor([(p2, ps[2], 2), (p2, ps[3], 2)])
    preparada(p2, [c2])
    despacho(p2, [c2])
    m2 = p2.move_ids.move_dest_ids
    chequear('C2', 'despacho del pedido 2: sus líneas de la entrega quedan hechas', all(m.state == 'done' for m in m2),
             ', '.join(sorted(set(m2.mapped('state')))))
    # WIS prepara el pedido 3 pero todavía no lo despacha; despacha el pedido 1.
    c3 = contenedor([(p3, ps[4], 2), (p3, ps[5], 2)])
    preparada(p3, [c3])
    c1 = contenedor([(p1, ps[0], 2), (p1, ps[1], 2)])
    preparada(p1, [c1])
    despacho(p1, [c1])
    m1 = p1.move_ids.move_dest_ids
    m3 = p3.move_ids.move_dest_ids
    chequear('C2', 'despacho del pedido 1: sus líneas quedan hechas', all(m.state == 'done' for m in m1),
             ', '.join(sorted(set(m1.mapped('state')))))
    chequear('C2', 'el pedido 3 (preparado, NO despachado) no se da por entregado', all(m.state != 'done' for m in m3),
             ', '.join(sorted(set(m3.mapped('state')))))
    chequear('C2', 'las líneas del pedido 3 no se cancelan', all(m.state != 'cancel' for m in m3))
    recep = (p1 | p2).move_ids.move_dest_ids.move_dest_ids
    chequear('C2', 'la tienda tiene lista la recepción de lo despachado', all(m.state == 'assigned' for m in recep),
             ', '.join(sorted(set(recep.mapped('state')))))

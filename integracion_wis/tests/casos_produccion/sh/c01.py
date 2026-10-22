def caso_c1():
    """Reenvío de la MISMA notificación de preparación (producción: 88 e-Remitos duplicados)."""
    print('C1 · reenvío de la misma preparación')
    wh = tienda('045')  # Melo
    ps = productos(2, 200)
    stock(ps)
    p = reponer(wh, [(ps[0], 2), (ps[1], 2)], 'OP/C1-1')
    # WIS prepara parcial: el producto A completo y 1 de 2 del B (queda saldo de B).
    c = contenedor([(p, ps[0], 2), (p, ps[1], 1)])
    remitos_antes = remitos_emitidos()
    nie, r1, _ = preparada(p, [c])
    nie, r2, _ = preparada(p, [c], nie=nie)  # el corte por tiempo hace que WIS reenvíe lo mismo
    todos = por_codigo(p.codigo_unico)
    hechos = todos.filtered(lambda x: x.state == 'done')
    chequear('C1', 'el reenvío no genera otra operación hecha', len(hechos) == 1, f'{len(hechos)} hecha(s): {hechos.mapped("name")}')
    chequear('C1', 'Odoo tiene hecho exactamente lo que WIS preparó (3)', done_qty(todos) == 3, f'hecho {done_qty(todos)}')
    chequear('C1', 'un solo e-Remito emitido', remitos_emitidos() - remitos_antes == 1,
             f'{remitos_emitidos() - remitos_antes} e-Remito(s)')
    saldo = todos.filtered(lambda x: x.state not in ('done', 'cancel'))
    pendiente = sum(saldo.move_ids.filtered(lambda m: m.product_id == ps[1]).mapped('product_uom_qty'))
    chequear('C1', 'el saldo conserva la unidad que WIS todavía tiene pendiente', pendiente == 1, f'pendiente {pendiente}')
    chequear('C1', 'WIS recibe OK en el reenvío (no un error)', isinstance(r2, dict) and r2.get('status') == 200, str(r2)[:160])


def caso_c1t():
    """Tiempo de respuesta del webhook con una notificación grande (producción: corte a los 600 s)."""
    print('C1T · tiempo de respuesta')
    open(BASE + '/log/ucfe_demora', 'w').write('1')  # ~1 s por e-Remito, como Uruware
    try:
        wh = tienda('046')
        ps = productos(15, 300)
        stock(ps)
        pedidos = [reponer(wh, [(ps[i], 2)], f'OP/C1T-{i}') for i in range(15)]
        conts = [contenedor([(pk, ps[i], 2)]) for i, pk in enumerate(pedidos)]
        nie, res, seg = enviar('ConfirmacionMercaderiaPreparada', {'FechaFacturacion': HOY, 'Pedidos': _pedidos(pedidos), 'Contenedores': conts}, esperar=False)
        chequear('C1T', 'WIS recibe respuesta en menos de 10 s (15 pedidos)', seg < 10, f'{seg:.0f} s')
        # El procesamiento tiene que terminar igual, aunque sea en segundo plano.
        for _ in range(60):
            if all(pk.state == 'done' for pk in pedidos):
                break
            time.sleep(5)
            refrescar()
        chequear('C1T', 'los 15 pedidos terminan preparados y validados', all(pk.state == 'done' for pk in pedidos),
                 f'{sum(pk.state == "done" for pk in pedidos)}/15')
    finally:
        open(BASE + '/log/ucfe_demora', 'w').write('0')

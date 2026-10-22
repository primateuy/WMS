def caso_c6():
    """Notificación con cajas de un pedido que no está en su lista Pedidos (producción: 642399)."""
    print('C6 · pedido sólo en los contenedores')
    wh = tienda('012')  # Cordón
    ps = productos(2, 700)
    stock(ps)
    a = reponer(wh, [(ps[0], 2)], 'OP/C6-A')
    b = reponer(wh, [(ps[1], 2)], 'OP/C6-B')
    ca = contenedor([(a, ps[0], 2)])
    cb = contenedor([(b, ps[1], 2)])
    preparada(a | b, [ca, cb], pedidos=a)  # la lista sólo nombra al pedido A
    tb = por_codigo(b.codigo_unico)
    lb = logs(codigo=b.codigo_unico) | logs(picking=tb)
    chequear('C6', 'el pedido B (sólo en las cajas) también se prepara', done_qty(tb) == 2, f'{tb.mapped(lambda x: (x.state, x.wms_estado))}')
    chequear('C6', 'queda registro del pedido B en el log', bool(lb.filtered(lambda l: l.proceso == 'confirmacionMercaderiaPreparada')),
             f'{len(lb)} registro(s)')

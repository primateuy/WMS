def caso_c8():
    """Dos notificaciones simultáneas sobre el mismo stock (producción: «could not serialize access», 19-10)."""
    print('C8 · notificaciones simultáneas')
    ps = productos(1, 900)
    stock(ps, 50)
    a = reponer(tienda('014'), [(ps[0], 2)], 'OP/C8-A')  # Colón
    b = reponer(tienda('024'), [(ps[0], 2)], 'OP/C8-B')  # Las Piedras Shopping
    ca = contenedor([(a, ps[0], 2)])
    cb = contenedor([(b, ps[0], 2)])
    out = {}

    def hilo(k, pk, c):
        out[k] = requests.post(URL, json={'Id': 'ConfirmacionMercaderiaPreparada', 'NumeroInterfazEjecucion': next(_nie),
                                          'ConfirmacionMercaderiaPreparada': {'FechaFacturacion': HOY, 'Pedidos': _pedidos(pk),
                                                                              'Contenedores': [c]}}, timeout=300).json().get('result')
    hs = [threading.Thread(target=hilo, args=('a', a, ca)), threading.Thread(target=hilo, args=('b', b, cb))]
    [h.start() for h in hs]
    [h.join() for h in hs]
    for _ in range(60):
        refrescar()
        if done_qty(por_codigo(a.codigo_unico)) == 2 and done_qty(por_codigo(b.codigo_unico)) == 2:
            break
        time.sleep(5)
    chequear('C8', 'las dos preparaciones quedan aplicadas', done_qty(por_codigo(a.codigo_unico)) == 2 and done_qty(por_codigo(b.codigo_unico)) == 2,
             f"a={out.get('a')} b={out.get('b')}"[:200])

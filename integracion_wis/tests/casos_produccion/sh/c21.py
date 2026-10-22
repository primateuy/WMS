# Variantes de robustez del servidor: corren con una conf de límites bajos (limit_time_real = 90).
import subprocess


def _servidor(accion):
    conf = os.environ['CASOS_CONF']
    if accion == 'matar':
        subprocess.run(['pkill', '-9', '-f', f'odoo-bin -c {conf}'])
        time.sleep(3)
        return
    entorno = dict(os.environ, OBJC_DISABLE_INITIALIZE_FORK_SAFETY='YES', no_proxy='*', NO_PROXY='*')
    subprocess.Popen([os.environ['CASOS_PYTHON'], 'odoo-bin', '-c', conf], cwd=os.environ['CASOS_ODOO'],
                     env=entorno, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        try:
            if requests.get(URL.replace('/webhook/wis/callback', '/web/login'), timeout=3).status_code == 200:
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(3)


def caso_v18():
    """Notificación de 30 pedidos que no entra en una corrida del cron (corte a los 90 s)."""
    print('V18 · notificación grande, varias corridas')
    u0 = remitos_emitidos()
    open(BASE + '/log/ucfe_demora', 'w').write('2')
    try:
        wh = tienda('030')
        ps = productos(30, 3000)
        stock(ps)
        pk = [reponer(wh, [(ps[i], 2)], f'OP/V18-{i}') for i in range(30)]
        cajas = [contenedor([(pk[i], ps[i], 2)]) for i in range(30)]
        todos = env['stock.picking'].browse([p.id for p in pk])
        nie, res, seg = enviar('ConfirmacionMercaderiaPreparada',
                               {'FechaFacturacion': HOY, 'Pedidos': _pedidos(todos), 'Contenedores': cajas}, esperar=False)
        chequear('V18', 'WIS recibe respuesta enseguida', seg < 10, f'{seg:.1f} s')
        res = esperar_notificacion('ConfirmacionMercaderiaPreparada', nie, limite=1200)
        notif = env['wis.notificacion'].search([('numero_interfaz', '=', nie)])
        chequear('V18', 'terminó en varias corridas, los 30 hechos', notif.estado == 'hecho' and notif.intentos >= 2
                 and all(p.state == 'done' for p in todos), f'{notif.estado}, {notif.intentos} corridas')
        integridad('V18', todos, despachado=False, ucfe_desde=u0)
        para_video('V18', 'Notificación de 30 pedidos en varias corridas del cron', todos[:3])
    finally:
        open(BASE + '/log/ucfe_demora', 'w').write('0')


def caso_v19():
    """El servidor muere a mitad del procesamiento; vuelve y retoma sin duplicar."""
    print('V19 · el servidor muere a mitad')
    u0 = remitos_emitidos()
    open(BASE + '/log/ucfe_demora', 'w').write('2')
    try:
        wh = tienda('031')
        ps = productos(12, 3100)
        stock(ps)
        pk = [reponer(wh, [(ps[i], 2)], f'OP/V19-{i}') for i in range(12)]
        cajas = [contenedor([(pk[i], ps[i], 2)]) for i in range(12)]
        todos = env['stock.picking'].browse([p.id for p in pk])
        nie, _, _ = enviar('ConfirmacionMercaderiaPreparada',
                           {'FechaFacturacion': HOY, 'Pedidos': _pedidos(todos), 'Contenedores': cajas}, esperar=False)
        time.sleep(15)
        _servidor('matar')
        refrescar()
        notif = env['wis.notificacion'].search([('numero_interfaz', '=', nie)])
        hechos_al_morir = sum(p.state == 'done' for p in todos)
        print(f'    al morir: {notif.estado}, {hechos_al_morir}/12 hechos')
        # Pasó el tiempo de «procesando colgada»: se simula moviendo la fecha.
        env.cr.execute("UPDATE wis_notificacion SET write_date = now() - interval '2 hours' WHERE id = %s", [notif.id])
        refrescar()
        _servidor('arrancar')
        env['wis.notificacion']._disparar_cron()
        refrescar()
        res = esperar_notificacion('ConfirmacionMercaderiaPreparada', nie, limite=900)
        chequear('V19', 'retomó y terminó los 12', all(p.state == 'done' for p in todos), f'{sum(p.state == "done" for p in todos)}/12, {res}')
        integridad('V19', todos, despachado=False, ucfe_desde=u0)
        para_video('V19', 'El servidor muere a mitad y retoma sin duplicar', todos[:3])
    finally:
        open(BASE + '/log/ucfe_demora', 'w').write('0')

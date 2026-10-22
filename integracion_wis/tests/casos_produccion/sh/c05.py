def mayorista():
    cli = env['res.partner'].search([('name', '=', 'Mayorista Réplica SA')], limit=1)
    if not cli:
        modelo = env['res.partner'].browse(373)
        cli = env['res.partner'].with_context(skip_wms_integration=True).create({
            'name': 'Mayorista Réplica SA', 'is_company': True, 'vat': modelo.vat,
            'l10n_latam_identification_type_id': modelo.l10n_latam_identification_type_id.id,
            'country_id': modelo.country_id.id, 'state_id': modelo.state_id.id, 'city': modelo.city,
            'street': 'Calle 1', 'es_receptor': True, 'customer_rank': 1})
        cli.with_context(skip_wms_integration=True).write({'codigo_unico_cliente': f'CLI-MAY{cli.id}', 'integracion_wms': True})
        refrescar()
    if not cli.phone:
        # Los clientes mayoristas de producción tienen teléfono (las sucursales no).
        cli.with_context(skip_wms_integration=True).write({'phone': '+598 2900 0000'})
        refrescar()
    return cli


def pedido_mayorista(prods_qtys, origen):
    PG = env['procurement.group']
    cli = mayorista()
    ruta = env['stock.route'].browse(217)
    grupo = PG.create({'name': origen, 'partner_id': cli.id})
    destino = env.ref('stock.stock_location_customers')
    PG.run([PG.Procurement(p, q, p.uom_id, destino, origen, origen, env.company,
                           {'warehouse_id': POLO, 'route_ids': ruta, 'group_id': grupo, 'partner_id': cli.id,
                            'date_planned': fields.Datetime.now()}) for p, q in prods_qtys])
    refrescar()
    return env['stock.move'].search([('origin', '=', origen), ('picking_type_id', '=', 351)]).picking_id


def caso_c5():
    """Pedido mayorista (producción: MAY/00011-00018 «preparados» sin validar; MAY/00002 con 2.835 u.)."""
    print('C5 · mayorista')
    ps = productos(2, 600)
    stock(ps)
    may = pedido_mayorista([(ps[0], 5), (ps[1], 3)], f'SO_MAY-C5-{CORRIDA}')
    print('   ', may.name, may.codigo_unico, may.state, may.wms_estado)
    remitos_antes = remitos_emitidos()
    c = contenedor([(may, ps[0], 5), (may, ps[1], 3)])
    preparada(may, [c])
    lg = logs(picking=may, proceso='confirmacionMercaderiaPreparada')
    chequear('C5', 'la preparación deja el pedido mayorista hecho', may.state == 'done', f'{may.state}/{may.wms_estado}')
    chequear('C5', 'se emitió el e-Remito', remitos_emitidos() - remitos_antes == 1, f'{remitos_emitidos() - remitos_antes}')
    chequear('C5', 'el log no informa éxito si no quedó hecho',
             may.state == 'done' or not lg.filtered(lambda l: l.resultado == 'exito' and l.nivel == 'info'),
             '; '.join(lg.mapped(lambda l: f'{l.nivel}/{l.resultado}')))
    _, res, _ = despacho(may, [c])
    todos = por_codigo(may.codigo_unico)
    chequear('C5', 'el despacho queda registrado', all(x.wms_estado == 'despachado' for x in todos.filtered(lambda x: x.state == 'done')) and done_qty(todos) == 8,
             str(res)[:140])
    chequear('C5', 'la entrega al cliente avanza', all(m.state == 'done' for m in may.move_ids.move_dest_ids),
             ', '.join(sorted(set(may.move_ids.move_dest_ids.mapped('state')))))

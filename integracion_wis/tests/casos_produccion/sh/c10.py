def caso_c10():
    """Envío con fecha programada vencida (producción: 39 CROSS de P06871 rechazados con 400)."""
    print('C10 · fecha de entrega vencida')
    wh = tienda('027')  # San José
    ps = productos(1, 1100)
    stock(ps)
    PG = env['procurement.group']
    origen = f'OP/C10-1-{CORRIDA}'
    PG.run([PG.Procurement(ps[0], 2, ps[0].uom_id, wh.lot_stock_id, origen, origen, env.company,
                           {'warehouse_id': wh, 'route_ids': ruta_tienda(wh),
                            'date_planned': fields.Datetime.subtract(fields.Datetime.now(), days=2)})])
    refrescar()
    p = env['stock.move'].search([('origin', '=', origen), ('picking_type_id', '=', 3)]).picking_id
    llamada = [l for l in wis_llamadas('/Pedido/Create') if p.codigo_unico and p.codigo_unico in json.dumps(l.get('body'))]
    chequear('C10', 'WIS acepta el pedido (fecha de entrega no vencida)', p.wms_estado == 'enviado' and llamada and llamada[-1]['status'] == 200,
             f'{p.name} {p.wms_estado}, scheduled {p.scheduled_date}, status {[l["status"] for l in llamada]}')

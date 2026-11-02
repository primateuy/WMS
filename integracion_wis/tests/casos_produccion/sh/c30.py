# Individualización de operaciones con WIS (documento de Forum del 21-10-2026).
# El ejemplo del documento, con «Reabastecer» de verdad (reglas de reabastecimiento) y no
# con `reponer()`, que corre el abastecimiento a mano y sin grupo.

def _reabastecer(wh, producto, cantidad):
    Regla = env['stock.warehouse.orderpoint']
    regla = Regla.search([('product_id', '=', producto.id), ('location_id', '=', wh.lot_stock_id.id)],
                         limit=1) or Regla.create({
        'product_id': producto.id, 'location_id': wh.lot_stock_id.id, 'warehouse_id': wh.id,
        'trigger': 'manual'})
    # Como desde la pantalla: la ruta de la tienda y la cantidad a pedir cargada a mano.
    regla.write({'route_id': ruta_tienda(wh).id, 'qty_to_order': cantidad})
    regla.action_replenish()
    refrescar()
    return env['stock.move'].search([('product_id', '=', producto.id), ('picking_type_id', '=', 3),
                                     ('create_date', '>=', INICIO_CASO)]).picking_id


def caso_x01():
    """Unión: Reabastecer 10, WIS prepara 8, otro Reabastecer."""
    print('X01 · dos solicitudes de reabastecimiento para Unión')
    wh = tienda('004')
    ps = productos(2, 3100)
    stock(ps)
    prep1 = _reabastecer(wh, ps[0], 10)
    envio1 = siguiente(prep1)
    chequear('X01', 'la solicitud 1 tiene su grupo, preparación y envío',
             len(prep1) == 1 and len(envio1) == 1 and prep1.group_id and envio1.group_id == prep1.group_id,
             f'prep {prep1.mapped("name")}, envío {envio1.mapped("name")}')
    preparada(prep1, [contenedor([(prep1, ps[0], 8)])])
    refrescar()
    pendiente = por_codigo(prep1.codigo_unico).filtered(lambda x: x.state not in ('done', 'cancel')
                                                        and x.picking_type_id.id == 3)
    prep2 = _reabastecer(wh, ps[1], 5)
    envio2 = siguiente(prep2)
    chequear('X01', 'la solicitud 2 abre su propia preparación',
             len(prep2) == 1 and prep2 != prep1 and prep2 not in pendiente,
             f'prep2 {prep2.mapped("name")}, parcial {pendiente.mapped("name")}')
    chequear('X01', 'la solicitud 2 abre su propio envío',
             len(envio2) == 1 and envio2 != envio1 and envio2.group_id == prep2.group_id,
             f'envío1 {envio1.mapped("name")}, envío2 {envio2.mapped("name")}')
    chequear('X01', 'el parcial de la preparación 1 no recibió nada nuevo',
             ps[1] not in (prep1 | pendiente).move_ids.product_id,
             ', '.join((prep1 | pendiente).move_ids.product_id.mapped('display_name')))
    envios1 = env['stock.picking'].search([('group_id', '=', prep1.group_id.id), ('picking_type_id', '=', 2)])
    chequear('X01', 'los envíos de la solicitud 1 llevan sólo lo suyo',
             ps[1] not in envios1.move_ids.product_id, ', '.join(envios1.mapped('name')))
    para_video('X01', 'Dos solicitudes de reabastecimiento: circuitos separados',
               prep1 | pendiente | envios1 | prep2 | envio2)

# Verificación «de punta a cabo» de lo que dejó un caso en Odoo, más allá del resultado puntual.
# Se carga con exec después de casos.py.

def integracion(pedido):
    return env['stock.picking'].search([('codigo_unico', '=', pedido.codigo_unico if hasattr(pedido, 'codigo_unico') else pedido),
                                        ('wms_estado', '!=', 'no_integrado')])


NOTAS = []


def recibir_en_tienda(pickings):
    """La tienda recibe todo lo que le llegó (líneas completas y parciales de la misma caja)."""
    hechos = pickings.filtered(lambda p: p.state == 'done')
    recep = hechos.move_ids.move_dest_ids.move_dest_ids.filtered(lambda m: m.state in ('assigned', 'partially_available'))
    for pk in recep.picking_id:
        lineas = pk.move_ids.filtered(lambda m: m.state in ('assigned', 'partially_available'))
        incumplen = pk._moves_con_multiplo_incumplido() if hasattr(pk, '_moves_con_multiplo_incumplido') else pk.move_ids.browse()
        if incumplen:
            NOTAS.append(f'{pk.name}: la tienda necesita el permiso «saltear múltiplo» para recibir '
                         + ', '.join(f'{m.product_id.codigo_unico} ({m.quantity:g} u.)' for m in incumplen))
        lineas.write({'picked': True})
        # Usuario de tienda con permiso para saltear el múltiplo (ver NOTAS).
        res = pk.with_context(skip_sms=True, skip_wms_integration=True).button_validate()
        if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
            env['stock.backorder.confirmation'].with_context(**res['context']).create({}).process()
    refrescar()
    return recep


def integridad(caso, pickings, despachado=True, en_tienda=False, ucfe_desde=None):
    """Problemas que deja el caso en Odoo. `pickings`: operaciones integradas (preparaciones)."""
    problemas = []
    todos = env['stock.picking'].browse()
    for p in pickings:
        todos |= integracion(p)
    hechos = todos.filtered(lambda p: p.state == 'done')
    prods = todos.move_ids.product_id
    # e-Remito: cada preparación hecha con CFE quedó emitida, sin líneas en 0
    for p in hechos:
        if getattr(p, 'uses_cfe', False) and not p.cfe_emitido:
            problemas.append(f'{p.name} hecho sin e-Remito')
        if p.move_line_ids.filtered(lambda l: l.quantity <= 0):
            problemas.append(f'{p.name} con líneas en 0')
    if ucfe_desde is not None:
        emitidos = remitos_emitidos() - ucfe_desde
        con_cfe = len(hechos.filtered(lambda p: getattr(p, 'uses_cfe', False)))
        if emitidos != con_cfe:
            problemas.append(f'{emitidos} e-Remitos emitidos para {con_cfe} operaciones hechas')
    # stock: nada negativo y cada caja en una sola ubicación
    quants = env['stock.quant'].search([('product_id', 'in', prods.ids), ('location_id.usage', 'in', ('internal', 'transit')),
                                        ('write_date', '>=', INICIO_CASO)])
    for q in quants.filtered(lambda q: q.quantity < 0):
        problemas.append(f'stock negativo {q.product_id.codigo_unico} en {q.location_id.complete_name}: {q.quantity}')
    for paq in hechos.move_line_ids.result_package_id:
        ubic = paq.quant_ids.filtered(lambda q: q.quantity > 0).location_id
        if len(ubic) > 1:
            problemas.append(f'caja {paq.name} repartida en {ubic.mapped("complete_name")}')
    # despacho: lo despachado llegó entero a la entrega y la entrega quedó hecha sólo con eso
    if despachado:
        for p in hechos:
            if p.wms_estado != 'despachado':
                problemas.append(f'{p.name} hecho pero {p.wms_estado}')
            if not logs(picking=p, proceso='confirmacionPedido') and not logs(picking=p).filtered(lambda l: 'confirmacionPedido' in (l.texto or '')):
                problemas.append(f'{p.name} despachado sin log propio')
        for m in hechos.move_ids.filtered(lambda m: m.state == 'done'):
            dest = m.move_dest_ids.filtered(lambda d: not d.picking_id.picking_type_id.integracion_wms)
            hecho_dest = sum(dest.filtered(lambda d: d.state == 'done').mapped('quantity'))
            if dest and hecho_dest + 1e-6 < m.quantity and sum(dest.mapped('product_uom_qty')) >= m.quantity:
                problemas.append(f'{m.product_id.codigo_unico}: despachado {m.quantity}, entregado {hecho_dest}')
    # lo preparado y no despachado no se entrega
    for p in hechos.filtered(lambda p: p.wms_estado == 'preparado'):
        if p.move_ids.move_dest_ids.filtered(lambda d: d.state == 'done' and not d.picking_id.picking_type_id.integracion_wms):
            problemas.append(f'{p.name} no despachado pero su entrega quedó hecha')
    if en_tienda:
        recep = recibir_en_tienda(hechos)
        for m in recep:
            if m.state != 'done':
                problemas.append(f'recepción en tienda {m.picking_id.name} {m.product_id.codigo_unico} quedó {m.state}')
    for n in NOTAS:
        print('    NOTA', caso, n)
    NOTAS.clear()
    chequear(caso, 'integridad de punta a cabo', not problemas, ' | '.join(problemas)[:400])
    return problemas

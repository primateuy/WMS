def caso_c9():
    """Recepción integrada cancelada desde la compra (producción: IMPO/00022 y 00025 siguen abiertas en WIS)."""
    print('C9 · recepción cancelada desde la compra')
    prov = env['res.partner'].search([('name', '=', 'Proveedor Réplica SA')], limit=1)
    if not prov:
        prov = env['res.partner'].with_context(skip_wms_integration=True).create({
            'name': 'Proveedor Réplica SA', 'is_company': True, 'supplier_rank': 1,
            'country_id': env.ref('base.uy').id})
        prov.with_context(skip_wms_integration=True).write({'codigo_unico_proveedor': f'PRO-REP{prov.id}', 'integracion_wms': True})
        refrescar()
    ps = productos(1, 1000)
    tipo = env['purchase.order.type'].search([('name', 'ilike', 'SIN Crossdocking')], limit=1)
    po = env['purchase.order'].create({
        'partner_id': prov.id, 'order_type': tipo.id, 'picking_type_id': 1,
        'order_line': [(0, 0, {'product_id': ps[0].id, 'product_qty': 12, 'price_unit': 10})]})
    po.button_confirm()
    refrescar()
    rec = po.picking_ids
    print('   ', po.name, rec.mapped('name'), rec.mapped('codigo_unico'), rec.mapped('wms_estado'))
    chequear('C9', 'la recepción se envió a WIS', all(r.wms_estado == 'enviado' for r in rec), f'{rec.mapped("wms_estado")}')
    antes = len(wis_llamadas('AnulacionReferenciaRecepcion'))
    try:
        po.button_cancel()
    except Exception as e:
        print('   cancelación rechazada:', str(e)[:150])
        env.cr.rollback()
    refrescar()
    avisos = len(wis_llamadas('AnulacionReferenciaRecepcion')) - antes
    chequear('C9', 'al cancelar la compra se avisa la anulación a WIS (o no se deja cancelar)',
             avisos >= 1 or po.state != 'cancel', f'compra {po.state}, recepción {rec.mapped(lambda r: (r.state, r.wms_estado))}, avisos {avisos}')

def caso_c11():
    """Ajuste de WIS fuera de escala (producción: 275407 «ASD», 430.970 bolsas)."""
    print('C11 · ajuste fuera de escala')
    ps = productos(1, 1200)
    nro = 900000 + next(_cont) % 99999
    item = {'Faixa': 1.0, 'Motivo': 'AM', 'Predio': '1', 'Empresa': 6005, 'Producto': ps[0].codigo_unico,
            'Ubicacion': 'ALTURA18', 'Aplicacion': 'INV540', 'AreaAveria': 'N', 'TipoAjuste': 'US',
            'Funcionario': 169, 'Identificador': '*', 'FechaRealizacion': HOY, 'DescripcionMotivo': 'ASD',
            'NumeroAjusteStock': nro, 'CantidadMovimiento': 430970.0}
    antes = ps[0].with_context(location=POLO_STOCK.id).qty_available
    _, res, _ = enviar('Ajustes', {'Ajustes': [item]})
    despues = ps[0].with_context(location=POLO_STOCK.id).qty_available
    chequear('C11', 'un ajuste de 430.970 u. no impacta sin aprobación', despues - antes < 1000, f'stock POLO {antes} -> {despues}; {str(res)[:100]}')
    lg = env['wms.integracion.log'].sudo().search([('proceso', '=', 'ajustes')], order='id desc', limit=3)
    chequear('C11', 'queda registrado como pendiente de aprobación', any('aprob' in (l.texto or '').lower() for l in lg),
             ' | '.join((l.texto or '')[:60] for l in lg))

# -*- coding: utf-8 -*-
"""0.10.0: cada contenedor de WIS se aplica una sola vez a cada pedido (wis.preparacion.aplicada).

Lo que se preparó con versiones anteriores no tiene ese registro, y un reenvío o un reproceso
de una notificación vieja lo volvería a aplicar (así se duplicaron 88 e-Remitos el 21 y 22-10).
Se reconstruye desde el log de webhooks: un contenedor cuenta como aplicado si hay una operación
hecha de ese pedido con su paquete. Lo que nunca se aplicó (la preparación falló) queda sin
registro y se puede aplicar cuando llegue.
"""
import json
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _clave(c):
    return str(c.get('NumeroContenedor') or c.get('CodigoBarras') or c.get('IdExternoContenedor') or '')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Aplicada = env['wis.preparacion.aplicada']
    MoveLine = env['stock.move.line']
    vistos = set(Aplicada.search([]).mapped(lambda a: (a.pedido, a.contenedor)))
    creados = 0
    logs = env['wis.webhook.log'].search(
        [('tipo', 'in', ('confirmacionMercaderiaPreparada', 'confirmacionPedido'))], order='id')
    for log in logs:
        try:
            payload = json.loads(log.request or '')
        except ValueError:
            continue
        datos = (payload.get('ConfirmacionMercaderiaPreparada') or payload.get('ConfirmacionPedido') or {})
        for c in datos.get('Contenedores') or []:
            clave = _clave(c)
            barras = c.get('CodigoBarras') or ''
            if not clave or not barras:
                continue
            for pedido in {d.get('Pedido') for d in c.get('Detalles') or [] if d.get('Pedido')}:
                if (pedido, clave) in vistos:
                    continue
                ml = MoveLine.search([('picking_id.codigo_unico', '=', pedido),
                                      ('result_package_id.name', '=', barras),
                                      ('state', '=', 'done')], limit=1)
                if not ml:
                    continue
                Aplicada.create({
                    'pedido': pedido, 'contenedor': clave,
                    'nro_preparacion': str(c.get('Preparacion') or ''),
                    'picking_id': ml.picking_id.id,
                    'despachado': ml.picking_id.wms_estado == 'despachado',
                })
                vistos.add((pedido, clave))
                creados += 1
    _logger.info("[WIS] 0.10.0: %s contenedor(es) históricos registrados como aplicados.", creados)

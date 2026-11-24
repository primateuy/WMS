# -*- coding: utf-8 -*-
"""Recuperar las líneas que WIS nunca recibió.

Producción, 05 al 24-11-2026: la reposición en segundo plano armaba los pedidos en tandas y la
cola los mandaba a WIS antes de que terminaran. Las líneas que llegaban después quedaron en
Odoo, con el código W-P del pedido, pero WIS no las tuvo nunca: 28.177 líneas, 133.033
unidades. Cuando WIS preparaba el pedido, Odoo pasaba esas líneas a un saldo que conserva el
mismo código, así que también hay que buscarlas en los saldos.

La falla está corregida en la cola (0.14.5). Esto recupera lo que ya quedó así:

* **Detectar.** Una línea pendiente nunca viajó si su producto no está en ningún
  `/Pedido/Create` exitoso del código W-P de su operación (`wis.envio.log`). Si no hay envío
  registrado (anterior al registro, 05-11) o el pedido viajó con cajas (`lpns`, el detalle no
  está en `detalles`), no se puede saber y la operación no se toca.
* **Recuperar.** Las líneas se MUEVEN a una operación nueva —no se borran ni se duplican— y
  esa operación sale a WIS por la cola con su propio código (`W-P-<id nuevo>`): WIS no acepta
  modificar un pedido ya liberado para picking. Como los movimientos son los mismos, la
  entrega a la tienda sigue encadenada y avanza cuando WIS despache el pedido nuevo
  (`_wis_avanzar_cadena` sigue a los movimientos, no al código).

El grupo de abastecimiento de la operación lo toma Odoo de sus movimientos (`group_id` es
related). No hace falta más: una vez enviada queda `enviado` y
`_search_picking_for_assignation_domain` no le deja sumar líneas, y los grupos de las
reposiciones viejas ya no generan movimientos nuevos.
"""
import json
import logging
from collections import defaultdict

from odoo import _, api, models

_logger = logging.getLogger(__name__)

ENDPOINT_PEDIDO = 'Pedido/Create'


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # ------------------------------------------------------------------
    # Detección
    # ------------------------------------------------------------------
    @api.model
    def _wis_productos_enviados(self, codigos):
        """{código W-P: set(códigos de producto enviados)} según los `/Pedido/Create`
        exitosos. `None` si alguno de esos envíos viajó con cajas: el contenido de una caja no
        figura en `detalles` y no se puede saber qué recibió WIS. Los códigos sin envío
        registrado no aparecen."""
        enviados = {}
        if not codigos:
            return enviados
        logs = self.env['wis.envio.log'].sudo().search([
            ('codigo_unico', 'in', list(codigos)),
            ('endpoint', 'ilike', ENDPOINT_PEDIDO),
            ('resultado', '=', 'exito'),
        ])
        for log in logs:
            try:
                pedidos = (json.loads(log.request or '{}') or {}).get('pedidos') or []
            except (ValueError, AttributeError):
                _logger.warning("[WIS] recuperar | envío %s con JSON ilegible: se ignora", log.id)
                continue
            productos = enviados.setdefault(log.codigo_unico, set())
            if productos is None:
                continue
            for pedido in pedidos:
                if pedido.get('lpns'):
                    enviados[log.codigo_unico] = None
                    break
                productos.update(
                    d.get('codigoProducto') for d in (pedido.get('detalles') or [])
                    if d.get('codigoProducto'))
        return enviados

    def _wis_lineas_no_enviadas(self):
        """`self`: operaciones a revisar. Devuelve (movimientos pendientes que WIS nunca
        recibió, operaciones que no se pudieron revisar)."""
        Move = self.env['stock.move']
        candidatas = self.filtered(
            lambda p: p.codigo_unico and p.wms_estado == 'enviado'
            and p.state not in ('done', 'cancel') and p.picking_type_id.integracion_wms)
        enviados = self._wis_productos_enviados(set(candidatas.mapped('codigo_unico')))
        no_enviadas = Move
        sin_revisar = self.env['stock.picking']
        for picking in candidatas:
            productos = enviados.get(picking.codigo_unico)
            if productos is None:
                sin_revisar |= picking
                continue
            no_enviadas |= picking.move_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
                and m.product_id.codigo_unico and m.product_id.codigo_unico not in productos)
        return no_enviadas, sin_revisar

    # ------------------------------------------------------------------
    # Recuperación
    # ------------------------------------------------------------------
    @api.model
    def _wis_recuperar_lineas(self, moves, max_lineas=0):
        """Pasa `moves` a operaciones nuevas —una por tipo, origen, destino y contacto (en la
        reposición, una por sucursal)— y las pone en la cola de WIS. Con `max_lineas`, parte
        cada operación en otras de ese tamaño como máximo. Devuelve las operaciones nuevas."""
        grupos = defaultdict(lambda: self.env['stock.move'])
        for move in moves.filtered(lambda m: m.state not in ('done', 'cancel') and m.picking_id):
            picking = move.picking_id
            grupos[(picking.picking_type_id.id, move.location_id.id, move.location_dest_id.id,
                    picking.partner_id.id)] |= move
        nuevas = self.env['stock.picking']
        for clave in sorted(grupos, key=lambda c: tuple(x or 0 for x in c)):
            lineas = grupos[clave].sorted('id')
            tamano = max_lineas if max_lineas and max_lineas > 0 else len(lineas)
            for inicio in range(0, len(lineas), tamano):
                nuevas |= self._wis_recuperar_lote(lineas[inicio:inicio + tamano])
        return nuevas

    @api.model
    def _wis_recuperar_lote(self, moves):
        Picking = self.with_context(skip_wms_integration=True)
        origenes = moves.picking_id
        modelo = origenes.sorted('id')[:1]
        procesos = sorted({o for o in origenes.mapped('origin') if o})
        nueva = Picking.create({
            'picking_type_id': modelo.picking_type_id.id,
            'location_id': moves[:1].location_id.id,
            'location_dest_id': moves[:1].location_dest_id.id,
            'partner_id': modelo.partner_id.id,
            'company_id': modelo.company_id.id,
            'move_type': modelo.move_type,
            'origin': (_("%s (recuperación WIS)") % ", ".join(procesos))[:250] if procesos
            else _("Recuperación WIS"),
        })
        por_origen = {o: len(moves.filtered(lambda m, o=o: m.picking_id == o)) for o in origenes}
        moves = moves.with_context(skip_wms_integration=True)
        moves.filtered('move_line_ids')._do_unreserve()
        moves.write({'picking_id': nueva.id})
        nueva._wis_complete_document_type()

        tipo = nueva.picking_type_id.tipo_pedido_wis or 'NORM'
        nueva.with_context(skip_wms_integration=True).write({
            'codigo_unico': nueva._wis_codigo_esperado(tipo),
            'wms_estado': 'en_cola',
        })
        self.env['wis.picking.cola'].sudo()._encolar(nueva, tipo)

        for origen, cantidad in por_origen.items():
            origen.message_post(
                body=_("%(n)s línea(s) que WIS nunca recibió con %(cod)s pasaron a %(nueva)s "
                       "(%(nuevo_cod)s), que sale a WIS como pedido nuevo.",
                       n=cantidad, cod=origen.codigo_unico, nueva=nueva.name,
                       nuevo_cod=nueva.codigo_unico),
                subtype_xmlid='mail.mt_note')
        nueva.message_post(
            body=_("Líneas que WIS nunca recibió, recuperadas de: %s.")
            % ", ".join("%s (%s)" % (o.name, o.codigo_unico) for o in origenes),
            subtype_xmlid='mail.mt_note')
        _logger.info("[WIS] recuperar | %s líneas de %s operaciones pasan a %s (%s)",
                     len(moves), len(origenes), nueva.name, nueva.codigo_unico)
        return nueva

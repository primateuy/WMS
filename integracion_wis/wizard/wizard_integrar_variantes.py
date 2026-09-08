# -*- coding: utf-8 -*-
"""Wizard de integración prioritaria de variantes desde una Orden de Compra.

Resuelve el escenario del requerimiento: una OC con 5 variantes no integradas
de un producto que tiene 100. Se integran YA las 5 que la orden necesita —una
sola llamada por lote a WIS, cuestión de segundos— y las 95 restantes quedan
en la cola de segundo plano sin bloquear al operador.
"""
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from ..models.wis_sync_queue import PRIORIDAD_URGENTE

_logger = logging.getLogger(__name__)


class WisIntegrarVariantesWizard(models.TransientModel):
    _name = 'wis.integrar.variantes.wizard'
    _description = 'Integrar variantes pendientes con WIS'

    order_id = fields.Many2one(
        'purchase.order',
        string='Orden de Compra',
        required=True,
        ondelete='cascade',
    )

    variante_ids = fields.Many2many(
        'product.product',
        string='Variantes a integrar',
        help='Variantes de la orden que todavía no tienen código WIS.',
    )

    cantidad_variantes = fields.Integer(
        string='Variantes de la orden',
        compute='_compute_resumen',
    )

    cantidad_resto = fields.Integer(
        string='Otras variantes de los mismos productos',
        compute='_compute_resumen',
        help='Variantes de los mismos productos que no participan de esta orden. '
             'Se integran en segundo plano, sin bloquear la operación.',
    )

    resultado = fields.Text(string='Resultado', readonly=True)

    @api.depends('variante_ids', 'order_id')
    def _compute_resumen(self):
        for wizard in self:
            wizard.cantidad_variantes = len(wizard.variante_ids)
            templates = wizard.variante_ids.mapped('product_tmpl_id')
            resto = templates.mapped('product_variant_ids').filtered(
                lambda v: v.type == 'product' and not v.codigo_unico
            ) - wizard.variante_ids
            wizard.cantidad_resto = len(resto)

    def action_integrar_ahora(self):
        """Integra las variantes de la orden y encola el resto.

        Solo continúa con la confirmación de la orden si TODAS las variantes
        que la orden necesita quedaron integradas.
        """
        self.ensure_one()

        if not self.variante_ids:
            raise UserError(_("No hay variantes para integrar."))

        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise UserError(_(
                "La comunicación con WIS está deshabilitada. Actívela en la "
                "configuración de WIS antes de integrar."
            ))

        # 1. Las que la orden necesita: ahora, en un solo lote.
        self.variante_ids.filtered(lambda v: not v.integracion_wms).with_context(
            _avoid_wms=True).write({'integracion_wms': True})

        resultado = self.variante_ids._enviar_wms_en_lote(
            motivo=f'integración prioritaria desde {self.order_id.name}'
        )

        # 2. El resto de las variantes de esos productos: a la cola.
        entradas = self.order_id._wis_encolar_resto_variantes()

        # 3. ¿Quedó alguna sin código?
        sin_integrar = self.variante_ids.filtered(lambda v: not v.codigo_unico)

        if sin_integrar:
            detalle = "\n".join(f"  • {v.display_name}" for v in sin_integrar[:20])
            errores = "\n".join(resultado.get('errores_detalle', [])[:10])
            raise UserError(_(
                "No se pudieron integrar %s de %s variantes:\n\n%s\n\n%s"
            ) % (len(sin_integrar), len(self.variante_ids), detalle, errores))

        _logger.info("[WIS] %s: integradas %d variantes, %d encoladas en segundo plano",
                     self.order_id.name, resultado['enviados'], len(entradas))

        # 4. Todo listo: se continúa con la confirmación de la orden.
        return self.order_id.with_context(wis_omitir_control=True).button_confirm()

    def action_continuar_sin_integrar(self):
        """Confirma la orden igual, dejando constancia.

        La advertencia no es un bloqueo: la decisión es del operador. Queda
        registrada en el chatter de la orden.
        """
        self.ensure_one()

        nombres = ", ".join(self.variante_ids.mapped('display_name')[:20])
        self.order_id.message_post(body=_(
            "Orden confirmada con %s variante(s) sin integrar en WIS: %s. "
            "La recepción en WIS puede fallar si no se integran antes."
        ) % (len(self.variante_ids), nombres))

        _logger.warning("[WIS] %s confirmada con %d variantes sin integrar.",
                        self.order_id.name, len(self.variante_ids))

        return self.order_id.with_context(wis_omitir_control=True).button_confirm()

    def action_encolar_y_cerrar(self):
        """Encola las variantes y cierra el wizard, sin confirmar la orden."""
        self.ensure_one()

        self.variante_ids.filtered(lambda v: not v.integracion_wms).with_context(
            _avoid_wms=True).write({'integracion_wms': True})

        entradas = self.env['wis.sync.queue']._encolar(
            self.variante_ids,
            origen='oc',
            origen_ref=self.order_id.name,
            prioridad=PRIORIDAD_URGENTE,  # la orden está esperando
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Variantes encoladas'),
                'message': _("%s variante(s) en cola con prioridad alta. La orden se "
                             "puede confirmar cuando terminen de integrarse.") % len(entradas),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

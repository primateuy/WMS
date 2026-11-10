# -*- coding: utf-8 -*-
from odoo import models, fields, api, _


class WisIntegrarPickingsWizard(models.TransientModel):
    _name = 'wis.integrar.pickings.wizard'
    _description = 'Integrar operaciones con WMS (masivo)'

    picking_ids = fields.Many2many('stock.picking', string='Operaciones seleccionadas')
    total = fields.Integer(string='Seleccionadas', readonly=True)
    integrables = fields.Integer(string='Se integrarán', readonly=True)
    mensaje = fields.Html(readonly=True)

    @api.model
    def _pickings_integrables(self, pickings):
        """Las que se integrarían con WMS: tipo con Integración con WMS activa (y que
        no adquiere código) y aún sin enviar. Las recepciones que integran por estado
        y las operaciones sin integración NO entran acá."""
        return pickings.filtered(
            lambda p: p.picking_type_id.integracion_wms
            and not p.picking_type_id.adquiere_codigo_unico_wms
            and p.wms_estado == 'sin_enviar'
        )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ids = self.env.context.get('active_ids') or []
        pickings = self.env['stock.picking'].browse(ids).exists()
        integrables = self._pickings_integrables(pickings)
        omitidas = len(pickings) - len(integrables)
        mensaje = _(
            "<p>Seleccionaste <b>%(total)s</b> operación(es). Sólo se integrarán las "
            "que tienen <b>Integración con WMS</b> y están sin enviar: "
            "<b>%(integrables)s</b>.</p>"
        ) % {'total': len(pickings), 'integrables': len(integrables)}
        if omitidas:
            mensaje += _(
                "<p>Se omitirán <b>%(omitidas)s</b> (no integran con WMS, ya fueron "
                "enviadas, o integran automáticamente por estado —p. ej. las "
                "recepciones—).</p>"
            ) % {'omitidas': omitidas}
        mensaje += _("<p>Las operaciones se <b>encolan</b> para integrarse (no se espera).</p>")
        res.update({
            'picking_ids': [(6, 0, pickings.ids)],
            'total': len(pickings),
            'integrables': len(integrables),
            'mensaje': mensaje,
        })
        return res

    def action_confirmar(self):
        self.ensure_one()
        encoladas = self.picking_ids.action_integrar_wms_manual()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Integración con WMS'),
                'message': _('%s operación(es) encoladas para integrar con WMS.') % len(encoladas),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

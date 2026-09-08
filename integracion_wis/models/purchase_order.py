from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
import logging

from .wis_sync_queue import PRIORIDAD_URGENTE, PRIORIDAD_BAJA

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    integracion_wms = fields.Boolean(
        string='Integración con WIS',
        default=False,
    )

    numeroInterfazWMS = fields.Char(
        string="Numero interfaz Wms",
        help="Identificación WMS"
    )

    referencia = fields.Char(string="Referencia", default="");

    enviadoWMS = fields.Boolean(string="Enviado de WMS", default=False)

    # ------------------------------------------------------------------
    # Control de variantes no integradas
    # ------------------------------------------------------------------
    wis_variantes_pendientes_ids = fields.Many2many(
        'product.product',
        string='Variantes sin integrar en WIS',
        compute='_compute_wis_variantes_pendientes',
        help='Variantes de esta orden que todavía no tienen código WIS. Si la '
             'recepción llega a WIS con alguna de estas, la operación falla.',
    )

    wis_cantidad_pendientes = fields.Integer(
        string='Cantidad de variantes sin integrar',
        compute='_compute_wis_variantes_pendientes',
    )

    wis_recepcion_integrada = fields.Boolean(
        string='La recepción va a WIS',
        compute='_compute_wis_variantes_pendientes',
    )

    @api.depends('order_line.product_id', 'picking_type_id',
                 'order_line.product_id.codigo_unico',
                 'order_line.product_id.integracion_wms')
    def _compute_wis_variantes_pendientes(self):
        Producto = self.env['product.product']
        config = self.env['integracion_wis.integracion_wis']._get_config()
        activa = bool(config and config.comunicacion_activa)
        exigir_todas = bool(config and config.exigir_integracion_oc)

        for order in self:
            integrada = activa and bool(order.picking_type_id.integracion_wms)
            order.wis_recepcion_integrada = integrada

            if not integrada:
                order.wis_variantes_pendientes_ids = Producto
                order.wis_cantidad_pendientes = 0
                continue

            pendientes = order._wis_variantes_pendientes(exigir_todas=exigir_todas)
            order.wis_variantes_pendientes_ids = pendientes
            order.wis_cantidad_pendientes = len(pendientes)

    def _wis_variantes_pendientes(self, exigir_todas=None):
        """Variantes de la orden que deberían estar integradas y no lo están.

        Por defecto se consideran solo las marcadas para integración —en la
        variante o en su plantilla—. Con `exigir_integracion_oc` activo en la
        configuración, cualquier producto almacenable de la orden cuenta.
        """
        self.ensure_one()

        if exigir_todas is None:
            config = self.env['integracion_wis.integracion_wis']._get_config()
            exigir_todas = bool(config and config.exigir_integracion_oc)

        productos = self.order_line.filtered(
            lambda l: not l.display_type and l.product_id
        ).mapped('product_id').filtered(lambda p: p.type == 'product')

        def requiere_integracion(producto):
            if exigir_todas:
                return True
            return producto.integracion_wms or producto.product_tmpl_id.integracion_wms

        return productos.filtered(
            lambda p: requiere_integracion(p) and not p.codigo_unico
        )

    def action_wis_integrar_pendientes(self):
        """Abre el wizard de integración de las variantes pendientes."""
        self.ensure_one()

        pendientes = self.wis_variantes_pendientes_ids
        if not pendientes:
            raise UserError(_("No hay variantes pendientes de integrar en esta orden."))

        wizard = self.env['wis.integrar.variantes.wizard'].create({
            'order_id': self.id,
            'variante_ids': [(6, 0, pendientes.ids)],
        })

        return {
            'name': _('Variantes sin integrar en WIS'),
            'type': 'ir.actions.act_window',
            'res_model': 'wis.integrar.variantes.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def button_confirm(self):
        """Antes de confirmar, avisa si hay variantes sin integrar en WIS.

        El objetivo es que el problema se detecte acá y no recién durante la
        recepción en WIS. Los flujos automáticos que no deban ser interrumpidos
        pueden pasar `wis_omitir_control=True` en el contexto.
        """
        if self.env.context.get('wis_omitir_control'):
            return super(PurchaseOrder, self).button_confirm()

        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not config or not config.comunicacion_activa:
            return super(PurchaseOrder, self).button_confirm()

        con_pendientes = self.filtered(
            lambda o: o.state in ('draft', 'sent') and o.wis_cantidad_pendientes
        )

        if not con_pendientes:
            return super(PurchaseOrder, self).button_confirm()

        if config.bloquear_oc_sin_integrar:
            orden = con_pendientes[0]
            nombres = "\n".join(f"  • {p.display_name}"
                                for p in orden.wis_variantes_pendientes_ids[:20])
            extra = ("\n  … y más" if orden.wis_cantidad_pendientes > 20 else "")
            raise UserError(_(
                "La orden %s tiene %s variante(s) sin integrar en WIS. La recepción "
                "va a fallar en WIS si se confirma así.\n\n%s%s\n\n"
                "Usá el botón «Integrar ahora» de la advertencia para resolverlo."
            ) % (orden.name, orden.wis_cantidad_pendientes, nombres, extra))

        # Una sola orden: se ofrece resolverlo en el momento.
        if len(con_pendientes) == 1:
            return con_pendientes.action_wis_integrar_pendientes()

        # Confirmación múltiple: no se interrumpe, se deja constancia.
        for orden in con_pendientes:
            _logger.warning(
                "[WIS] La orden %s se confirma con %d variante(s) sin integrar.",
                orden.name, orden.wis_cantidad_pendientes
            )
        return super(PurchaseOrder, self).button_confirm()

    def _wis_encolar_resto_variantes(self):
        """Encola, con prioridad baja, el resto de variantes de los templates
        involucrados en la orden.

        Es el punto 6 del requerimiento: la orden solo depende de las variantes
        que necesita; las demás se integran en segundo plano sin bloquear.
        """
        self.ensure_one()

        templates = self.wis_variantes_pendientes_ids.mapped('product_tmpl_id')
        if not templates:
            templates = self.order_line.mapped('product_id.product_tmpl_id')

        resto = templates.mapped('product_variant_ids').filtered(
            lambda v: v.type == 'product' and not v.codigo_unico
        )
        if not resto:
            return self.env['wis.sync.queue']

        return self.env['wis.sync.queue']._encolar(
            resto,
            origen='oc',
            origen_ref=self.name,
            prioridad=PRIORIDAD_BAJA,
        )

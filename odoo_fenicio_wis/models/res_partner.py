# -*- coding: utf-8 -*-

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    """Extiende res.partner para integrar con WIS los clientes de Fenicio."""

    _inherit = 'res.partner'

    @api.model
    def get_partner_orden_venta(self, json_data):
        """Obtiene o crea el cliente de la orden Fenicio y lo sincroniza con WIS.

        Se desactiva la sincronización automática de integracion_wis durante el
        proceso original: odoo_fenicio escribe país/departamento sobre el
        contacto y eso dispararía un envío a WIS antes de tiempo (y sin
        customer_rank, lo que hace fallar la orden). El envío se hace una sola
        vez al final, de forma controlada.

        Args:
            json_data (dict): orden recibida desde Fenicio.

        Returns:
            res.partner: cliente de la orden.
        """
        partner = super(
            ResPartner, self.with_context(skip_wis_sync=True)
        ).get_partner_orden_venta(json_data)
        if partner:
            partner.with_context(skip_wis_sync=False)._sync_fenicio_partner_wis()
        return partner

    def _sync_fenicio_partner_wis(self):
        """Marca el contacto con integración WIS y lo envía como agente CLI.

        Si el contacto ya existía en WIS (tiene codigo_unico_cliente), enviarWS
        lo actualiza; si no, lo da de alta. customer_rank se fuerza a 1 porque
        Odoo recién lo incrementa al publicar una factura, y enviarWS rechaza
        contactos que no son cliente ni proveedor.

        Un error de WIS no interrumpe la orden de Fenicio: se registra en los
        logs WIS del contacto para poder reenviarlo desde el formulario.
        """
        self.ensure_one()
        vals = {}
        if not self.integracion_wms:
            vals['integracion_wms'] = True
        if not self.customer_rank:
            vals['customer_rank'] = 1
        if vals:
            self.with_context(skip_wis_sync=True).write(vals)

        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            _logger.info(
                "Fenicio: comunicación con WIS deshabilitada, no se envía el contacto %s.",
                self.id,
            )
            return

        try:
            with self.env.cr.savepoint():
                self.enviarWS()
        # enviarWS puede fallar por validaciones, red o respuesta de WIS; ninguna
        # de esas causas debe impedir que se registre la orden de Fenicio.
        except Exception as error:
            _logger.warning(
                "Fenicio: no se pudo sincronizar el contacto %s con WIS: %s", self.id, error
            )
            # El savepoint descarta el log de error que crea enviarWS, se registra de nuevo.
            self.env['logs.res.partner'].create({
                'partner_id': self.id,
                'fecha': fields.Datetime.now(),
                'operacion': 'envio_cli',
                'resultado': 'error',
                'texto': f"[CLI] Error al sincronizar desde orden Fenicio: {error}",
            })

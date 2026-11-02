import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class WisWebhookLog(models.Model):
    _name = 'wis.webhook.log'
    _description = 'Webhook Logs'
    _order = 'fecha desc, hora desc'

    fecha = fields.Date(string='Fecha', default=fields.Date.today, required=True)
    hora = fields.Char(string='Hora', required=True)
    numero_interfaz_ejecucion = fields.Integer(string='Nro. Interfaz Ejecución')
    codigo_unico = fields.Char(
        string='Código WMS', index=True,
        help="Código(s) WMS identificado(s) en el payload: pedido / nroPedido / codigoUnico / "
             "referencia / serializado. Ej.: W-P-1772.")
    request = fields.Text(string='Request')
    tipo = fields.Char(string='Tipo')
    respuesta = fields.Text(string='Respuesta')
    estado = fields.Selection([
        ('exito', 'Éxito'),
        ('error', 'Error'),
    ], string='Estado', required=True, default='exito')
    notificacion_id = fields.Many2one(
        'wis.notificacion', string="Notificación", ondelete='set null', index=True,
        help="La notificación guardada a partir de este webhook. Es la que se reprocesa.")

    # Tipos que no se guardan como notificación: no modifican nada en Odoo.
    TIPOS_SIN_REPROCESO = ('test', 'consultaStock', 'ENTRADA_DEBUG', 'desconocido')

    def _notificacion_para_reprocesar(self):
        """La notificación de este webhook; si es un log anterior a las notificaciones, la arma
        con el JSON guardado en el log."""
        self.ensure_one()
        if self.notificacion_id:
            return self.notificacion_id
        if self.tipo in self.TIPOS_SIN_REPROCESO:
            if self.tipo == 'ENTRADA_DEBUG':
                raise UserError(_(
                    "Este es el registro de llegada y guarda el contenido recortado. Reprocesá "
                    "desde el registro del procesamiento de la misma notificación."))
            raise UserError(_("Un webhook de tipo «%s» no modifica nada en Odoo: no hay qué reprocesar.")
                            % self.tipo)
        Notificacion = self.env['wis.notificacion'].sudo()
        if self.numero_interfaz_ejecucion:
            notif = Notificacion.search([('tipo', '=', self.tipo),
                                         ('numero_interfaz', '=', self.numero_interfaz_ejecucion)], limit=1)
            if notif:
                return notif
        # Un log anterior a las notificaciones no tiene marcado qué partes se aplicaron.
        # Recepciones, almacenamientos y preparaciones se protegen solos (picking ya validado,
        # contenedor ya aplicado); un ajuste o una anulación parcial se aplicaría de nuevo.
        if self.tipo in ('ajustes', 'pedidosAnulados'):
            raise UserError(_(
                "Este webhook se procesó antes de que Odoo registrara qué partes aplicaba: "
                "reprocesar un «%s» podría aplicarlo dos veces. Revisalo y corregilo a mano.")
                % self.tipo)
        if self.estado != 'error':
            raise UserError(_("Este webhook se procesó bien y es anterior a las notificaciones: "
                              "no se reprocesa."))
        try:
            payload = json.loads(self.request or '')
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise UserError(_("El contenido guardado en este registro no es un JSON completo: no se "
                              "puede reprocesar."))
        notif, _nueva = Notificacion._registrar(self.tipo, self.numero_interfaz_ejecucion, payload)
        # Nace como error para que «Reprocesar» la tome; nunca se aplicó por esta vía.
        notif.write({'estado': 'error'})
        return notif

    def action_reprocesar(self):
        self.ensure_one()
        notif = self._notificacion_para_reprocesar()
        if notif.estado in ('pendiente', 'procesando'):
            raise UserError(_("La notificación %s ya está en cola o procesándose.") % notif.display_name)
        if notif.estado == 'retenida':
            raise UserError(_("La notificación %s está retenida: se aplica con «Aprobar y aplicar» "
                              "desde Notificaciones WIS.") % notif.display_name)
        if not self.notificacion_id:
            self.sudo().notificacion_id = notif
        notif.action_reprocesar()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'wis.notificacion',
            'res_id': notif.id,
            'view_mode': 'form',
            'name': _("Notificación WIS"),
        }

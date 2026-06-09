from odoo import models, fields, api


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
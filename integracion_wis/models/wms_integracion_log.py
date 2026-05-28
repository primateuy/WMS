from odoo import models, fields, api

class WMSIntegracionLog(models.Model):
    _name = 'wms.integracion.log'
    _description = 'Logs de Integración WMS'
    _order = 'fecha desc'
    
    name = fields.Char(string='Nombre', compute='_compute_name', store=True)
    fecha = fields.Datetime(
        string='Fecha y Hora',
        default=fields.Datetime.now,
        required=True
    )
    nivel = fields.Selection([
        ('info', 'Info'),
        ('warning', 'Advertencia'),
        ('error', 'Error')
    ], string='Nivel', required=True, default='info')
    
    modelo = fields.Char(string='Módulo/Modelo', required=True)
    texto = fields.Text(string='Detalle del Log', required=True)
    
    picking_id = fields.Many2one('stock.picking', string='Picking', ondelete='cascade')
    resultado = fields.Selection([
        ('exito', 'Éxito'),
        ('error', 'Error'),
        ('pendiente', 'Pendiente'),
        ('omitido', 'Omitido')
    ], string='Resultado')
    detalle = fields.Text(string='Detalle')
    payload_webhook = fields.Text(string='Payload recibido de WIS')
    
    @api.depends('fecha', 'nivel', 'modelo')
    def _compute_name(self):
        for record in self:
            if record.fecha:
                fecha_str = fields.Datetime.to_string(record.fecha)
                record.name = f"{record.nivel.upper()} - {record.modelo} - {fecha_str}"
            else:
                record.name = f"{record.nivel.upper()} - {record.modelo}"
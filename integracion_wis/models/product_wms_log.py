from odoo import models, fields, api

class ProductWmsLog(models.Model):
    _name = 'product.wms.log'
    _description = 'Log de operaciones WMS por producto'
    _order = 'fecha desc'
    _rec_name = 'operacion'

    product_id = fields.Many2one(
        'product.product', 
        string='Producto', 
        required=True, 
        ondelete='cascade'
    )
    
    operacion = fields.Selection([
        ('enviar_producto', 'Enviar Producto'),
        ('consulta_stock', 'Consultar Stock'),
        ('actualizar_stock', 'Actualizar Stock'),
        ('enviar_barcode', 'Enviar Código de Barras'),
        ('sincronizacion', 'Sincronización'),
        ('otro', 'Otro')
    ], string='Operación', required=True)
    
    resultado = fields.Selection([
        ('success', 'Exitoso'),
        ('error', 'Error'),
        ('warning', 'Advertencia'),
        ('info', 'Información')
    ], string='Resultado', required=True)
    
    detalle = fields.Text(string='Detalle')
    
    response_data = fields.Text(string='Datos de Respuesta')
    
    fecha = fields.Datetime(
        string='Fecha', 
        required=True, 
        default=fields.Datetime.now
    )
    
    usuario_id = fields.Many2one(
        'res.users', 
        string='Usuario', 
        required=True, 
        default=lambda self: self.env.user
    )

    @api.depends('operacion', 'resultado', 'fecha')
    def _compute_display_name(self):
        for record in self:
            record.display_name = f"{record.operacion} - {record.resultado} ({record.fecha})"
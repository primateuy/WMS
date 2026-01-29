# models/wms_pedido_evento.py

from odoo import models, fields, api
import json

class WmsPedidoEvento(models.Model):
    _name = 'wms.pedido.evento'
    _description = 'Eventos de Pedidos WMS'
    _order = 'fechaHora desc'

    tipoEvento = fields.Char('Tipo de Evento', required=True)
    fechaHora = fields.Datetime('Fecha y Hora', default=fields.Datetime.now, required=True)
    datosRecibidos = fields.Text('Datos Recibidos')
    
    picking_id = fields.Many2one('stock.picking', string='Picking', ondelete='cascade')
    purchase_order_id = fields.Many2one('purchase.order', string='Orden de Compra', ondelete='cascade')
    estado = fields.Selection([
        ('procesado', 'Procesado'),
        ('error', 'Error'), 
        ('pendiente', 'Pendiente')
    ], default='procesado', string='Estado')
    operador = fields.Char('Operador WMS')
    observaciones = fields.Text('Observaciones')
    
    datos_formateados = fields.Text('Datos Formateados', compute='_compute_datos_formateados', store=True)
    
    @api.depends('datosRecibidos')
    def _compute_datos_formateados(self):
       
        for record in self:
            if record.datosRecibidos:
                try:
                    # Si es string JSON, parsearlo
                    if isinstance(record.datosRecibidos, str):
                        datos = json.loads(record.datosRecibidos)
                    else:
                        datos = record.datosRecibidos
                    
                    # Formatear bonito
                    record.datos_formateados = json.dumps(datos, indent=2, ensure_ascii=False)
                except:
                    record.datos_formateados = str(record.datosRecibidos)
            else:
                record.datos_formateados = ""

    def obtener_resumen_evento(self):
        """
        Retorna un resumen del evento para dashboards
        """
        resumen = {
            'tipo': self.tipoEvento,
            'fecha': self.fechaHora,
            'estado': self.estado,
        }
        
        if 'empaquetado' in self.tipoEvento.lower():
            try:
                datos = json.loads(self.datosRecibidos) if isinstance(self.datosRecibidos, str) else self.datosRecibidos
                contenedores = datos.get('contenedores', [])
                resumen['num_paquetes'] = len(contenedores)
                resumen['transportadora'] = datos.get('transportadora')
            except:
                pass
                
        return resumen
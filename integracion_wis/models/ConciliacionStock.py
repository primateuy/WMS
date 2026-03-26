from odoo import fields, api, models
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)

class ConciliacionLogsStock(models.Model):
    _name = 'logs.conciliacion.stock'
    _description = 'Logs de Conciliación de Stock entre Sistemas'
    _order = 'fecha asc'

    
    fecha = fields.Datetime(string='Fecha', required=True, default=fields.Datetime.now)
    texto = fields.Text(string='Detalle')
    nivel = fields.Selection([
        ('info', 'Información'),
        ('warning', 'Advertencia'),
        ('error', 'Error')
    ], string='Nivel', default='info')
    conciliacion_id = fields.Many2one('conciliacion.stock', string='Conciliación', ondelete='cascade')

class ConciliacionStock(models.Model):
    _name = 'conciliacion.stock'
    _description = 'Conciliación de Stock entre Sistemas'

    
    logs = fields.One2many('logs.conciliacion.stock', 'conciliacion_id', string='Logs de Conciliación')

    name = fields.Char(string='Nombre', default='Nueva Conciliación', required=True)
    fecha_creacion = fields.Datetime(string='Fecha de Creación', default=fields.Datetime.now, readonly=True)
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('en_proceso', 'En Proceso'),
        ('completado', 'Completado'),
        ('completado_con_errores', 'Completado con Errores'),
        ('error', 'Error Fatal')
    ], string='Estado', default='borrador')

    ubicacionSalida = fields.Many2one('stock.location', string='Ubicación de Salida', domain=[('usage', '=', 'internal')], required=True)
    ubicacionDestino = fields.Many2one('stock.location', string='Ubicación de destino', domain=[('usage', '=', 'internal')], required=True)
    # Campo calculado para el nombre de visualización
    display_name = fields.Char(string='Nombre', compute='_compute_display_name', store=True)

    diferenciaMinima = fields.Integer(string='Diferencia Mínima para Registrar', default=1, help='Cantidad mínima de diferencia para que se registre una acción de ajuste.')


    @api.model
    def cron_conciliacionStock(self):
        
        self.env['conciliacion.stock'].create({
            'name': 'Conciliación Automática - ' + fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'ubicacionSalida': self.env['integracion_wis.integracion_wis'].search([], limit=1).ubicacionSalida.id,
            'ubicacionDestino': self.env['integracion_wis.integracion_wis'].search([], limit=1).ubicacionDestino.id,
            'diferenciaMinima': self.env['integracion_wis.integracion_wis'].search([], limit=1).diferenciaMinima,
            'partner': self.env['integracion_wis.integracion_wis'].search([], limit=1).partner.id if self.env['integracion_wis.integracion_wis'].search([], limit=1).partner else False,
            'estado': 'borrador',
        }).conciliarStock();




    def conciliarStock(self):
        _logger.info("Iniciando proceso de conciliación de stock...")
        try:
            inicio = fields.Datetime.now()
            self.estado = 'en_proceso'
            productos = self.env['product.template'].search([
                ('integracion_wms', '=', True),
                ('active', '=', True)
            ])

            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
            if not datosAPI or not datosAPI.apiLink:
                raise ValidationError("No se encuentran todos los datos para una consulta a la API")

            if not productos:
                raise ValidationError("No se encontraron productos para conciliar stock.")

            for producto in productos:
                variantes = producto.product_variant_ids
                if not variantes:
                    self.env['logs.conciliacion.stock'].create({
                        'texto': f'El producto {producto.name} no tiene variantes.',
                        'nivel': 'warning',
                        'conciliacion_id': self.id
                    })
                    continue

                for var in variantes:
                    errores_antes = self.env['logs.conciliacion.stock'].search_count([
                        ('conciliacion_id', '=', self.id),
                        ('nivel', '=', 'error')
                    ])
                    try:
                        datosAPI.conciliarStockProducto(var, self.id, ubicacionSalida=self.ubicacionSalida, ubicacionDestino=self.ubicacionDestino, diferenciaMinima=self.diferenciaMinima)
                    except Exception as e:
                        self.env['logs.conciliacion.stock'].create({
                            'texto': f'Error al actualizar stock del producto {var.display_name}: {str(e)}',
                            'nivel': 'error',
                            'conciliacion_id': self.id
                        })
                        continue

                    errores_despues = self.env['logs.conciliacion.stock'].search_count([
                        ('conciliacion_id', '=', self.id),
                        ('nivel', '=', 'error')
                    ])
                    if errores_despues == errores_antes:
                        self.env['logs.conciliacion.stock'].create({
                            'texto': f'Stock del producto {var.display_name} procesado correctamente.',
                            'nivel': 'info',
                            'conciliacion_id': self.id
                        })

            errores = self.logs.filtered(lambda l: l.nivel == 'error' and l.fecha >= inicio)
            if errores:
                self.estado = 'completado_con_errores'
                self.env['logs.conciliacion.stock'].create({
                    'texto': f'Conciliación completada CON ERRORES ({len(errores)} errores encontrados).',
                    'nivel': 'warning',
                    'conciliacion_id': self.id
                })
            else:
                self.estado = 'completado'
                self.env['logs.conciliacion.stock'].create({
                    'texto': 'Conciliación de stock completada exitosamente.',
                    'nivel': 'info',
                    'conciliacion_id': self.id
                })
            _logger.info("Proceso de conciliación de stock completado.")

        except Exception as e:
            self.estado = 'error'
            self.env['logs.conciliacion.stock'].create({
                'texto': f'Error fatal en la conciliación de stock: {str(e)}',
                'nivel': 'error',
                'conciliacion_id': self.id
            })
            raise


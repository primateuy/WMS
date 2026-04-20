from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging
import requests

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    integracion_wms = fields.Boolean(
        string='Integración con WMS',
        help='Habilitar la integración con el sistema de WIS',
        default=False,
    )
        
    def consultaStock(self):
        """Método específico para template que consulta stock de todas las variantes"""
        if not self.integracion_wms:
            raise ValidationError("El template no está marcado para integración con WMS")
        
        variantes_wms = self.product_variant_ids.filtered(lambda v: v.integracion_wms and v.codigo_unico)
        
        if not variantes_wms:
            raise ValidationError("No hay variantes configuradas para integración con WMS")
        
        if len(variantes_wms) == 1:
            return variantes_wms.consultaStock()
        
        resultados = []
        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")
        
        total_stock = 0
        resumen_texto = f"Stock de variantes para {self.name}:\n\n"
        
        for variant in variantes_wms:
            try:
                response = datosAPI.consultaStock(variant)
                total_stock += response
                resumen_texto += f"• {variant.display_name}: {response} unidades\n"
            except Exception as e:
                resumen_texto += f"• {variant.display_name}: Error - {str(e)}\n"
        
        resumen_texto += f"\nTotal combinado: {total_stock} unidades"
        
        wizard = self.env['integracion_wis.wizard_stock'].create({
            'product_id': variantes_wms[0].id,
            'stock_disponible': total_stock,
            'resultado_consulta': resumen_texto
        })
        
        return {
            'name': f'Stock WMS - {self.name}',
            'type': 'ir.actions.act_window',
            'res_model': 'integracion_wis.wizard_stock',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }
            

    def enviar_variantes_wms(self):
        """Envía todas las variantes del template a WMS"""
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")
        for variant in self.product_variant_ids:
            if variant.type == 'product':
                try:
                    response = variant.enviarWS()
                    variant.with_context(_avoid_wms=True).write({
                        'codigo_interfaz_wms': response.get('numeroInterfaz'),
                        'codigo_unico': response.get('codigoUnico')
                    })
                    _logger.info(f"Variante {variant.name} enviada a WMS: {response}")
                except Exception as e:
                    _logger.error(f"Error enviando variante {variant.name} a WMS: {str(e)}")

    @api.model
    def create(self, vals):
        res = super(ProductTemplate, self).create(vals)

        if res.integracion_wms and res.type == 'product':
            try:
                self._procesar_variantes_wms_directo(res)
            except Exception as e:
                _logger.error(f"Error procesando variantes para template {res.name}: {str(e)}")
        
        return res

    def _procesar_variantes_wms_directo(self, template):
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            return
        template = template.with_context(_avoid_wms=True)
        
        if template.product_variant_ids:
            _logger.info(f"Procesando {len(template.product_variant_ids)} variantes para template {template.name}")
            
            template.product_variant_ids.with_context(_avoid_wms=True).write({
                'integracion_wms': True
            })
            
            for variant in template.product_variant_ids:
                if variant.type == 'product':
                    try:
                        response = variant.enviarWS()
                        variant.with_context(_avoid_wms=True).write({
                            'codigo_interfaz_wms': response.get('numeroInterfaz'),
                            'codigo_unico': response.get('codigoUnico')
                        })
                        _logger.info(f"Variante {variant.name} enviada a WMS: {response}")
                    except Exception as e:
                        _logger.error(f"Error enviando variante {variant.name} a WMS: {str(e)}")

    def write(self, vals):
        res = super(ProductTemplate, self).write(vals)

        if vals.get('integracion_wms') and self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            for record in self:
                if record.type == 'product':
                    record.product_variant_ids.with_context(_avoid_wms=True).write({
                        'integracion_wms': True
                    })
                    
                    record.enviar_variantes_wms()

                    
        
        return res

    def _create_variant_ids(self):
        res = super(ProductTemplate, self)._create_variant_ids()

        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            return res

        for template in self:
            if template.integracion_wms and template.type == 'product':

                new_variants = template.product_variant_ids.filtered(
                    lambda v: not v.codigo_interfaz_wms and not v.codigo_unico and v.integracion_wms
                )
                if new_variants:
                    _logger.info(f"Procesando {len(new_variants)} nuevas variantes para template {template.name}")
                    
                    for variant in new_variants:
                        try:
                            response = variant.enviarWS()
                            variant.with_context(_avoid_wms=True).write({
                                'codigo_interfaz_wms': response.get('numeroInterfaz'),
                                'codigo_unico': response.get('codigoUnico')
                            })
                        except Exception as e:
                            raise ValidationError(f"Error enviando nueva variante {variant.name} a WMS: {str(e)}")
        
        return res

class Product(models.Model):
    _inherit = 'product.product'

    integracion_wms = fields.Boolean(
        string='Integración con WMS',
        help='Habilitar la integración con el sistema de WIS',
        default=False,
    )

    codigo_interfaz_wms = fields.Char(
        string='Código de interfaz WMS',
        help='Código único del producto en el sistema de WIS',
        required=False,
    )

    codigo_unico = fields.Char(
        string='Código identificatorio WIS'
    )

    ajusteStockManual = fields.Boolean(
        string='Habilitar ajuste manual',
        help='Si no se habilita la opción los ajustes de inventario se harán automáticamente',
        default=True
    )

    wms_log_ids = fields.One2many(
        'product.wms.log', 
        'product_id', 
        string='Logs de WMS',
        help='Historial de operaciones con WMS'
    )
    
    wms_last_sync = fields.Datetime(
        string='Última Sincronización',
        readonly=True,
        help='Fecha y hora de la última sincronización con WMS'
    )
    
    wms_sync_status = fields.Selection([
        ('pending', 'Pendiente'),
        ('success', 'Exitoso'),
        ('error', 'Error'),
        ('sync', 'Sincronizado')
    ], string='Estado Sincronización', default='pending', readonly=True)

    def _agregar_log_wms(self, operacion, resultado='success', detalle='', response_data=None):
        """Método helper para agregar logs de WMS"""
        self.env['product.wms.log'].create({
            'product_id': self.id,
            'operacion': operacion,
            'resultado': resultado,
            'detalle': detalle,
            'response_data': str(response_data) if response_data else '',
            'fecha': fields.Datetime.now(),
            'usuario_id': self.env.user.id
        })
        
        self.with_context(_avoid_wms=True).write({
            'wms_last_sync': fields.Datetime.now(),
            'wms_sync_status': resultado if resultado in ['success', 'error'] else 'sync'
        })

    def consultaStock(self):
        try:
            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1);
            if not datosAPI or not datosAPI.apiLink:
                raise ValidationError("No se encuentran todos los datos para una consulta a la API")
            
            if not self.integracion_wms:
                raise ValidationError("El producto no está marcado para integración con WMS")
            
            if not self.codigo_unico:
                raise ValidationError("El producto no tiene un código único asignado en WMS")

            response = datosAPI.consultaStock(self);
            
            self._agregar_log_wms(
                operacion='consulta_stock',
                resultado='success',
                detalle=f'Stock disponible: {response}',
                response_data=response
            )
        
            wizard = self.env['integracion_wis.wizard_stock'].create({
                    'product_id': self.id,
                    'stock_disponible': response,
                    'resultado_consulta': 'Stock disponible: ' + str(response)
                })

            return {
                    'name': 'Consultar Stock en WMS',
                    'type': 'ir.actions.act_window',
                    'res_model': 'integracion_wis.wizard_stock',
                    'res_id': wizard.id,
                    'view_mode': 'form',
                    'target': 'new',
                    'context': self.env.context,
                }
        except Exception as e:
            self._agregar_log_wms(
                operacion='consulta_stock',
                resultado='error',
                detalle=f'Error: {str(e)}'
            )
            raise
        
    def saveBarcode(self):
        try:
            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
            if not datosAPI or not datosAPI.apiLink:
                raise ValidationError("No se encuentran todos los datos para una consulta a la API")

            result = datosAPI.insertarBarcode(self)
            
            self._agregar_log_wms(
                operacion='enviar_barcode',
                resultado='success',
                detalle=f'Código de barras enviado: {self.barcode}',
                response_data=result
            )
            
            return result
        except Exception as e:
            self._agregar_log_wms(
                operacion='enviar_barcode',
                resultado='error',
                detalle=f'Error enviando código de barras: {str(e)}'
            )
            raise

    def enviarWS(self):
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")
        try:
            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
            if not datosAPI or not datosAPI.apiLink:
                raise ValidationError("No se encuentran todos los datos para una consulta a la API")

            result = datosAPI.insertarProducto(self)

            if result and isinstance(result, dict):
                update_vals = {}
                if result.get('numeroInterfaz'):
                    update_vals['codigo_interfaz_wms'] = result.get('numeroInterfaz')
                if result.get('codigoUnico'):
                    update_vals['codigo_unico'] = result.get('codigoUnico')
                if update_vals:
                    self.with_context(_avoid_wms=True).write(update_vals)

            if self.barcode:
                self.saveBarcode()

            self._agregar_log_wms(
                operacion='enviar_producto',
                resultado='success',
                detalle=f'Producto enviado exitosamente. Códigos: Interface={result.get("numeroInterfaz", "N/A")}, Único={result.get("codigoUnico", "N/A")}',
                response_data=result
            )

            return result

        except Exception as e:
            self._agregar_log_wms(
                operacion='enviar_producto',
                resultado='error',
                detalle=f'Error enviando producto: {str(e)}'
            )
            raise

    @api.model
    def create(self, vals):
        res = super(Product, self).create(vals)
        if (not self.env.context.get('_avoid_wms') and
            self.env['integracion_wis.integracion_wis']._comunicacion_habilitada() and
            res.integracion_wms and
            res.type == 'product' and
            not res.product_tmpl_id.integracion_wms):
            try:
                response = res.enviarWS()
                res.with_context(_avoid_wms=True).write({
                    'codigo_interfaz_wms': response.get('numeroInterfaz'),
                    'codigo_unico': response.get('codigoUnico')
                })

                if 'barcode' in vals and vals.get('barcode') and res.codigo_unico:
                    res.saveBarcode();
            except Exception as e:
                raise ValidationError(f"Error enviando producto {res.name} a WMS: {str(e)}")
        
        return res


        
        

    CAMPOS_WIS = {'name', 'default_code', 'description', 'active', 'integracion_wms', 'barcode',
                  'list_price', 'standard_price', 'taxes_id', 'uom_id', 'uom_po_id'}

    def write(self, vals):
        avoid_recursion = self.env.context.get('_avoid_wms', False)
        res = super(Product, self).write(vals)

        if not avoid_recursion and self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            hay_cambios_relevantes = bool(self.CAMPOS_WIS & set(vals.keys()))
            if hay_cambios_relevantes:
                for record in self:
                    if record.integracion_wms and record.type == 'product':
                        try:
                            record.enviarWS()
                            _logger.info(f"Producto {record.name} actualizado en WMS")
                        except Exception as e:
                            _logger.error(f"Error enviando producto {record.name} a WMS: {str(e)}")

        return res
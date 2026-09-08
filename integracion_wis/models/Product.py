from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging
import json
import requests

from .wis_sync_queue import PRIORIDAD_BAJA, PRIORIDAD_NORMAL

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
        datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
        
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
        """Envía todas las variantes del template a WMS, en lote.

        Es una acción explícita del usuario sobre un template concreto: se
        resuelve en el momento. Con el envío por lote, un template de 232
        variantes son 2 llamadas HTTP y no 232.
        """
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")

        variantes = self.mapped('product_variant_ids').filtered(lambda v: v.type == 'product')
        if not variantes:
            return

        try:
            variantes._enviar_wms_en_lote(motivo='envío de variantes del template')
        except Exception as e:
            _logger.error(f"Error enviando variantes a WMS: {str(e)}")

    @api.model_create_multi
    def create(self, vals_list):
        templates = super(ProductTemplate, self).create(vals_list)

        if self.env.context.get('_avoid_wms'):
            return templates

        a_procesar = templates.filtered(
            lambda t: t.integracion_wms and t.type == 'product'
        )
        for template in a_procesar:
            try:
                self._procesar_variantes_wms_directo(template)
            except Exception as e:
                _logger.error(f"Error procesando variantes para template {template.name}: {str(e)}")

        return templates

    def _procesar_variantes_wms_directo(self, template):
        """Marca las variantes del template y las encola para integrar.

        El alta de un template es un camino automático: no se hace la llamada
        a WIS acá adentro para no dejar al usuario esperando (un template con
        cientos de variantes tarda minutos). Va a la cola, que las procesa por
        lote en segundo plano.
        """
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            return

        variantes = template.product_variant_ids.filtered(lambda v: v.type == 'product')
        if not variantes:
            return

        _logger.info(f"Encolando {len(variantes)} variantes para template {template.name}")

        variantes.with_context(_avoid_wms=True).write({'integracion_wms': True})

        self.env['wis.sync.queue']._encolar(
            variantes,
            origen='template',
            origen_ref=template.name,
            prioridad=PRIORIDAD_NORMAL,
        )

    def write(self, vals):
        res = super(ProductTemplate, self).write(vals)

        # `_avoid_wms` es el guard anti-recursión del módulo: product.product lo
        # respetaba y product.template no, así que una escritura interna sobre
        # el template terminaba disparando el envío igual.
        if self.env.context.get('_avoid_wms'):
            return res

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
                    # Se encolan en vez de enviarse acá: agregar un atributo a
                    # un producto puede generar cientos de variantes, y antes
                    # cada una era una llamada HTTP sincrónica. Además, un WIS
                    # caído lanzaba ValidationError e impedía crear variantes
                    # en Odoo — el WMS no debe bloquear el maestro de productos.
                    _logger.info(f"Encolando {len(new_variants)} nuevas variantes para template {template.name}")
                    self.env['wis.sync.queue']._encolar(
                        new_variants,
                        origen='template',
                        origen_ref=template.name,
                        prioridad=PRIORIDAD_BAJA,
                    )

        return res

    def action_integrar_wms_masivo(self):
        """Acción masiva desde el listado de Productos: encola las variantes.

        Marca los templates para integración y encola todas sus variantes. El
        usuario sigue trabajando: el cron las procesa por lote en segundo plano.
        """
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")

        templates = self.filtered(lambda t: t.type == 'product')
        if not templates:
            raise ValidationError("Ninguno de los productos seleccionados es almacenable.")

        templates.filtered(lambda t: not t.integracion_wms).with_context(
            _avoid_wms=True).write({'integracion_wms': True})

        variantes = templates.mapped('product_variant_ids').filtered(lambda v: v.type == 'product')
        variantes.filtered(lambda v: not v.integracion_wms).with_context(
            _avoid_wms=True).write({'integracion_wms': True})

        entradas = self.env['wis.sync.queue']._encolar(
            variantes,
            origen='masiva',
            origen_ref=f"{len(templates)} producto(s)",
            prioridad=PRIORIDAD_NORMAL,
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Integración con WIS encolada',
                'message': (f"{len(entradas)} variante(s) en cola. Se integran en segundo "
                            f"plano; podés seguir trabajando. El avance se ve en "
                            f"Integración WIS → Cola de productos."),
                'type': 'success',
                'sticky': False,
            },
        }

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
        string='Código identificatorio WIS',
        index=True,
        copy=False,
    )

    @api.constrains('codigo_unico')
    def _check_codigo_unico_wis(self):
        """El código WIS identifica al producto en el WMS: no puede repetirse.

        No se usa un _sql_constraints porque las bases existentes pueden tener
        duplicados heredados de la generación aleatoria anterior, y el índice
        único haría fallar la actualización del módulo. Esta validación impide
        que se sigan generando duplicados nuevos.
        """
        for record in self:
            if not record.codigo_unico:
                continue
            duplicado = self.with_context(active_test=False).search([
                ('codigo_unico', '=', record.codigo_unico),
                ('id', '!=', record.id),
            ], limit=1)
            if duplicado:
                raise ValidationError(
                    f"El código WIS '{record.codigo_unico}' ya está asignado al producto "
                    f"'{duplicado.display_name}'. Cada producto debe tener un código único en WIS."
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

    def _agregar_log_wms(self, operacion, resultado='success', detalle='', response_data=None, payload_enviado=None):
        self.env['product.wms.log'].create({
            'product_id': self.id,
            'operacion': operacion,
            'resultado': resultado,
            'detalle': detalle,
            'payload_enviado': payload_enviado or '',
            'response_data': json.dumps(response_data, ensure_ascii=False, indent=2) if isinstance(response_data, dict) else (str(response_data) if response_data else ''),
            'fecha': fields.Datetime.now(),
            'usuario_id': self.env.user.id,
        })
        self.with_context(_avoid_wms=True).write({
            'wms_last_sync': fields.Datetime.now(),
            'wms_sync_status': resultado if resultado in ['success', 'error'] else 'sync',
        })

    def consultaStock(self):
        try:
            datosAPI = self.env['integracion_wis.integracion_wis']._get_config();
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
            datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
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

    def action_integrar_wms_masivo(self):
        """Acción masiva desde el listado de Variantes: encola las seleccionadas."""
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")

        variantes = self.filtered(lambda v: v.type == 'product')
        if not variantes:
            raise ValidationError("Ninguna de las variantes seleccionadas es almacenable.")

        variantes.filtered(lambda v: not v.integracion_wms).with_context(
            _avoid_wms=True).write({'integracion_wms': True})

        entradas = self.env['wis.sync.queue']._encolar(
            variantes,
            origen='masiva',
            origen_ref=f"{len(variantes)} variante(s)",
            prioridad=PRIORIDAD_NORMAL,
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Integración con WIS encolada',
                'message': (f"{len(entradas)} variante(s) en cola. Se integran en segundo "
                            f"plano; podés seguir trabajando. El avance se ve en "
                            f"Integración WIS → Cola de productos."),
                'type': 'success',
                'sticky': False,
            },
        }

    def enviarWS(self):
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")
        payload_log = ''
        try:
            datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
            if not datosAPI or not datosAPI.apiLink:
                raise ValidationError("No se encuentran todos los datos para una consulta a la API")

            tracking = self.tracking
            unidad_wis = (self.uom_id.wis_code or '').strip() if self.uom_id else 'UND'
            payload_log = json.dumps({
                'codigoProducto': self.codigo_unico or '(nuevo)',
                'descripcion': self.name,
                'unidadMedida': unidad_wis or 'UND',
                'pesoNeto': self.weight,
                'precioVenta': self.list_price,
                'categoria1': self.categ_id.name if self.categ_id else '',
                'activo': self.active,
                'tracking': tracking,
                'tipoManejoFecha': 'F' if tracking in ('lot', 'serial') else 'D',
                'manejoIdentificador': 'L' if tracking in ('lot', 'serial') else 'P',
            }, ensure_ascii=False, indent=2)

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
                detalle=f'Enviado. Interface={result.get("numeroInterfaz", "N/A")}, Único={result.get("codigoUnico", "N/A")}',
                response_data=result,
                payload_enviado=payload_log,
            )

            return result

        except Exception as e:
            self._agregar_log_wms(
                operacion='enviar_producto',
                resultado='error',
                detalle=f'Error enviando producto: {str(e)}',
                payload_enviado=payload_log,
            )
            raise

    @api.model_create_multi
    def create(self, vals_list):
        records = super(Product, self).create(vals_list)

        if (self.env.context.get('_avoid_wms') or
                not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada()):
            return records

        # Las variantes de un template marcado para WMS las maneja
        # `_create_variant_ids` / `_procesar_variantes_wms_directo`, en lote.
        a_enviar = records.filtered(
            lambda r: r.integracion_wms and r.type == 'product'
            and not r.product_tmpl_id.integracion_wms
        )
        if not a_enviar:
            return records

        try:
            if len(a_enviar) > 1:
                a_enviar._enviar_wms_en_lote(motivo='alta de variantes')
            else:
                a_enviar.enviarWS()
        except Exception as e:
            raise ValidationError(f"Error enviando producto {a_enviar[0].name} a WMS: {str(e)}")

        return records



    # Campos que efectivamente viajan en el payload de WIS
    # (ver `_build_producto_payload` en models.py). Cualquier otro campo NO
    # debe disparar una llamada HTTP: `standard_price`, por ejemplo, se
    # recalcula solo con AVCO/FIFO al validar cada recepción, y tenerlo acá
    # hacía que validar una recepción de N líneas disparara N llamadas a WIS
    # dentro de la transacción.
    CAMPOS_WIS = {'name', 'active', 'integracion_wms', 'barcode',
                  'list_price', 'weight', 'uom_id', 'categ_id'}

    def write(self, vals):
        avoid_recursion = self.env.context.get('_avoid_wms', False)
        res = super(Product, self).write(vals)

        if not avoid_recursion and self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            hay_cambios_relevantes = bool(self.CAMPOS_WIS & set(vals.keys()))
            if hay_cambios_relevantes:
                a_sincronizar = self.filtered(
                    lambda r: r.integracion_wms and r.type == 'product'
                )
                # Con muchas variantes conviene un solo lote en vez de una
                # llamada HTTP por registro.
                if len(a_sincronizar) > 1:
                    a_sincronizar._enviar_wms_en_lote(motivo='actualización masiva')
                else:
                    for record in a_sincronizar:
                        try:
                            record.enviarWS()
                            _logger.info(f"Producto {record.name} actualizado en WMS")
                        except Exception as e:
                            _logger.error(f"Error enviando producto {record.name} a WMS: {str(e)}")

        return res

    def _enviar_wms_en_lote(self, motivo=''):
        """Envía este recordset a WIS en lotes (una llamada cada N productos).

        Es el camino rápido: `enviarWS()` hace 1 request por producto (más 2 por
        código de barras). Para 232 variantes eso son ~696 requests; por lote
        son 2. Devuelve el dict de resultado de `insertarProductosMasivo`.
        """
        datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        variantes = self.filtered(lambda v: v.type == 'product')
        if not variantes:
            return {'enviados': 0, 'errores': 0, 'errores_detalle': [], 'barcodes_enviados': 0}

        _logger.info("[WIS] Envío en lote de %d variantes (%s)", len(variantes), motivo or 's/motivo')
        resultado = datosAPI.insertarProductosMasivo(variantes)

        # Un log por variante, con el resultado del lote.
        errores_texto = "\n".join(resultado.get('errores_detalle', []))
        for variante in variantes:
            fallo = variante.codigo_unico and any(
                variante.codigo_unico in e for e in resultado.get('errores_detalle', [])
            )
            variante._agregar_log_wms(
                operacion='enviar_producto',
                resultado='error' if fallo else 'success',
                detalle=(f"Envío en lote ({motivo}). "
                         f"Enviados: {resultado['enviados']} | Errores: {resultado['errores']}"
                         + (f"\n{errores_texto}" if fallo else '')),
                response_data=None,
            )

        return resultado
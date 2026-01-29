from odoo import models, fields, api
import requests
from odoo.exceptions import ValidationError
import base64;
import gzip;


import logging

_logger = logging.getLogger(__name__)

class StockPicking(models.Model):

    _inherit = 'stock.picking'


    idPedidoWMS = fields.Char(
        string='ID Pedido WMS', help="Identificador del pedido en el sistema WMS.")

    codigo_unico = fields.Char(
        string = "Código WMS identificatorio"
    )
    
    state = fields.Selection(selection_add=[
        ('preparado_wms', 'Preparado por WMS')
    ], ondelete={'preparado_wms': 'cascade'})
    
    log_ids = fields.One2many(
        'wms.integracion.log',
        'picking_id',
        string='Logs de Integración WMS'
    )
    
    log_count = fields.Integer(
        string='Cantidad de Logs',
        compute='_compute_log_count'
    )
    
    @api.depends('log_ids')
    def _compute_log_count(self):
        for record in self:
            record.log_count = len(record.log_ids)

    def needs_crossdocking(self):
        self.ensure_one()
        main_stock_location = self.picking_type_id.warehouse_id.lot_stock_id

        if not main_stock_location:
            return False

        for move in self.move_ids_without_package:
            dest = move.location_dest_id

            if dest.usage == 'internal' and dest != main_stock_location:
                return True

        return False

    def generarPDFBase64(self, factura):
        if not factura:
            raise ValidationError("No se ha proporcionado una factura válida.")
        
        try:
            report = self.env['ir.actions.report']
            
            pdf_content, _ = report._render_qweb_pdf(
                report_ref='account.account_invoices_without_payment',
                res_ids=factura.ids
            )

            pdf_compressed = gzip.compress(pdf_content);
            
            pdf_base64 = base64.b64encode(pdf_compressed)
            return pdf_base64.decode("utf-8")
            
        except Exception as e:
            _logger.error("Error generating PDF: %s", str(e))
            raise ValidationError(f"Error generando PDF: {str(e)}")

        
    def enviarWS(self, tipo):

        
        _logger.info(f"OPERACION {self.picking_type_id.name}");

        _logger.info("🔄 Iniciando integración WMS para picking %s", self.picking_type_id.code);

        

        if not self.partner_id:
            raise ValidationError("No se ha asignado un partner")

        if not self.partner_id.codigo_wms or not self.partner_id.codigo_unico:
            raise ValidationError("El partner no se encuentra sincronizado en WIS")

        central_locations = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('company_id', '=', self.env.company.id)
        ])

        # hayCentral = False

        # _logger.info("CENTRALES: %s", str(central_locations));

        # for c in central_locations:
        #     _logger.info("CENTRAL: %s", str(c.name));
        
        # for d in self.location_dest_id:
        #     _logger.info("DESTINO: %s", str(d.name));
        # for c in central_locations:
        #     if c.id == self.location_dest_id.id:
        #         hayCentral = True
        #         break

        # if not hayCentral and tipo == 'AL':
        #     raise ValidationError("Debe tener origen un depósito logístico central")
            
        if self.state != self.picking_type_id.estado_disparo_wms:
            _logger.info("No se ha ejecutado la integración con WIS ya que los estados no coinciden")            
            return
        
        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        if self.picking_type_id.code == 'incoming' and self.partner_id.customer_rank > 0:
            return datosAPI.insertarDevolucion(self);


        pdfData = None
        if tipo == 'EC' and self.picking_type_id.emitir_factura_antes_envio and self.sale_id:
            try:
                if not self.sale_id:
                    raise ValidationError("No hay orden de venta asociada")
                
                facturas_existentes = self.sale_id.invoice_ids.filtered(
                    lambda inv: inv.state == 'posted' and inv.move_type == 'out_invoice'
                )
                
                if facturas_existentes:
                    factura = facturas_existentes[0]
                else:
                    facturas_borrador = self.sale_id.invoice_ids.filtered(
                        lambda inv: inv.state == 'draft' and inv.move_type == 'out_invoice'
                    )
                    
                    if facturas_borrador:
                        factura = facturas_borrador[0]
                        factura.action_post()
                        _logger.info("Factura borrador confirmada: %s", factura.name)
                    else:
                        nuevas_facturas = self.sale_id._create_invoices()
                        
                        if not nuevas_facturas:
                            raise ValidationError("No se pudo crear la factura")
                        

                        facturas = self.sale_id.invoice_ids;
                        factura = facturas[0]
                        factura.action_post()

                pdfData = self.generarPDFBase64(factura);
                _logger.info(pdfData);
                _logger.info("PDF generado para factura: %s", factura.name)
                
            except Exception as e:
                _logger.error("Error procesando factura para picking %s: %s", self.id, str(e))
                raise ValidationError(f"Error procesando factura: {str(e)}")


        if tipo == 'AL' and not self.location_id or not self.location_dest_id:
            
            raise ValidationError("No se han definido las ubicaciones de origen o destino");
        

        try:
            response = datosAPI.insertarPedidos(self, tipo, pdfData);
            return response;
        
        except Exception as e:
            _logger.error("Error en integración WMS para picking %s: %s", self.id, str(e))
            raise ValidationError(f"Error en integración WMS: {str(e)}")


    @api.model
    def create(self, vals):
        res = super(StockPicking, self).create(vals)

        _logger.info("CREANDO UN NUEVO PICKING");
        
        if res.picking_type_id.integracion_wms:
            try:
                tipo = 'NORM'
                if res.picking_type_id.tipo_pedido_wms:
                    tipo = res.picking_type_id.tipo_pedido_wms;

                # if self.metodo_creacion_wms is None:
                #     raise ValidationError("No se ha definido el método de creación en WMS para este tipo de operación.")
                response = res.enviarWS(tipo)
                if response:
                    res.with_context(skip_wms_integration=True).write({
                        'idPedidoWMS': response.get('numeroInterfaz', ''),
                        'codigo_unico': response.get('codigoUnico', '')
                    })
                    
                    # Log de éxito
                    self.env['wms.integracion.log'].create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'info',
                        'modelo': 'stock.picking',
                        'texto': f"Picking {res.name} creado exitosamente en WMS. NumeroInterfaz: {response.get('numeroInterfaz', '')}, CodigoUnico: {response.get('codigoUnico', '')}",
                        'picking_id': res.id,
                        'resultado': 'exito',
                        'detalle': f"Tipo: {tipo}, Partner: {res.partner_id.name if res.partner_id else 'N/A'}"
                    })
                    
            except Exception as e:
                _logger.error("Error en integración WMS durante create: %s", str(e))
                
                # Log de error
                self.env['wms.integracion.log'].create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'stock.picking',
                    'texto': f"Error al crear picking {res.name} en WMS: {str(e)}",
                    'picking_id': res.id,
                    'resultado': 'error',
                    'detalle': f"Tipo operación: {res.picking_type_id.name if res.picking_type_id else 'N/A'}, Partner: {res.partner_id.name if res.partner_id else 'N/A'}"
                })
                
                raise ValidationError(f"Error en integración WMS durante create: {str(e)}")
        return res

    def write(self, vals):
        res = super(StockPicking, self).write(vals)
        
        if not self.env.context.get('skip_wms_integration'):
            for record in self:
                if record.picking_type_id.integracion_wms:
                    tipo = 'NORM'
                    if record.picking_type_id.tipo_pedido_wms:
                        tipo = record.picking_type_id.tipo_pedido_wms;

                    try:
                        record_ctx = record.with_context(skip_wms_integration=True)
                        response = record.enviarWS(tipo)
                        
                        _logger.info("Response de API para picking %s: %s", record.id, response)
                        if response:
                            record_ctx.write({
                                'idPedidoWMS': response.get('numeroInterfaz', ''),
                                'codigo_unico': response.get('codigoUnico', '')
                            })
                            
                            # Log de éxito en actualización
                            self.env['wms.integracion.log'].create({
                                'fecha': fields.Datetime.now(),
                                'nivel': 'info',
                                'modelo': 'stock.picking',
                                'texto': f"Picking {record.name} actualizado exitosamente en WMS. NumeroInterfaz: {response.get('numeroInterfaz', '')}, CodigoUnico: {response.get('codigoUnico', '')}",
                                'picking_id': record.id,
                                'resultado': 'exito',
                                'detalle': f"Estado: {record.state}, Tipo: {tipo}, Campos actualizados: {', '.join(vals.keys())}"
                            })
                            
                    except Exception as e:
                        _logger.error("Error en integración WMS durante write para picking %s: %s", record.id, str(e))
                        
                        # Log de error en actualización
                        self.env['wms.integracion.log'].create({
                            'fecha': fields.Datetime.now(),
                            'nivel': 'error',
                            'modelo': 'stock.picking',
                            'texto': f"Error al actualizar picking {record.name} en WMS: {str(e)}",
                            'picking_id': record.id,
                            'resultado': 'error',
                            'detalle': f"Estado: {record.state}, Tipo operación: {record.picking_type_id.name if record.picking_type_id else 'N/A'}"
                        })
        return res

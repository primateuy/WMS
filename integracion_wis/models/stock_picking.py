from odoo import models, fields, api
import requests
from odoo.exceptions import ValidationError
import base64;
import gzip;


import logging

_logger = logging.getLogger(__name__)

class StockPicking(models.Model):

    _inherit = 'stock.picking'

    wms_estado = fields.Selection(
        selection=[
            ("sin_enviar", "Sin enviar a WMS"),
            ("enviado", "Enviado a WMS"),
            ("preparado", "Preparado por WMS"),
            ("despachado", "Despachado por WMS"),
            ("anulado", "Anulado por WMS"),
        ],
        string="Estado WMS",
        default="sin_enviar",
        tracking=True,
        copy=False,
        index=True,
    )
 
    # ------------------------------------------------------------------
    # Campos WMS - Trazabilidad general
    # ------------------------------------------------------------------
 
    wms_referencia = fields.Char(
        string="Referencia WMS",
        copy=False,
        index=True,
        help="Identificador del pedido en el WMS externo.",
    )
    wms_fecha_confirmacion = fields.Datetime(
        string="Fecha confirmación WMS",
        copy=False,
        readonly=True,
    )
 
    # ------------------------------------------------------------------
    # Campos WMS - Webhook 1: confirmacionMercaderiaPreparada
    # ------------------------------------------------------------------
 
    wms_fecha_preparacion = fields.Datetime(
        string="Fecha preparación WMS",
        copy=False,
        readonly=True,
    )
    wms_observaciones_preparacion = fields.Text(
        string="Observaciones preparación WMS",
        copy=False,
        readonly=True,
    )
 
    # ------------------------------------------------------------------
    # Campos WMS - Webhook 2: confirmacionDespacho
    # ------------------------------------------------------------------
 
    wms_fecha_despacho = fields.Datetime(
        string="Fecha despacho WMS",
        copy=False,
        readonly=True,
    )
    wms_transportadora = fields.Char(
        string="Transportadora WMS",
        copy=False,
        readonly=True,
    )
    wms_nro_remito = fields.Char(
        string="Nro. Remito WMS",
        copy=False,
        readonly=True,
    )
    wms_nro_seguimiento = fields.Char(
        string="Nro. Seguimiento / Tracking",
        copy=False,
        readonly=True,
    )
    wms_cantidad_bultos = fields.Integer(
        string="Cantidad de bultos",
        copy=False,
        readonly=True,
    )
    wms_peso_total = fields.Float(
        string="Peso total (kg)",
        copy=False,
        readonly=True,
        digits=(10, 3),
    )
 
    # ------------------------------------------------------------------
    # Campos WMS - Webhook 3: anulacionOperativa
    # ------------------------------------------------------------------
 
    wms_fecha_anulacion = fields.Datetime(
        string="Fecha anulación WMS",
        copy=False,
        readonly=True,
    )
    wms_motivo_anulacion = fields.Char(
        string="Motivo anulación WMS",
        copy=False,
        readonly=True,
    )


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

        

        
        if not self.partner_id:
            raise ValidationError("No se ha asignado un partner")



        if not self.partner_id.codigo_wms or not self.partner_id.codigo_unico:
            raise ValidationError("El partner no se encuentra sincronizado en WIS")

        

        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        state_actual = str(self.state or '').strip()
        state_objetivo = str(self.picking_type_id.estado_disparo_wis or '').strip()
        

        if state_objetivo and state_actual != state_objetivo:
            _logger.info("Postergando integración: El estado '%s' no coincide con el objetivo '%s'", state_actual, state_objetivo)
            return False

        


        # Es un movimiento que viene de una compra
        if tipo == 'OCI' or self.sale_id:
            return datosAPI.insertarReferenciaRecepcion(self)

        if self.picking_type_id.code == 'incoming':
            if self.partner_id.customer_rank > 0:
                _logger.info("Es una devolución de cliente, se enviará a la API de devoluciones");
                return datosAPI.insertarDevolucion(self)

        if tipo == 'EC':
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

                
                
            except Exception as e:
                _logger.error("Error procesando factura para picking %s: %s", self.id, str(e))
                raise ValidationError(f"Error procesando factura: {str(e)}")


        if tipo == 'AL' and not self.location_id or not self.location_dest_id:
            
            raise ValidationError("No se han definido las ubicaciones de origen o destino");
        

        try:
            response = datosAPI.insertarPedidos(self, tipo);

            
            
            return response;
        
        except Exception as e:
            _logger.error("Error en integración WMS para picking %s: %s", self.id, str(e))
            raise ValidationError(f"Error en integración WMS: {str(e)}")



    def write(self, vals):
        res = super(StockPicking, self).write(vals)
        
        if not self.env.context.get('skip_wms_integration'):
            for record in self:
                if not record.picking_type_id.integracion_wms:
                    continue
                if not record.move_ids:          
                    continue
                if record.wms_estado != 'sin_enviar':
                    continue

                tipo = record.picking_type_id.tipo_pedido_wis or 'NORM'

                try:
                    response = record.enviarWS(tipo)
                    if response is not False:
                        wms_vals = {'wms_estado': 'enviado'}
                        if isinstance(response, dict):
                            wms_vals['idPedidoWMS'] = response.get('numeroInterfaz', '')
                            wms_vals['codigo_unico'] = response.get('codigoUnico', '')
                            
                        record.with_context(skip_wms_integration=True).write(wms_vals)
                        
                        codigo_log = response.get('codigoUnico', '') if isinstance(response, dict) else ''
                        self.env['wms.integracion.log'].create({
                            'fecha': fields.Datetime.now(),
                            'nivel': 'info',
                            'modelo': 'stock.picking',
                            'texto': f"Picking {record.name} enviado a WMS. CodigoUnico: {codigo_log}",
                            'picking_id': record.id,
                            'resultado': 'exito',
                            'detalle': f"Estado: {record.state}, Tipo: {tipo}",
                        })
                except Exception as e:
                    self.env['wms.integracion.log'].create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'error',
                        'modelo': 'stock.picking',
                        'texto': f"Error al enviar picking {record.name} a WMS: {str(e)}",
                        'picking_id': record.id,
                        'resultado': 'error',
                        'detalle': f"Estado: {record.state}, Tipo: {record.picking_type_id.name if record.picking_type_id else 'N/A'}",
                    })
        return res
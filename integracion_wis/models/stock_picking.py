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

        _logger.info(
            "[WIS] enviarWS llamado | picking=%s | tipo=%s | state=%s | partner=%s (id=%s) | picking_type=%s",
            self.name, tipo, self.state,
            self.partner_id.name if self.partner_id else 'SIN PARTNER',
            self.partner_id.id if self.partner_id else None,
            self.picking_type_id.name if self.picking_type_id else 'SIN TIPO',
        )

        state_actual = str(self.state or '').strip()
        state_objetivo = str(self.picking_type_id.estado_disparo_wis or '').strip()

        # 'waiting' cubre tanto 'waiting' (en espera de otra operación)
        # como 'confirmed' (en espera de disponibilidad)
        estados_aceptados = {state_objetivo}
        if state_objetivo == 'waiting':
            estados_aceptados.add('confirmed')

        _logger.info(
            "[WIS] enviarWS | state_actual='%s' | estado_disparo_wis='%s' | estados_aceptados=%s | coincide=%s",
            state_actual, state_objetivo, estados_aceptados, state_actual in estados_aceptados,
        )

        if state_objetivo and state_actual not in estados_aceptados:
            return False

        if not self.partner_id:
            raise ValidationError("No se ha asignado un partner")

        tipo_agente = self.picking_type_id.tipo_agente_wis or 'CLI'
        codigo_agente = self.partner_id.codigo_unico_cliente if tipo_agente == 'CLI' else self.partner_id.codigo_unico_proveedor

        _logger.info(
            "[WIS] enviarWS | tipo_agente_wis config=%s | tipo_agente resuelto=%s | "
            "codigo_unico_cliente='%s' | codigo_unico_proveedor='%s' | codigo_agente resuelto='%s'",
            self.picking_type_id.tipo_agente_wis,
            tipo_agente,
            self.partner_id.codigo_unico_cliente,
            self.partner_id.codigo_unico_proveedor,
            codigo_agente,
        )

        if not codigo_agente:
            raise ValidationError(f"El partner no tiene Identificación WIS {'Cliente' if tipo_agente == 'CLI' else 'Proveedor'}. Sincronicelo primero desde el contacto.")

        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        


        if (tipo == 'OCI' or self.sale_id) and self.picking_type_id.code == 'incoming':
            return datosAPI.insertarReferenciaRecepcion(self)

        if self.picking_type_id.code == 'incoming':
            if self.partner_id.customer_rank > 0 and not self.purchase_id:
                _logger.info("Es una devolución de cliente, se enviará a la API de devoluciones")
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



    @api.model_create_multi
    def create(self, vals_list):
        records = super(StockPicking, self).create(vals_list)
        if self.env.context.get('skip_wms_integration'):
            return records
        for record in records:
            _logger.info("[WIS] create | picking=%s | state=%s | integracion=%s | partner=%s | wms_estado=%s",
                record.name, record.state,
                record.picking_type_id.integracion_wms,
                record.partner_id.name if record.partner_id else 'VACIO',
                record.wms_estado,
            )
            if not record.picking_type_id.integracion_wms:
                continue
            if not record.move_ids:
                continue
            if not record.partner_id:
                continue
            if record.wms_estado != 'sin_enviar':
                continue
            estado_obj = record.picking_type_id.estado_disparo_wis or ''
            estados_aceptados = {estado_obj}
            if estado_obj == 'waiting':
                estados_aceptados.add('confirmed')
            if estado_obj and record.state not in estados_aceptados:
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
                    _logger.info("[WIS] create | picking=%s enviado a WMS", record.name)
            except Exception as e:
                _logger.exception("[WIS] create | error en picking=%s: %s", record.name, e)
        return records

    def _enviar_wis_si_corresponde(self, hook_name):
        """Dispara integración WIS según estado actual del picking."""
        if self.env.context.get('skip_wms_integration'):
            return
        for record in self:
            estado_obj = record.picking_type_id.estado_disparo_wis or ''
            estados_aceptados = {estado_obj}
            if estado_obj == 'waiting':
                estados_aceptados.add('confirmed')
            _logger.info(
                "[WIS] %s | picking=%s | state=%s | integracion=%s | partner=%s | wms_estado=%s | estado_disparo=%s",
                hook_name, record.name, record.state,
                record.picking_type_id.integracion_wms,
                record.partner_id.name if record.partner_id else 'SIN PARTNER',
                record.wms_estado, estado_obj,
            )
            if not record.picking_type_id.integracion_wms:
                continue
            if not record.move_ids:
                continue
            if not record.partner_id:
                continue
            if record.wms_estado != 'sin_enviar':
                continue
            if not estado_obj or record.state not in estados_aceptados:
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
                    _logger.info("[WIS] %s | picking=%s enviado a WMS", hook_name, record.name)
            except Exception as e:
                _logger.exception("[WIS] %s | error en picking=%s: %s", hook_name, record.name, e)

    def action_confirm(self):
        res = super().action_confirm()
        self._enviar_wis_si_corresponde('action_confirm')
        return res

    def action_assign(self):
        res = super().action_assign()
        self._enviar_wis_si_corresponde('action_assign')
        return res

    def _action_done(self):
        res = super(StockPicking, self)._action_done()
        if self.env.context.get('skip_wms_integration'):
            return res
        for record in self:
            _logger.info("[WIS] _action_done | picking=%s | type=%s | integracion=%s | partner=%s | wms_estado=%s",
                record.name,
                record.picking_type_id.name,
                record.picking_type_id.integracion_wms,
                record.partner_id.name if record.partner_id else 'SIN PARTNER',
                record.wms_estado,
            )
            if not record.picking_type_id.integracion_wms:
                continue
            if not record.move_ids:
                continue
            if not record.partner_id:
                continue
            if record.wms_estado != 'sin_enviar':
                continue
            if record.picking_type_id.estado_disparo_wis not in ('done', False, ''):
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
                    _logger.info("[WIS] _action_done | picking=%s enviado a WMS", record.name)
            except Exception as e:
                _logger.exception("[WIS] _action_done | error en picking=%s: %s", record.name, e)
        return res

    def write(self, vals):
        res = super(StockPicking, self).write(vals)

        if not self.env.context.get('skip_wms_integration') and 'scheduled_date' in vals:
            for record in self:
                if record.wms_estado != 'enviado' or not record.picking_type_id.integracion_wms:
                    continue
                datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
                if not datosAPI:
                    continue
                try:
                    datosAPI.actualizarReferenciaRecepcion(record)
                    self.env['wms.integracion.log'].create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'info',
                        'modelo': 'stock.picking',
                        'texto': f"Fecha programada actualizada en WMS para {record.name}",
                        'picking_id': record.id,
                        'resultado': 'exito',
                        'detalle': f"Nueva fecha: {vals.get('scheduled_date')}",
                    })
                except Exception as e:
                    self.env['wms.integracion.log'].create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'error',
                        'modelo': 'stock.picking',
                        'texto': f"Error al actualizar fecha en WMS para {record.name}: {str(e)}",
                        'picking_id': record.id,
                        'resultado': 'error',
                    })

        if not self.env.context.get('skip_wms_integration') and 'state' in vals:
            nuevo_state = vals.get('state')
            for record in self:
                _logger.info(
                    "[WIS] write state | picking=%s | nuevo_state=%s | integracion_wms=%s | "
                    "move_ids=%s | wms_estado=%s",
                    record.name, nuevo_state,
                    record.picking_type_id.integracion_wms,
                    bool(record.move_ids),
                    record.wms_estado,
                )
                if nuevo_state == 'done':
                    continue  # manejado por _action_done
                if not record.picking_type_id.integracion_wms:
                    continue
                if not record.move_ids:
                    continue
                if not record.partner_id:
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
                    raise
        return res


class StockMove(models.Model):
    _inherit = 'stock.move'

    def write(self, vals):
        res = super().write(vals)
        if 'product_uom_qty' in vals:
            for move in self:
                picking = move.picking_id
                if not picking or picking.wms_estado != 'enviado' or not picking.picking_type_id.integracion_wms:
                    continue
                datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
                if not datosAPI:
                    continue
                try:
                    datosAPI.actualizarReferenciaRecepcion(picking)
                    self.env['wms.integracion.log'].create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'info',
                        'modelo': 'stock.move',
                        'texto': f"Cantidad de demanda actualizada en WMS para {picking.name}",
                        'picking_id': picking.id,
                        'resultado': 'exito',
                        'detalle': f"Producto: {move.product_id.name}, Nueva cantidad: {vals.get('product_uom_qty')}",
                    })
                except Exception as e:
                    self.env['wms.integracion.log'].create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'error',
                        'modelo': 'stock.move',
                        'texto': f"Error al actualizar cantidad en WMS para {picking.name}: {str(e)}",
                        'picking_id': picking.id,
                        'resultado': 'error',
                    })
        return res
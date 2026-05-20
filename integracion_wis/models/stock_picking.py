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

    wms_origen = fields.Selection(
        selection=[
            ("manual", "Manual"),
            ("recepcion", "Recepción del WMS"),
            ("mercaderia_preparada", "Mercadería preparada por WMS"),
            ("despacho", "Despachado por WMS"),
            ("anulacion", "Anulado por WMS"),
            ("ajuste", "Ajuste de inventario WMS"),
            ("almacenamiento", "Almacenamiento WMS"),
            ("crossdocking", "Crossdocking"),
        ],
        string="Origen WMS",
        default="manual",
        tracking=True,
        copy=False,
        index=True,
        help="Tipo de evento WMS que generó o modificó este picking. "
             "Complementa a wms_estado (ciclo de vida) identificando el origen funcional.",
    )

    wis_location_id = fields.Char(
        string="WIS Location ID",
        copy=False,
        index=True,
        help="Identificador de ubicación logística informado por WIS.",
    )
    wis_panel_id = fields.Char(
        string="WIS Panel ID",
        copy=False,
        index=True,
        help="Identificador de panel informado por WIS.",
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
    wms_descripcion_camion = fields.Char(
        string="Descripción camión WMS",
        copy=False,
        readonly=True,
        help="Descripción del camión informada por WIS en el webhook confirmacionPedido. "
             "Campo informativo, no impacta en la lógica.",
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

    wms_nro_caja = fields.Char(
        string="Nro. de Caja (FINT)",
        copy=False,
        help="Número de caja específico para pedidos de fin de temporada (FINT). "
             "Si se completa, se enviará al WMS como LPN con tipo FINTEMP.",
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

    def _get_wis_partner(self):
        """Partner efectivo para WIS: partner_id del picking, o partner de la compañía del tipo de operación como fallback."""
        self.ensure_one()
        if self.partner_id:
            return self.partner_id
        company = self.picking_type_id.company_id
        return company.partner_id if company else False

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

        partner = self._get_wis_partner()
        if not partner:
            raise ValidationError("No se ha asignado un partner y la compañía del tipo de operación no tiene partner configurado")
        if not self.partner_id:
            _logger.info("[WIS] enviarWS | sin partner_id, usando partner de compañía como fallback: %s (id=%s)", partner.name, partner.id)

        tipo_agente = self.picking_type_id.tipo_agente_wis or 'CLI'
        codigo_agente = partner.codigo_unico_cliente if tipo_agente == 'CLI' else partner.codigo_unico_proveedor

        _logger.info(
            "[WIS] enviarWS | tipo_agente_wis config=%s | tipo_agente resuelto=%s | "
            "codigo_unico_cliente='%s' | codigo_unico_proveedor='%s' | codigo_agente resuelto='%s'",
            self.picking_type_id.tipo_agente_wis,
            tipo_agente,
            partner.codigo_unico_cliente,
            partner.codigo_unico_proveedor,
            codigo_agente,
        )

        if not codigo_agente:
            fallback_info = f" (compañía fallback: {partner.name})" if not self.partner_id else ""
            raise ValidationError(f"El partner{fallback_info} no tiene Identificación WIS {'Cliente' if tipo_agente == 'CLI' else 'Proveedor'}. Sincronicelo primero desde el contacto.")

        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        


        TIPOS_DEVOLUCION = ('ODM', 'ODT', 'ODW', 'ODFT')

        if tipo in TIPOS_DEVOLUCION:
            return datosAPI.insertarDevolucion(self)

        if (tipo == 'OCI' or self.sale_id) and self.picking_type_id.code == 'incoming':
            return datosAPI.insertarReferenciaRecepcion(self)

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
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
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
            if record.wms_estado != 'sin_enviar':
                continue
            estado_obj = record.picking_type_id.estado_disparo_wis or ''
            estados_aceptados = {estado_obj}
            if estado_obj == 'waiting':
                estados_aceptados.add('confirmed')
            # Requiere estado_obj configurado Y que el estado actual coincida.
            # Si estado_obj no está configurado, el disparo es en 'done' (manejado por _action_done).
            # Esto evita enviar pickings en 'draft' que todavía no están completamente formados.
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
                    _logger.info("[WIS] create | picking=%s enviado a WMS", record.name)
            except Exception as e:
                _logger.exception("[WIS] create | error en picking=%s: %s", record.name, e)
        return records

    def _create_backorder(self):
        backorders = super()._create_backorder()
        for backorder in backorders:
            original = backorder.backorder_id
            if not original:
                continue
            if not original.picking_type_id.integra_parciales and original.codigo_unico:
                backorder.with_context(skip_wms_integration=True).write({
                    'codigo_unico': original.codigo_unico,
                    'wms_estado': 'enviado',
                })
                _logger.info(
                    "[WIS] _create_backorder | backorder=%s hereda codigo_unico='%s' de original=%s",
                    backorder.name, original.codigo_unico, original.name,
                )
        return backorders

    def _enviar_wis_si_corresponde(self, hook_name):
        """Dispara integración WIS según estado actual del picking."""
        if self.env.context.get('skip_wms_integration'):
            return
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
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
            if record.wms_estado != 'sin_enviar':
                continue
            if record.codigo_unico:
                _logger.info(
                    "[WIS] %s | picking=%s ya tiene codigo_unico='%s', se omite reenvío a WMS.",
                    hook_name, record.name, record.codigo_unico,
                )
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
                self.env['wms.integracion.log'].create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'stock.picking',
                    'texto': f"Error al enviar picking {record.name} a WMS: {str(e)}",
                    'picking_id': record.id,
                    'resultado': 'error',
                    'detalle': f"Hook: {hook_name} | Estado: {record.state} | Tipo: {record.picking_type_id.name if record.picking_type_id else 'N/A'}",
                })

    def action_confirm(self):
        res = super().action_confirm()
        self._enviar_wis_si_corresponde('action_confirm')
        # Punto 4: cubre cross-docking generado por otros módulos (los onchange de vista
        # no corren en creación programática, así que forzamos el compute acá).
        self._wis_complete_document_type()
        return res

    def action_assign(self):
        res = super().action_assign()
        self._enviar_wis_si_corresponde('action_assign')
        self._wis_complete_document_type()
        return res

    def _action_done(self):
        res = super(StockPicking, self)._action_done()
        if self.env.context.get('skip_wms_integration'):
            return res
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
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

        if (not self.env.context.get('skip_wms_integration') and
                self.env['integracion_wis.integracion_wis']._comunicacion_habilitada() and
                'scheduled_date' in vals):
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


    # ------------------------------------------------------------------
    # Punto 3: bypass e-Remito según destino logístico.
    #
    # El campo `uses_cfe` en l10n_uy_einvoice_base es related a picking_type_id.uses_cfe
    # y no se puede sobrescribir a compute desde un _inherit. En su lugar:
    #
    #   1) Campo `wis_skip_eremito` (store, depende del flag de la location destino).
    #      Se usa en la vista para ocultar el botón "e-Remito" y en la lógica.
    #   2) Override del compute `_compute_l10n_latam_document_type` para no exigir
    #      tipo de documento.
    #   3) Override de `create_delivery_guide` para que el envío del CFE/eRemito
    #      no se ejecute cuando el flag está activo.
    # ------------------------------------------------------------------
    wis_skip_eremito = fields.Boolean(
        string="Salta validación e-Remito (WIS)",
        compute='_compute_wis_skip_eremito',
        store=True,
        help="Computed desde location_dest_id.wis_no_requiere_eremito. "
             "Si True: se oculta el botón 'e-Remito', no se exige Document Type y "
             "no se envía el CFE.",
    )

    @api.depends('location_dest_id.wis_no_requiere_eremito')
    def _compute_wis_skip_eremito(self):
        for picking in self:
            picking.wis_skip_eremito = bool(
                picking.location_dest_id and picking.location_dest_id.wis_no_requiere_eremito
            )

    def _wis_skip_eremito(self):
        """Mantiene la firma para legibilidad; delega en el campo store."""
        self.ensure_one()
        return bool(self.wis_skip_eremito)

    @api.depends('wis_skip_eremito')
    def _compute_l10n_latam_document_type(self):
        bypass = self.filtered('wis_skip_eremito')
        normales = self - bypass
        if normales:
            super(StockPicking, normales)._compute_l10n_latam_document_type()
        for picking in bypass:
            picking.l10n_latam_document_type_id = False

    def create_delivery_guide(self):
        """Salta el envío del e-Remito para pickings cuya location destino tiene
        wis_no_requiere_eremito=True. Los demás siguen el flujo normal de l10n_uy."""
        skip = self.filtered('wis_skip_eremito')
        normales = self - skip
        if skip:
            _logger.info(
                "[WIS] create_delivery_guide | omitiendo e-Remito para %s pickings "
                "con location_dest_id.wis_no_requiere_eremito=True: %s",
                len(skip), skip.mapped('name'),
            )
        if normales:
            return super(StockPicking, normales).create_delivery_guide()
        return True

    # ------------------------------------------------------------------
    # Punto 4: autocompletar l10n_latam_document_type_id en pickings programáticos
    # ------------------------------------------------------------------
    def _wis_complete_document_type(self):
        """Forzar recálculo de document_type cuando el picking se creó programáticamente.

        El compute `_compute_l10n_latam_document_type` en l10n_uy_einvoice_base depende de
        partner_id + punto_emision_id + picking_type_id, pero NO se dispara correctamente
        cuando el picking se crea desde un controller WIS (los onchange de vista no corren).

        Este helper:
        1. Asegura que el picking tenga partner_id (cae al partner de la company si falta).
        2. Invalida el cache del campo y fuerza la lectura para que el compute corra.
        """
        for picking in self:
            if not picking.picking_type_id.uses_cfe:
                continue
            if picking.l10n_latam_document_type_id:
                continue
            if picking.state not in ('draft', 'assigned', 'confirmed'):
                continue
            if not picking.partner_id:
                company = picking.picking_type_id.company_id or picking.company_id
                if company and company.partner_id:
                    picking.with_context(skip_wms_integration=True).write({
                        'partner_id': company.partner_id.id,
                    })
            try:
                picking.invalidate_recordset(['l10n_latam_document_type_id', 'punto_emision_id'])
                _ = picking.l10n_latam_document_type_id  # fuerza el read y dispara el compute
            except Exception as e:
                _logger.warning(
                    "[WIS] _wis_complete_document_type | picking=%s no pudo autocompletar: %s",
                    picking.name, e,
                )


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_confirm(self, merge=True, merge_into=False):
        res = super()._action_confirm(merge=merge, merge_into=merge_into)
        if self.env.context.get('skip_wms_integration'):
            return res
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            return res
        # Recopila pickings únicos afectados cuyo estado haya quedado en waiting/confirmed
        pickings_vistos = set()
        for move in res:
            picking = move.picking_id
            if not picking or picking.id in pickings_vistos:
                continue
            pickings_vistos.add(picking.id)
            if not picking.picking_type_id.integracion_wms:
                continue
            if not picking.move_ids:
                continue
            if picking.wms_estado != 'sin_enviar':
                continue
            estado_obj = picking.picking_type_id.estado_disparo_wis or ''
            if not estado_obj:
                continue
            estados_aceptados = {estado_obj}
            if estado_obj == 'waiting':
                estados_aceptados.add('confirmed')
            if picking.state not in estados_aceptados:
                continue
            tipo = picking.picking_type_id.tipo_pedido_wis or 'NORM'
            _logger.info("[WIS] _action_confirm | picking=%s | state=%s | enviando a WMS", picking.name, picking.state)
            try:
                response = picking.enviarWS(tipo)
                if response is not False:
                    wms_vals = {'wms_estado': 'enviado'}
                    if isinstance(response, dict):
                        wms_vals['idPedidoWMS'] = response.get('numeroInterfaz', '')
                        wms_vals['codigo_unico'] = response.get('codigoUnico', '')
                    picking.with_context(skip_wms_integration=True).write(wms_vals)
                    _logger.info("[WIS] _action_confirm | picking=%s enviado a WMS", picking.name)
            except Exception as e:
                _logger.exception("[WIS] _action_confirm | error en picking=%s: %s", picking.name, e)
        return res

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
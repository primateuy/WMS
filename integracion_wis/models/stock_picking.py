from odoo import models, fields, api
import requests
from odoo.exceptions import ValidationError, UserError
from markupsafe import Markup
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
            ("no_integrado", "No integrado con WMS"),
            ("en_proceso_cancelacion", "En proceso de cancelación"),
            ("error_cancelacion", "Error en cancelación"),
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

    wms_nro_preparacion = fields.Char(
        string="Nro. Preparación WMS",
        copy=False,
        readonly=True,
        help="Número de preparación que asigna WIS internamente. Necesario para anular "
             "pedidos de salida vía /Preparacion/AnularPickingPedidoPendiente. Se captura "
             "cuando WIS lo informa en el webhook de mercadería preparada.",
    )


    idPedidoWMS = fields.Char(
        string='ID Pedido WMS', help="Identificador del pedido en el sistema WMS.",
        copy=False)

    codigo_unico = fields.Char(
        string = "Código WMS identificatorio",
        copy=False,
        # copy=False: sin esto, al crear una devolución (que copia el picking original) el
        # retorno heredaba el codigo_unico de la operación original, y el guard
        # `if record.codigo_unico: continue` saltaba el envío -> nunca se generaba el W-D-.
    )

    wms_nro_caja = fields.Char(
        string="Nro. de Caja (FINT)",
        copy=False,
        help="Número de caja específico para pedidos de fin de temporada (FINT). "
             "Si se completa, se enviará al WMS como LPN con tipo FINTEMP.",
    )

    wms_lpns_enviados = fields.Boolean(
        string="LPN enviados a WMS",
        copy=False,
        help="Indica que los LPN (cajas) de esta devolución de caja cerrada ya se crearon en WIS "
             "(POST /Lpn/Create). Evita reenvíos duplicados; el botón 'Reenviar LPN a WIS' lo ignora.",
    )

    wis_enviar_lpns = fields.Boolean(
        related='picking_type_id.enviar_lpns_wis',
        string="Tipo envía LPN a WIS",
        help="Reflejo del flag del tipo de operación; usado para mostrar el botón de reenvío de LPN.",
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
            response = datosAPI.insertarDevolucion(self)
            # Caja cerrada fin de temporada: tras la referencia de devolución (OD),
            # crear los LPN de las cajas en WIS (POST /Lpn/Create) si el tipo lo habilita.
            if response is not False:
                self._wis_enviar_lpns_si_corresponde(datosAPI)
            return response

        if self.picking_type_id.code == 'incoming':
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

    def _wis_registrar_envio(self, codigo, tipo, ok=True, error=None):
        """Registra en `wms.integracion.log` el resultado del envío de ESTE picking a WMS.

        Centraliza el log de los 5 puntos de envío (create / _enviar_wis_si_corresponde /
        _action_done / write / _action_confirm) para que TODOS los pickings integrados queden
        registrados —no solo el que envía por el path `write`— cada uno con su `codigo_unico` y
        la orden de origen (`origin`), de modo que se puedan agrupar por compra/venta filtrando
        por la orden. Nunca rompe el flujo de negocio (envuelto en try/except)."""
        self.ensure_one()
        try:
            detalle = "Orden: %s · Tipo: %s · Estado: %s" % (
                self.origin or 's/orden', tipo or '-', self.state)
            if ok:
                texto = "Picking %s enviado a WMS. CodigoUnico: %s" % (self.name, codigo or '')
                nivel, resultado = 'info', 'exito'
            else:
                texto = "Error al enviar picking %s a WMS: %s" % (self.name, error)
                nivel, resultado = 'error', 'error'
            self.env['wms.integracion.log'].create({
                'fecha': fields.Datetime.now(),
                'nivel': nivel,
                'modelo': 'stock.picking',
                'texto': texto,
                'picking_id': self.id,
                'resultado': resultado,
                'detalle': detalle,
            })
        except Exception as e:
            _logger.exception(
                "[WIS] _wis_registrar_envio | no se pudo registrar log de %s: %s", self.name, e)

    def _wis_codigo_esperado(self, tipo):
        """Código único determinístico que Odoo asigna a este picking (W-D-/W-R-/W-P-<id>).

        El `codigo_unico` lo genera Odoo, NO WIS: los métodos `insertar*` de models.py lo arman
        localmente y WIS solo lo eco-devuelve en la respuesta. Esto permite persistir el código
        aunque la llamada a WIS falle (ej. WIS rechaza un tipoReferencia), para no dejar la
        operación sin código y romper trazabilidad/conciliación. Replica el ruteo de `enviarWS`:
        devolución -> W-D-, incoming -> W-R-, resto (pedidos) -> W-P-."""
        self.ensure_one()
        if self.codigo_unico:
            return self.codigo_unico
        TIPOS_DEVOLUCION = ('ODM', 'ODT', 'ODW', 'ODFT')
        if tipo in TIPOS_DEVOLUCION:
            return "W-D-%s" % self.id
        if self.picking_type_id.code == 'incoming':
            return "W-R-%s" % self.id
        return "W-P-%s" % self.id

    def _wis_enviar_lpns_si_corresponde(self, datosAPI=None, force=False):
        """Crea los LPN de las cajas en WIS (POST /Lpn/Create) para devoluciones de caja cerrada.

        Solo actúa si el tipo de operación tiene `enviar_lpns_wis` activo y el picking aún no
        envió sus LPN (`wms_lpns_enviados`), salvo `force=True` (reenvío manual). No tumba el
        flujo de negocio: ante error registra en el log y NO reintenta automáticamente (mismo
        criterio que `codigo_unico`). El detalle de las cajas se arma en `insertarLpns`."""
        self.ensure_one()
        if not self.picking_type_id.enviar_lpns_wis:
            return False
        if self.wms_lpns_enviados and not force:
            return False
        if datosAPI is None:
            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI:
            return False
        try:
            response = datosAPI.insertarLpns(self)
            if response is not False:
                self.with_context(skip_wms_integration=True).write({'wms_lpns_enviados': True})
                self._wis_registrar_envio(self.codigo_unico, 'LPN')
            return response
        except Exception as e:
            _logger.error("[WIS] LPN | error al crear LPN del picking %s: %s", self.name, e)
            self._wis_registrar_envio(self.codigo_unico, 'LPN', ok=False, error=str(e))
            return False

    def action_wis_reenviar_lpns(self):
        """Acción manual (botón): reenvía los LPN a WIS, recuperando el caso en que la
        referencia de devolución entró pero `/Lpn/Create` falló. Respeta el alcance del
        tipo (`enviar_lpns_wis`) pero ignora el guard `wms_lpns_enviados` (force=True)."""
        for picking in self:
            picking._wis_enviar_lpns_si_corresponde(force=True)
        return True

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
            if not record.picking_type_id.integracion_wms or record.picking_type_id.adquiere_codigo_unico_wms:
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
                        wms_vals['codigo_unico'] = response.get('codigoUnico') or record._wis_codigo_esperado(tipo)
                    else:
                        wms_vals['codigo_unico'] = record._wis_codigo_esperado(tipo)
                    record.with_context(skip_wms_integration=True).write(wms_vals)
                    _logger.info("[WIS] create | picking=%s enviado a WMS", record.name)
                    record._wis_registrar_envio(wms_vals.get('codigo_unico', ''), tipo)
            except Exception as e:
                _logger.exception("[WIS] create | error en picking=%s: %s", record.name, e)
                # El código es determinístico (lo genera Odoo, no WIS): se persiste aunque WIS
                # rechace, para no dejar la operación sin código. wms_estado queda 'sin_enviar'.
                cod_local = record._wis_codigo_esperado(tipo)
                record.with_context(skip_wms_integration=True).write({'codigo_unico': cod_local})
                record._wis_registrar_envio(cod_local, tipo, ok=False, error=str(e))
        # Adquisición DESDE LA CREACIÓN: si el picking que adquiere ya tiene su predecesor
        # con código al crearse, hereda acá mismo. Si el predecesor aún no tiene código
        # (ej. cadenas por procurement que se arman hacia atrás), los hooks de ciclo de vida
        # (confirm/assign/done) completan la adquisición cuando el upstream ya lo tenga.
        records._wis_adquirir_codigo_si_corresponde()
        return records

    def _create_backorder(self):
        backorders = super()._create_backorder()
        for backorder in backorders:
            original = backorder.backorder_id
            if not original:
                continue
            if not original.picking_type_id.integra_parciales and original.codigo_unico:
                # Si el original es una operación INTERNA (no_integrado, ej. crosspick), su
                # backorder debe seguir siendo no_integrado — sino la cascada
                # `_wis_validar_operaciones_internas` (que busca no_integrado) NO lo encuentra y el
                # paso salida->transito del saldo queda sin procesar. El resto (intermedio
                # preparado/enviado) hereda 'enviado' (saldo pendiente de preparar).
                backorder.with_context(skip_wms_integration=True).write({
                    'codigo_unico': original.codigo_unico,
                    'wms_estado': (
                        'no_integrado' if original.wms_estado == 'no_integrado' else 'enviado'),
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
            if not record.picking_type_id.integracion_wms or record.picking_type_id.adquiere_codigo_unico_wms:
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
                        wms_vals['codigo_unico'] = response.get('codigoUnico') or record._wis_codigo_esperado(tipo)
                    else:
                        wms_vals['codigo_unico'] = record._wis_codigo_esperado(tipo)
                    record.with_context(skip_wms_integration=True).write(wms_vals)
                    _logger.info("[WIS] %s | picking=%s enviado a WMS", hook_name, record.name)
                    record._wis_registrar_envio(wms_vals.get('codigo_unico', ''), tipo)
            except Exception as e:
                _logger.exception("[WIS] %s | error en picking=%s: %s", hook_name, record.name, e)
                # El código es determinístico (lo genera Odoo, no WIS): se persiste aunque WIS
                # rechace, para no dejar la operación sin código. wms_estado queda 'sin_enviar' y
                # el guard `if record.codigo_unico` de arriba evita reintentos en próximos assign.
                cod_local = record._wis_codigo_esperado(tipo)
                record.with_context(skip_wms_integration=True).write({'codigo_unico': cod_local})
                record._wis_registrar_envio(cod_local, tipo, ok=False, error=str(e))

    def _wis_adquirir_codigo_si_corresponde(self):
        """Para pickings cuyo tipo tiene `adquiere_codigo_unico_wms=True`: adquieren el
        `codigo_unico`/`idPedidoWMS` del picking PREDECESOR en la cadena de movimientos
        (`move_orig_ids`) y quedan `no_integrado` (operación interna, sin comunicarse con WIS).

        Genérico para cualquier flujo encadenado por rutas (venta mayorista, reabastecimiento,
        2-step delivery, etc.). El crossdock NO usa esto (sus moves son make_to_stock, sin
        `move_orig`): para crossdock la propagación la hace el módulo automatic_crossdocking
        con su cadena explícita.

        Se llama en los eventos de ciclo de vida (confirm/assign/done): reintenta hasta que el
        predecesor tenga su código (el downstream recién queda listo cuando el upstream se
        valida/envía, momento en que ya lo tiene). Solo adquiere si hay UN predecesor
        inequívoco con código (si hay 0 o varios distintos, no toca nada y loggea).
        """
        if self.env.context.get('skip_wms_integration'):
            return
        for record in self:
            if not record.picking_type_id.adquiere_codigo_unico_wms:
                continue
            if record.codigo_unico or record.state in ('done', 'cancel'):
                continue
            if not record.move_ids:
                continue
            preds = record.move_ids.move_orig_ids.picking_id.filtered(
                lambda p: p.id != record.id and p.codigo_unico
            )
            # Fallback crossdock: los moves del crosspick son make_to_stock (sin `move_orig`),
            # así que la cadena de movimientos no encuentra al predecesor. Se busca por
            # `group_id` + adyacencia de ubicación (el destino del predecesor == mi origen):
            # el intermedio termina en la Salida de la sucursal y el crosspick sale de ahí.
            # Robustece la propagación cuando el intermedio obtiene su código DESPUÉS del pase
            # único post-confirm de automatic_crossdocking (ej. su envío a WIS se difirió).
            if not preds and record.group_id and record.location_id:
                preds = self.search([
                    ('group_id', '=', record.group_id.id),
                    ('location_dest_id', '=', record.location_id.id),
                    ('codigo_unico', '!=', False),
                    ('id', '!=', record.id),
                    ('state', 'not in', ('cancel',)),
                ])
            codigos = set(preds.mapped('codigo_unico'))
            if len(codigos) != 1:
                if len(codigos) > 1:
                    _logger.warning(
                        "[WIS] adquirir | picking=%s: predecesores con códigos distintos %s; "
                        "no se adquiere (ambiguo).", record.name, codigos,
                    )
                continue
            origen = preds.sorted('id')[:1]
            record.with_context(skip_wms_integration=True).write({
                'codigo_unico': origen.codigo_unico,
                'idPedidoWMS': origen.idPedidoWMS,
                'wms_estado': 'no_integrado',
            })
            _logger.info(
                "[WIS] adquirir | picking=%s adquirió codigo_unico=%s de %s.",
                record.name, origen.codigo_unico, origen.name,
            )

    def action_confirm(self):
        res = super().action_confirm()
        self._enviar_wis_si_corresponde('action_confirm')
        self._wis_adquirir_codigo_si_corresponde()
        # Punto 4: cubre cross-docking generado por otros módulos (los onchange de vista
        # no corren en creación programática, así que forzamos el compute acá).
        self._wis_complete_document_type()
        return res

    def action_assign(self):
        res = super().action_assign()
        self._enviar_wis_si_corresponde('action_assign')
        self._wis_adquirir_codigo_si_corresponde()
        self._wis_complete_document_type()
        return res

    def action_cancel(self):
        """Cancelación Odoo → WIS (SALIENTE), ACOTADA A RECEPCIONES NORMALES (OC/RR).

        Antes de cancelar en Odoo, notifica la anulación de la referencia de recepción a WIS
        vía /AnulacionReferenciaRecepcion/Update (identifica por `codigo_unico`).

        ALCANCE (decisión 2026-06-16): SOLO recepciones normales. Quedan FUERA (cancelan con el
        flujo estándar de Odoo, sin tocar WIS): las salidas (pedidos) y las devoluciones de caja
        cerrada / fin de temporada (tipo de devolución o `enviar_lpns_wis`). Sus referencias se
        crean distinto (OD / LPN) y su anulación no está en alcance.

        Si WIS acepta -> wms_estado='anulado' y se cancela en Odoo. Si WIS rechaza -> se ABORTA la
        cancelación con UserError y el picking queda marcado 'error_cancelacion' con un log de
        error. Ese marcado + log se persisten en un cursor SEPARADO (`_wis_log_cancelacion_rechazada`)
        porque el UserError hace rollback de la transacción principal.

        NO interviene si el picking no integra, no tiene código, o ya está anulado/despachado —
        esto último cubre la anulación ENTRANTE (`_handle_pedidos_anulados`), que setea
        wms_estado='anulado' ANTES de llamar action_cancel(), evitando re-notificar a WIS.
        """
        if self.env.context.get('skip_wms_integration'):
            return super().action_cancel()
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            return super().action_cancel()

        TIPOS_DEVOLUCION = ('ODM', 'ODT', 'ODW', 'ODFT')
        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        for picking in self:
            if (not picking.picking_type_id.integracion_wms
                    or not picking.codigo_unico
                    or picking.wms_estado in ('sin_enviar', 'no_integrado')):
                continue  # flujo estándar de Odoo (no integrado)
            if picking.wms_estado in ('anulado', 'despachado'):
                continue  # ya anulado (ej. anulación entrante) o despachado -> dejar pasar

            # Alcance: SOLO recepciones normales. Salidas y devoluciones/caja cerrada cancelan
            # con el flujo estándar de Odoo, sin notificar a WIS.
            pt = picking.picking_type_id
            es_recepcion_normal = (
                pt.code == 'incoming'
                and (pt.tipo_pedido_wis or '') not in TIPOS_DEVOLUCION
                and not pt.enviar_lpns_wis
            )
            if not es_recepcion_normal:
                continue

            if not datosAPI:
                continue

            try:
                datosAPI.anularReferenciaRecepcion(picking)
                # WIS aceptó (sin excepción) -> marcar anulado y seguir con el flujo estándar.
                picking.with_context(skip_wms_integration=True).write({
                    'wms_estado': 'anulado',
                    'wms_origen': 'anulacion',
                    'wms_fecha_anulacion': fields.Datetime.now(),
                    'wms_motivo_anulacion': 'Cancelado desde Odoo',
                })
                _logger.info("[WIS] action_cancel | picking=%s anulado en WIS y cancelado en Odoo", picking.name)
            except Exception as e:
                # WIS rechazó la cancelación -> bloqueo operativo. La traza (estado + log) se
                # persiste en cursor separado porque el UserError de abajo hace rollback.
                _logger.warning("[WIS] action_cancel | WIS rechazó cancelación de %s: %s", picking.name, e)
                self._wis_log_cancelacion_rechazada(picking, e)
                raise UserError(
                    f"No es posible cancelar el picking {picking.name} porque WIS no aceptó la "
                    f"cancelación: {str(e)}\n\n"
                    f"El picking quedó marcado como 'Error en cancelación'. "
                    f"Contactar a Polo Oeste para resolver."
                )
        return super().action_cancel()

    def _wis_log_cancelacion_rechazada(self, picking, error):
        """Persiste la traza del rechazo de una cancelación en un cursor SEPARADO.

        El `action_cancel` lanza `UserError` cuando WIS rechaza, lo que hace rollback de la
        transacción principal y se llevaría puesto cualquier write/log hecho en ella. Para que el
        marcado `error_cancelacion` y el log de error SOBREVIVAN, se escriben en un cursor
        nuevo que se confirma al cerrarse (independiente del rollback de la principal)."""
        try:
            with self.env.registry.cursor() as new_cr:
                new_env = api.Environment(new_cr, self.env.uid, self.env.context)
                new_env['stock.picking'].browse(picking.id).with_context(
                    skip_wms_integration=True).write({'wms_estado': 'error_cancelacion'})
                new_env['wms.integracion.log'].create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'stock.picking',
                    'texto': f"action_cancel: WIS rechazó la cancelación de {picking.name}: {str(error)}",
                    'picking_id': picking.id,
                    'resultado': 'error',
                    'detalle': f"Tipo: {picking.picking_type_id.name} | codigo_unico: {picking.codigo_unico}",
                })
        except Exception as log_err:
            _logger.exception(
                "[WIS] _wis_log_cancelacion_rechazada | no se pudo persistir traza de %s: %s",
                picking.name, log_err)

    def _wis_emitir_eremito_si_corresponde(self):
        """Emite el e-Remito (CFE) ANTES de que el picking pase a 'done'. INDISPENSABLE: un
        picking que use CFE debe emitir el remito antes de completarse, en CUALQUIER flujo —
        incluida la validación de operaciones internas del crossdock
        (`_wis_validar_operaciones_internas`), que corre con skip_wms_integration y se saltaba
        la emisión que hacen los handlers WIS (confirmacionPedido / mercaderiaPreparada).

        Si la emisión falla -> UserError: el picking NO pasa a 'done' (decisión del usuario
        2026-06-09). Se acota a pickings del flujo WIS (con código o tipo integrado) para no
        alterar el flujo estándar de entregas no-WIS. Respeta cfe_emitido (no re-emite) y el
        bypass wis_skip_eremito."""
        for picking in self:
            if not picking.uses_cfe or picking.cfe_emitido or picking.wis_skip_eremito:
                continue
            if not (picking.codigo_unico or picking.picking_type_id.integracion_wms):
                continue  # ajeno a WIS -> sigue su flujo estándar de e-Remito
            # Guard: NUNCA emitir un e-Remito (CFE) con líneas en cantidad 0 — un CFE con
            # cantidad 0 es inválido. Si no hay nada que despachar o alguna línea quedó en 0,
            # se BLOQUEA el pase a 'done' (las cantidades vienen SIEMPRE en los contenedores de
            # WIS; un 0 es una anomalía a resolver). Ver [[wis-cfe-no-emitir-cantidad-cero]].
            lineas = picking.move_line_ids
            if not lineas or any(ml.qty_done <= 0 for ml in lineas):
                self._wis_registrar_falla_eremito(
                    picking,
                    "Hay líneas en cantidad 0 — un e-Remito (CFE) no puede emitirse con cantidad 0.")
                raise UserError(
                    f"No se puede completar el picking {picking.name}: el e-Remito (CFE) no "
                    f"puede emitirse porque hay líneas en cantidad 0. Verificar las cantidades "
                    f"preparadas (deberían venir en el contenedor de WIS) antes de validar."
                )
            try:
                picking.create_delivery_guide()  # respeta cfe_emitido + wis_skip_eremito
            except Exception as e:
                _logger.exception(
                    "[WIS] e-Remito | falla al emitir para picking=%s: %s", picking.name, e)
                self._wis_registrar_falla_eremito(picking, str(e))
                raise UserError(
                    f"No se puede completar el picking {picking.name}: falló la emisión del "
                    f"e-Remito (CFE), obligatoria antes de pasar a 'Hecho'.\n\n"
                    f"Detalle: {e}\n\nResolver la emisión del CFE y reintentar."
                )
            if not picking.cfe_emitido:
                self._wis_registrar_falla_eremito(
                    picking, "create_delivery_guide() no dejó cfe_emitido=True (sin excepción).")
                raise UserError(
                    f"No se puede completar el picking {picking.name}: el e-Remito (CFE) no "
                    f"quedó emitido y es obligatorio antes de pasar a 'Hecho'. "
                    f"Resolver la emisión del CFE y reintentar."
                )

    def _wis_registrar_falla_eremito(self, picking, detalle):
        """Deja constancia EN EL CHATTER del picking + wms.integracion.log de que NO se pudo
        emitir el e-Remito (CFE), para que al abrir el picking se vea el motivo por el que no
        pasó a 'Hecho'. Se registra en la misma transacción: en el flujo WIS (operaciones
        internas / webhooks) la excepción se captura aguas arriba y el commit conserva el
        mensaje; en validación manual el operador ve además el error en pantalla. Nunca rompe
        (envuelto en try/except)."""
        try:
            picking.sudo().message_post(
                body=Markup(
                    "<b>⚠️ e-Remito (CFE) NO emitido</b><br/>"
                    "El picking NO pasó a <b>Hecho</b> porque no se pudo emitir el e-Remito, "
                    "que es obligatorio.<br/>"
                    "Motivo: %s<br/>"
                    "Resolver la emisión del CFE y reintentar la validación."
                ) % (detalle or '—'),
                subtype_xmlid='mail.mt_note',
            )
            self.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': f"e-Remito NO emitido para {picking.name}: validación bloqueada.",
                'picking_id': picking.id,
                'resultado': 'error',
                'detalle': detalle,
            })
        except Exception as e:
            _logger.exception(
                "[WIS] no se pudo registrar la falla de e-Remito en %s: %s", picking.name, e)

    def _action_done(self):
        # e-Remito SIEMPRE antes de 'done' (indispensable): cubre las validaciones que corren
        # con skip_wms_integration (crossdock / operaciones internas) que se saltaban la emisión.
        # Va ANTES del super() y del check de skip para no dejar ningún camino sin emitir.
        self._wis_emitir_eremito_si_corresponde()
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
            if not record.picking_type_id.integracion_wms or record.picking_type_id.adquiere_codigo_unico_wms:
                continue
            if not record.move_ids:
                continue
            if record.wms_estado != 'sin_enviar':
                continue
            if record.codigo_unico:
                # Ya tiene código (ej. un envío previo falló pero el código quedó asignado): no
                # se reintenta, para respetar la política de "código persistido, sin reenvío".
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
                        wms_vals['codigo_unico'] = response.get('codigoUnico') or record._wis_codigo_esperado(tipo)
                    else:
                        wms_vals['codigo_unico'] = record._wis_codigo_esperado(tipo)
                    record.with_context(skip_wms_integration=True).write(wms_vals)
                    _logger.info("[WIS] _action_done | picking=%s enviado a WMS", record.name)
                    record._wis_registrar_envio(wms_vals.get('codigo_unico', ''), tipo)
            except Exception as e:
                _logger.exception("[WIS] _action_done | error en picking=%s: %s", record.name, e)
                # El código es determinístico (lo genera Odoo, no WIS): se persiste aunque WIS
                # rechace, para no dejar la operación sin código. wms_estado queda 'sin_enviar'.
                cod_local = record._wis_codigo_esperado(tipo)
                record.with_context(skip_wms_integration=True).write({'codigo_unico': cod_local})
                record._wis_registrar_envio(cod_local, tipo, ok=False, error=str(e))
        self._wis_adquirir_codigo_si_corresponde()
        return res

    def write(self, vals):
        res = super(StockPicking, self).write(vals)

        # Cuando un picking obtiene su `codigo_unico` (por envío a WIS o por adquisición),
        # propagar HACIA ADELANTE a sus sucesores flagged en la cadena de movimientos. Cubre
        # los flujos por procurement (ej. reabastecimiento) donde el sucesor ya está creado
        # —con su `move_orig`— pero todavía SIN código en el momento en que el predecesor recién
        # lo obtiene (el `create`-hook del sucesor corrió demasiado temprano). Se llama con
        # contexto limpio aunque este write venga con `skip_wms_integration` (el código se setea
        # con skip). Cascada automática: cada sucesor que adquiere reescribe su `codigo_unico`,
        # lo que vuelve a entrar acá y propaga al siguiente eslabón. Sin loop: si ya tiene código,
        # `_wis_adquirir_codigo_si_corresponde` no vuelve a tocarlo.
        if vals.get('codigo_unico'):
            sucesores = self.move_ids.move_dest_ids.picking_id.filtered(lambda p: p.id not in self.ids)
            # Fallback crossdock: el crosspick no está encadenado por moves (make_to_stock), así
            # que `move_dest_ids` no lo alcanza. Se lo busca por `group_id` + adyacencia de
            # ubicación (mi destino == su origen) entre los pickings que adquieren código y aún
            # no lo tienen. Idempotente: si ya tiene código, `_wis_adquirir_codigo_si_corresponde`
            # no lo vuelve a tocar. Cubre el caso en que el intermedio codea tarde (después del
            # pase único post-confirm), dejando al crosspick huérfano de código.
            for rec in self.filtered(lambda p: p.group_id and p.location_dest_id):
                sucesores |= self.search([
                    ('group_id', '=', rec.group_id.id),
                    ('location_id', '=', rec.location_dest_id.id),
                    ('picking_type_id.adquiere_codigo_unico_wms', '=', True),
                    ('codigo_unico', '=', False),
                    ('id', 'not in', self.ids),
                    ('state', 'not in', ('cancel', 'done')),
                ])
            if sucesores:
                sucesores.with_context(skip_wms_integration=False)._wis_adquirir_codigo_si_corresponde()

        # Al enviar a WIS (wms_estado -> 'enviado'), fijar wis_cantidad_original en los moves
        # UNA SOLA VEZ. Es la base para validar/auditar anulaciones parciales contra la demanda
        # original. Todos los puntos de envío escriben wms_estado='enviado', así que esto los cubre.
        if vals.get('wms_estado') == 'enviado':
            for record in self:
                for m in record.move_ids.filtered(
                        lambda mv: not mv.wis_cantidad_original and mv.product_uom_qty):
                    m.with_context(skip_wms_integration=True).write(
                        {'wis_cantidad_original': m.product_uom_qty})

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
                if not record.picking_type_id.integracion_wms or record.picking_type_id.adquiere_codigo_unico_wms:
                    continue
                if not record.move_ids:
                    continue
                if record.wms_estado != 'sin_enviar':
                    continue
                if record.codigo_unico:
                    # Ya tiene código (envío previo fallido): no se reintenta. Evita además que un
                    # cambio de estado posterior relance el error de WIS y bloquee la operación.
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
                        record._wis_registrar_envio(wms_vals.get('codigo_unico', ''), tipo)
                except Exception as e:
                    record._wis_registrar_envio('', tipo, ok=False, error=str(e))
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
        """Autocompletar `l10n_latam_document_type_id` (e-Remito) en pickings programáticos.

        Problema: el compute base `_compute_l10n_latam_document_type` (l10n_uy_einvoice_base)
        SOLO actúa sobre pickings en estado `draft` o `assigned` (filtro de línea 222). En el
        flujo de cross-docking, el picking que debe emitir el e-Remito (tipo con `uses_cfe=True`)
        queda en estado **`waiting`** (está esperando el movimiento aguas arriba de la cadena),
        por lo que el compute base lo ignora y el Document Type nunca se sugiere — aunque el
        picking YA tenga partner/dirección de entrega y punto de emisión correctos.

        Este helper replica la MISMA búsqueda del compute base y asigna el documento
        directamente, sin depender del estado del picking. A diferencia del compute base,
        NUNCA lanza UserError: si no encuentra exactamente una relación, deja el campo vacío
        y loguea un warning (no debe bloquear el flujo de stock ni la integración WIS).

        Cubre cualquier estado no final (todo salvo `done`/`cancel`), de modo que "cuando el
        stock.picking tenga que viajar a UCFE y se cree, quede con el tipo de documento
        siempre que corresponda".
        """
        latam_obj = self.env['l10n_latam.document.type']
        for picking in self:
            if not picking.picking_type_id.uses_cfe:
                continue
            # Respetar el bypass de e-Remito: si la location destino tiene
            # wis_no_requiere_eremito=True, NO se exige Document Type (consistente con
            # el override de _compute_l10n_latam_document_type, que lo deja en False).
            if picking.wis_skip_eremito:
                continue
            if picking.l10n_latam_document_type_id:
                continue
            if picking.state in ('done', 'cancel'):
                continue
            # 1. Asegurar partner_id (cae al partner de la company si falta).
            if not picking.partner_id:
                company = picking.picking_type_id.company_id or picking.company_id
                if company and company.partner_id:
                    picking.with_context(skip_wms_integration=True).write({
                        'partner_id': company.partner_id.id,
                    })
            # 2. Asegurar punto_emision_id (lo toma del tipo de operación si el compute
            #    store no lo dejó seteado, p.ej. por orden de precompute en creación).
            if not picking.punto_emision_id and picking.picking_type_id.dgi_sucursal_id:
                punto = picking.picking_type_id.punto_emision_id
                if punto:
                    picking.with_context(skip_wms_integration=True).write({
                        'punto_emision_id': punto.id,
                    })
            if not (picking.partner_id and picking.punto_emision_id):
                continue
            # 3. Replicar el search del compute base y asignar (sin lanzar nunca).
            dgi_indicador = '8' if picking.picking_type_id.code == 'incoming' else 'na'
            relacion = latam_obj.search([
                ('internal_type', '=', 'stock_picking'),
                ('company_id', '=', picking.company_id.id),
                ('punto_emision_id', '=', picking.punto_emision_id.id),
                ('dgi_indicador_facturacion', '=', dgi_indicador),
                ('electronic_document', '=', True),
                ('l10n_latam_identification_type_id', '=',
                 picking.partner_id.l10n_latam_identification_type_id.id),
            ])
            if len(relacion) == 1:
                picking.with_context(skip_wms_integration=True).write({
                    'l10n_latam_document_type_id': relacion.id,
                })
                _logger.info(
                    "[WIS] document_type | picking=%s (estado=%s) -> %s asignado.",
                    picking.name, picking.state, relacion.display_name,
                )
            else:
                _logger.warning(
                    "[WIS] document_type | picking=%s: %s relaciones para "
                    "punto_emision=%s ident=%s ind=%s; se deja vacío.",
                    picking.name, len(relacion), picking.punto_emision_id.id,
                    picking.partner_id.l10n_latam_identification_type_id.name, dgi_indicador,
                )


class StockMove(models.Model):
    _inherit = 'stock.move'

    # Anulaciones parciales de demanda (WIS): se reduce product_uom_qty por evento y se
    # acumula lo anulado, conservando la cantidad original para validación/trazabilidad.
    wis_cantidad_original = fields.Float(
        string="Cantidad original WIS",
        readonly=True, copy=False,
        help="Cantidad demandada al momento de enviar la operación a WIS. Se setea una "
             "sola vez (cuando el picking pasa a 'enviado') y es la base para validar y "
             "auditar las anulaciones parciales de demanda.")
    wis_cantidad_anulada = fields.Float(
        string="Cantidad anulada WIS",
        readonly=True, copy=False, default=0.0,
        help="Cantidad acumulada anulada por WIS sobre este movimiento (anulaciones "
             "parciales de demanda). Valor inicial 0; solo la integración lo modifica.")

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
            if not picking.picking_type_id.integracion_wms or picking.picking_type_id.adquiere_codigo_unico_wms:
                continue
            if not picking.move_ids:
                continue
            if picking.wms_estado != 'sin_enviar':
                continue
            if picking.codigo_unico:
                # Ya tiene código (envío previo fallido): no se reintenta.
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
                        wms_vals['codigo_unico'] = response.get('codigoUnico') or picking._wis_codigo_esperado(tipo)
                    else:
                        wms_vals['codigo_unico'] = picking._wis_codigo_esperado(tipo)
                    picking.with_context(skip_wms_integration=True).write(wms_vals)
                    _logger.info("[WIS] _action_confirm | picking=%s enviado a WMS", picking.name)
                    picking._wis_registrar_envio(wms_vals.get('codigo_unico', ''), tipo)
            except Exception as e:
                _logger.exception("[WIS] _action_confirm | error en picking=%s: %s", picking.name, e)
                # El código es determinístico (lo genera Odoo, no WIS): se persiste aunque WIS
                # rechace, para no dejar la operación sin código. wms_estado queda 'sin_enviar'.
                cod_local = picking._wis_codigo_esperado(tipo)
                picking.with_context(skip_wms_integration=True).write({'codigo_unico': cod_local})
                picking._wis_registrar_envio(cod_local, tipo, ok=False, error=str(e))
        # Tras confirmar (cadena de moves ya enlazada), intentar la adquisición en los pickings
        # flagged: si su predecesor ya tiene código en este punto, adquieren sin esperar a que se
        # los procese (ej. cadenas por procurement donde el upstream ya se codificó en el run).
        res.picking_id._wis_adquirir_codigo_si_corresponde()
        # Autocompletar el e-Remito: el crossdock confirma a nivel move y deja el picking que
        # viaja a UCFE en estado 'waiting', donde el compute base no asigna el Document Type.
        res.picking_id._wis_complete_document_type()
        return res

    def _action_assign(self, *args, **kwargs):
        res = super()._action_assign(*args, **kwargs)
        if not self.env.context.get('skip_wms_integration'):
            # ENVÍO WIS: la AUTO-asignación de un picking aguas abajo (cuando su predecesor se
            # valida) ocurre a nivel move, NO por `picking.action_assign()`. Si ese picking
            # integra y dispara en 'assigned' (ej. la 2da operación de una devolución de caja
            # cerrada: Transito->Existencias), recién acá llega a su estado de disparo, así que
            # se intenta el envío para que genere su codigo_unico. `_enviar_wis_si_corresponde`
            # tiene los guards (integra, sin_enviar, estado de disparo) -> no duplica.
            self.picking_id._enviar_wis_si_corresponde('move_action_assign')
            # La adquisición de código (pickings flagged que adquieren del predecesor).
            self.picking_id._wis_adquirir_codigo_si_corresponde()
            self.picking_id._wis_complete_document_type()
        return res

    def write(self, vals):
        res = super().write(vals)
        # skip_wms_integration corta la notificación a WIS: cubre los cambios de demanda
        # ORIGINADOS por WIS (ej. anulación parcial), que no deben re-notificarse a WIS.
        if 'product_uom_qty' in vals and not self.env.context.get('skip_wms_integration'):
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
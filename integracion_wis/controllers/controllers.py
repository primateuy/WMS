import json
import hmac
import hashlib
import base64
import logging
import time
from odoo import http, fields, SUPERUSER_ID
from odoo.http import request
from markupsafe import Markup
_logger = logging.getLogger(__name__)

class WebhookWIS(http.Controller):

    @http.route('/webhook/wis/callback', type='json', auth='public', methods=['POST'], csrf=False)
    def callback(self, **kwargs):
        signature = request.httprequest.headers.get('X-Hub-Signature')
        body = request.httprequest.get_data()
        
        # ===== LOG INMEDIATO AL LLEGAR =====
        try:
            body_str = body.decode('utf-8') if body else 'BODY VACÍO'
            headers_dict = dict(request.httprequest.headers)
            debug_msg = f"""WEBHOOK RECIBIDO:
IP: {request.httprequest.remote_addr}
Método: {request.httprequest.method}
Headers: {str(headers_dict)[:500]}
Body: {body_str[:500]}"""
            
            request.env['wis.webhook.log'].sudo().create({
                'fecha': fields.Date.today(),
                'hora': time.strftime('%H:%M:%S'),
                'request': debug_msg,
                'respuesta': 'Webhook recibido',
                'tipo': 'ENTRADA_DEBUG',
                'estado': 'exito'
            })
        except Exception as e:
            _logger.error(f"Error en log inicial: {str(e)}")

        # if not self._verify_signature(body, signature):
        #     return {'status': 401, 'detail': 'Firma inválida'}

        try:
            payload = json.loads(body)
        except Exception as e:
            response = {'status': 400, 'detail': 'JSON inválido'}
            self._create_log(body.decode('utf-8') if body else '', response, 'desconocido', 'error', 0)
            return response

        # Webhooks externos: WIS no autentica como usuario Odoo, por lo que el endpoint
        # corre con auth='public' → request.env.user es el Public User, sin permisos
        # sobre stock.* / purchase.* / loyalty.*. Escalamos a SUPERUSER para todo el
        # procesamiento del handler. Si el HMAC se valida (línea 40, hoy deshabilitada),
        # el rate-limit y la firma son el control de acceso real.
        request.update_env(user=SUPERUSER_ID)

        raw_id = payload.get('Id') or payload.get('id') or ''
        event_id = raw_id[0].lower() + raw_id[1:] if raw_id else ''
        numero_interfaz = payload.get('NumeroInterfazEjecucion') or payload.get('numeroInterfazEjecucion') or 0

        handlers = {
            'confirmacionRecepcion':           self._handle_confirmacion_recepcion,
            'confirmacionPedido':              self._handle_confirmacion_pedido,
            'confirmacionMercaderiaPreparada': self._handle_mercaderia_preparada,
            'pedidosAnulados':                 self._handle_pedidos_anulados,
            'ajustes':                         self._handle_ajustes,
            'consultaStock':                   self._handle_consulta_stock,
            'almacenamiento':                  self._handle_almacenamiento,
            'confirmacionProduccion':          self._handle_confirmacion_produccion,
            'test':                            self._handle_test,
        }

        handler = handlers.get(event_id)
        if not handler:
            _logger.warning("Tipo de evento WIS no manejado: %s", event_id)
            response = {'status': 400, 'detail': f'Evento no soportado: {event_id}'}
            self._create_log(payload, response, event_id or 'desconocido', 'error', numero_interfaz)
            return response

        try:
            pascal_id = event_id[0].upper() + event_id[1:] if event_id else ''
            raw_data = payload.get(event_id) or payload.get(pascal_id) or ({} if event_id else payload)
            handler_data = self._normalize_keys(raw_data)
            handler(handler_data)
            response = {'status': 200}
            self._create_log(payload, response, event_id, 'exito', numero_interfaz)
            return response
        except Exception as e:
            _logger.exception("Error procesando evento WIS %s", event_id)
            response = {'status': 500, 'detail': str(e)}
            self._create_log(payload, response, event_id, 'error', numero_interfaz)
            return response

    def _normalize_keys(self, obj):
        """Normaliza recursivamente claves PascalCase a camelCase en dicts/listas."""
        if isinstance(obj, dict):
            return {
                (k[0].lower() + k[1:] if k else k): self._normalize_keys(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._normalize_keys(i) for i in obj]
        return obj

    def _verify_signature(self, body, received_signature):
        if not received_signature:
            _logger.warning("[WIS FIRMA] Header X-Hub-Signature ausente o vacío")
            return False

        secret = request.env['ir.config_parameter'].sudo().get_param('wis.webhook_secret', '')
        if not secret:
            _logger.warning("[WIS FIRMA] Parámetro wis.webhook_secret no configurado en Odoo")
            return False

        # WIS firma con HMAC-SHA512 y envía el resultado codificado en Base64
        computed = hmac.new(secret.encode('utf-8'), body, hashlib.sha512).digest()
        computed_b64 = base64.b64encode(computed).decode('utf-8')
        _logger.info("[WIS FIRMA] Body recibido (primeros 200 bytes): %s", body[:200])
        _logger.info("[WIS FIRMA] Firma esperada (Base64): %s", computed_b64)
        _logger.info("[WIS FIRMA] Firma recibida (header): %s", received_signature)
        try:
            signature_bytes = base64.b64decode(received_signature)
        except Exception:
            _logger.warning("X-Hub-Signature no es Base64 válido")
            return False

        return hmac.compare_digest(computed, signature_bytes)

    def _create_log(self, data, respuesta, tipo, estado='exito', numero_interfaz=0):
        try:
            if isinstance(data, str):
                request_str = data
            else:
                request_str = json.dumps(data, indent=4, ensure_ascii=False)
            if isinstance(respuesta, dict):
                respuesta_str = json.dumps(respuesta, indent=4, ensure_ascii=False)
            else:
                respuesta_str = json.dumps({'message': str(respuesta)}, indent=4, ensure_ascii=False)

            request.env['wis.webhook.log'].sudo().create({
                'fecha': fields.Date.today(),
                'hora': time.strftime('%H:%M:%S'),
                'numero_interfaz_ejecucion': numero_interfaz or 0,
                'request': request_str,
                'respuesta': respuesta_str,
                'tipo': tipo,
                'estado': estado
            })
        except Exception as e:
            _logger.exception("Error guardando log de webhook WIS: %s", str(e))

    def _handle_confirmacion_recepcion(self, data):
        """Spec sección 1: valida picking de recepción con cantidades reales de WIS.

        Mapeo principal por IdLineaSistemaExterno = 'odoo__stock.move__ID'.
        Fallback por codigo_unico del producto (orden ascendente de ID si hay múltiples
        moves del mismo producto). Se ignoran líneas con Identificador='(AUTO)' que son
        marcadores de lote automático sin cantidad real.
        """

        from datetime import datetime

        referencias = data.get('referencias', [])
        if not referencias:
            raise ValueError("El payload no contiene referencias.")

        fecha_ingreso_raw = data.get('fechaIngreso', '') or data.get('fechaCierre', '')
        fecha_ingreso = fields.Datetime.now()
        if fecha_ingreso_raw:
            for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y'):
                try:
                    fecha_ingreso = datetime.strptime(fecha_ingreso_raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                _logger.warning("No se pudo parsear fechaIngreso '%s', usando now()", fecha_ingreso_raw)

        errores = []

        for ref in referencias:
            numero_referencia = ref.get('numeroReferencia')
            tipo_referencia   = ref.get('tipoReferencia', '')
            memo              = ref.get('memo', '')
            detalles          = ref.get('detalles', [])

            detalles_raiz = data.get('detalles', [])
            mapa_raiz = {}
            for d in detalles_raiz:
                cod = d.get('producto', '')
                cant = d.get('cantidadRecibida', 0)
                if cod:
                    mapa_raiz[cod] = cant

            if not numero_referencia:
                errores.append("Una referencia no tiene 'numeroReferencia'.")
                continue

            if not detalles:
                errores.append(
                    f"Referencia '{numero_referencia}' no tiene detalles de productos."
                )
                continue

            pickings_todos = request.env['stock.picking'].sudo().search(
                [('codigo_unico', '=', numero_referencia)]
            )
            pickings_activos = pickings_todos.filtered(
                lambda p: p.wms_estado != 'sin_enviar' and p.state not in ('done', 'cancel')
            )

            # Fallback por name (spec 1.4 paso 1): si no se encontró por codigo_unico,
            # se busca por name para soportar referencias enviadas con el nombre del picking.
            if len(pickings_activos) == 0:
                pickings_por_name = request.env['stock.picking'].sudo().search(
                    [('name', '=', numero_referencia)]
                )
                pickings_activos = pickings_por_name.filtered(
                    lambda p: p.state not in ('done', 'cancel')
                )

            if len(pickings_activos) == 0:
                _logger.warning(
                    "[WIS] confirmacionRecepcion | Sin pickings activos con codigo_unico/name='%s'. "
                    "Pickings totales encontrados: %s",
                    numero_referencia,
                    pickings_todos.mapped('name'),
                )
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'warning',
                    'modelo': 'stock.picking',
                    'texto': f"confirmacionRecepcion: sin pickings activos para referencia '{numero_referencia}'.",
                    'picking_id': False,
                    'resultado': 'error',
                    'detalle': f"Pickings totales: {pickings_todos.mapped('name')}",
                })
                errores.append(
                    f"Sin pickings activos para referencia '{numero_referencia}' (tipo: {tipo_referencia})."
                )
                continue

            if len(pickings_activos) > 1:
                nombres = ', '.join(pickings_activos.mapped('name'))
                _logger.error(
                    "[WIS] confirmacionRecepcion | Múltiples pickings activos con codigo_unico='%s': %s",
                    numero_referencia, nombres,
                )
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"confirmacionRecepcion: múltiples pickings activos para '{numero_referencia}'. "
                        f"Intervención manual requerida."
                    ),
                    'picking_id': False,
                    'resultado': 'error',
                    'detalle': f"Pickings en conflicto: {nombres}",
                })
                errores.append(
                    f"Múltiples pickings activos para '{numero_referencia}': {nombres}. "
                    f"Intervención manual requerida."
                )
                continue

            picking = pickings_activos



            try:
                mapa_cantidades = {}
                mapa_por_codigo = {}
                for detalle in detalles:
                    id_linea       = detalle.get('idLineaSistemaExterno', '')
                    cod_producto   = detalle.get('producto', '')
                    cant_consumida = detalle.get('cantidadConsumida', 0)
                    identificador  = (detalle.get('identificador') or '').strip()

                    # Spec 1.3: ignorar líneas '(AUTO)' — son marcadores de lote sin
                    # cantidad real. Cualquier línea con CantidadConsumida<=0 también
                    # se omite (la cantidad real viene en otra línea del mismo move).
                    if identificador == '(AUTO)' or cant_consumida <= 0:
                        continue

                    # Spec 1.4: extraer ID del move desde idLineaSistemaExterno
                    # con split por '__' tomando el último elemento.
                    move_id = None
                    if id_linea:
                        ultimo = id_linea.split('__')[-1]
                        try:
                            move_id = int(ultimo)
                        except (ValueError, TypeError):
                            move_id = None

                    if move_id:
                        mapa_cantidades[move_id] = mapa_cantidades.get(move_id, 0) + cant_consumida
                    if cod_producto:
                        # Por producto se acumulan cantidades para soportar múltiples
                        # líneas WIS del mismo producto sobre un mismo move.
                        mapa_por_codigo[cod_producto] = mapa_por_codigo.get(cod_producto, 0) + cant_consumida

                es_parcial = False

                # Spec 1.4: si hay más de un move con el mismo producto, procesar en
                # orden ascendente de ID para que el mapeo por producto consuma primero
                # los moves más antiguos.
                moves_sorted = picking.move_ids.sorted('id')
                cantidades_por_cod_restante = dict(mapa_por_codigo)

                for move in moves_sorted:
                    cant_recibida = mapa_cantidades.get(move.id)

                    if cant_recibida is None:
                        # Fallback por codigo_unico del producto
                        for cod_buscar in [
                            move.product_id.codigo_unico,
                            move.product_id.default_code,
                            f"PRD-{move.product_id.id}",
                        ]:
                            if cod_buscar and cantidades_por_cod_restante.get(cod_buscar, 0) > 0:
                                # Consume hasta lo que pide el move o lo disponible
                                disponible = cantidades_por_cod_restante[cod_buscar]
                                cant_recibida = min(disponible, move.product_uom_qty)
                                cantidades_por_cod_restante[cod_buscar] = disponible - cant_recibida
                                break
                            if cod_buscar and cod_buscar in mapa_raiz:
                                cant_recibida = mapa_raiz[cod_buscar]
                                break

                    if cant_recibida is None:
                        _logger.warning(
                            "Producto '%s' (id=%s, codigo_unico=%s, default_code=%s) "
                            "no encontrado en detalles WMS para picking %s. qty_done=0.",
                            move.product_id.name,
                            move.product_id.id,
                            move.product_id.codigo_unico,
                            move.product_id.default_code,
                            picking.name,
                        )
                        cant_recibida = 0
                        es_parcial = True

                    if cant_recibida < move.product_uom_qty:
                        es_parcial = True
                        _logger.info(
                            "Recepción parcial - Producto %s: solicitado=%s recibido=%s",
                            move.product_id.name, move.product_uom_qty, cant_recibida,
                        )

                    cant_restante = cant_recibida
                    for move_line in move.move_line_ids:
                        capacidad_linea = move.product_uom_qty
                        if cant_restante <= 0:
                            move_line.sudo().quantity = 0
                        elif cant_restante >= capacidad_linea:
                            move_line.sudo().quantity = capacidad_linea
                            cant_restante -= capacidad_linea
                        else:
                            move_line.sudo().quantity = cant_restante
                            cant_restante = 0



                # Punto 2: marcar origen y campos WIS (location/panel si vienen en payload).
                picking_wis_vals = {'wms_origen': 'recepcion'}
                wis_loc = data.get('locationID') or data.get('LocationID') or data.get('location_id')
                wis_panel = data.get('panelID') or data.get('PanelID') or data.get('panel_id')
                if wis_loc:
                    picking_wis_vals['wis_location_id'] = str(wis_loc)
                if wis_panel:
                    picking_wis_vals['wis_panel_id'] = str(wis_panel)
                picking.with_context(skip_wms_integration=True).sudo().write(picking_wis_vals)

                # Punto 4: autocompletar document_type para que la validación no falle por CFE vacío.
                picking.sudo()._wis_complete_document_type()

                if picking.state == 'assigned':
                    res = picking.with_context(
                        skip_wms_integration=True,
                    ).sudo().button_validate()

                    if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                        backorder_wiz = (
                            request.env['stock.backorder.confirmation']
                            .with_context(**res.get('context', {}))
                            .sudo()
                            .create({})
                        )
                        if es_parcial:
                            backorder_wiz.process()
                            _logger.info(
                                "Backorder creado para picking %s con remanente.",
                                picking.name
                            )
                        else:
                            backorder_wiz.process_cancel_backorder()

                lineas_html = ""
                for move in picking.move_ids:
                    cant_recibida = mapa_cantidades.get(move.id)
                    if cant_recibida is None:
                        for cod_buscar in [
                            move.product_id.codigo_unico,
                            move.product_id.default_code,
                            f"PRD-{move.product_id.id}", 
                            
                            ]:
                            if cod_buscar and cod_buscar in mapa_por_codigo:
                                cant_recibida = mapa_por_codigo[cod_buscar]
                                break
                            if cod_buscar and cod_buscar in mapa_raiz:
                                cant_recibida = mapa_raiz[cod_buscar]
                                break

                    if cant_recibida is None:
                        _logger.warning(
                            "Producto '%s' (id=%s, codigo_unico=%s, default_code=%s) "
                            "no encontrado en detalles WMS para picking %s. qty_done=0.",
                            move.product_id.name,
                            move.product_id.id,
                            move.product_id.codigo_unico,
                            move.product_id.default_code,
                            picking.name,
                        )
                        cant_recibida = 0
                        es_parcial = True

                    solicitado = move.product_uom_qty
                    lineas_html += (
                        f"<tr>"
                        f"<td style='padding:4px 8px;'>{move.product_id.display_name}</td>"
                        f"<td style='padding:4px 8px; text-align:left;'>{solicitado} {move.product_uom.name}</td>"
                        f"<td style='padding:4px 8px; text-align:left;'>{cant_recibida} {move.product_uom.name}</td>"
                        f"</tr>"
                    )

                picking.sudo().message_post(
                    body=Markup(
                        f"<b>{'Recepción parcial' if es_parcial else 'Recepción total'} "
                        f"confirmada por WMS</b><br/>"
                        f"Referencia WMS: <b>{numero_referencia}</b> (tipo: {tipo_referencia})<br/>"
                        f"Fecha ingreso: {fecha_ingreso}<br/>"
                        f"{'<b>Se generó backorder con el remanente.</b><br/>' if es_parcial else ''}"
                        f"<br/>"
                        f"<table style='border-collapse:collapse; width:100%;'>"
                        f"<thead><tr style='background:#f0f0f0;'>"
                        f"<th style='text-align:left; padding:4px 8px;'>Producto</th>"
                        f"<th style='text-align:left; padding:4px 8px;'>Solicitado</th>"
                        f"<th style='text-align:left; padding:4px 8px;'>Recibido</th>"
                        f"</tr></thead>"
                        f"<tbody>{lineas_html}</tbody>"
                        f"</table>"
                    ),
                    subtype_xmlid='mail.mt_note',
                )

                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'info',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"confirmacionRecepcion: picking {picking.name} validado "
                        f"({'parcial con backorder' if es_parcial else 'total'})."
                    ),
                    'picking_id': picking.id,
                    'resultado': 'exito',
                    'detalle': (
                        f"Ref WMS: {numero_referencia} | "
                        f"Tipo: {tipo_referencia} | "
                        f"Fecha: {fecha_ingreso}"
                    ),
                    'payload_webhook': json.dumps(ref, ensure_ascii=False, indent=2),
                })

            except Exception as e:
                _logger.exception(
                    "Error procesando recepción '%s': %s", numero_referencia, e
                )
                errores.append(f"Error en referencia '{numero_referencia}': {str(e)}")

        if errores:
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': (
                    f"confirmacionRecepcion: {len(errores)} error(s) "
                    f"de {len(referencias)} referencia(s)."
                ),
                'picking_id': False,
                'resultado': 'error',
                'detalle': ' | '.join(errores),
            })
            raise ValueError(
                f"Se procesaron {len(referencias) - len(errores)}/{len(referencias)} referencias. "
                f"Errores: {' | '.join(errores)}"
            )

    def _handle_confirmacion_pedido(self, data):
        """Spec sección 2: WIS despachó físicamente un pedido.

        Antes de validar el picking se intenta emitir el eRemito (si uses_cfe=True
        y cfe_emitido=False). Las fallas de emisión NO bloquean el despacho — sólo
        se loggean para intervención manual posterior.

        Paquetes (spec 2.2 y 2.5): se crea un stock.quant.package por contenedor
        usando CodigoBarras como name (fallback a NumeroContenedor). Se guarda
        IdExternoContenedor en wis_id_externo para trazabilidad. Las cantidades por
        producto dentro de un contenedor se SUMAN sin superar la demanda del move.
        """
        from datetime import datetime

        pedidos = data.get('pedidos', [])
        if not pedidos:
            raise ValueError("El payload no contiene pedidos.")

        # Cabecera (spec 2.2)
        matricula          = data.get('matricula', '')
        descripcion_camion = data.get('descripcionCamion', '')
        transportadora     = data.get('transportadora', '')
        fecha_cierre_raw   = data.get('fechaCierre', '') or data.get('fechaFacturacion', '')

        fecha_despacho = fields.Datetime.now()
        if fecha_cierre_raw:
            try:
                fecha_despacho = datetime.strptime(fecha_cierre_raw, '%d/%m/%Y %H:%M')
            except ValueError:
                _logger.warning(
                    "No se pudo parsear fechaCierre '%s', usando now()", fecha_cierre_raw
                )

        contenedores = data.get('contenedores', [])
        # .get(k, default) NO usa el default cuando el valor es None. WIS manda null en
        # cantidadBultos/pesoReal cuando no hay dato, así que coercemos con `or` para evitar
        # TypeError "unsupported operand type(s) for +: 'int' and 'NoneType'".
        total_bultos = sum((c.get('cantidadBultos') or 0)   for c in contenedores)
        peso_total   = sum((c.get('pesoReal')       or 0.0) for c in contenedores)
        precintos    = ', '.join(filter(None,
            [c.get('precinto1', '') for c in contenedores] +
            [c.get('precinto2', '') for c in contenedores]
        ))

        errores = []

        for pedido_data in pedidos:
            nombre_pedido = pedido_data.get('pedido')
            codigo_origen = pedido_data.get('codigoOrigen', '')
            memo          = pedido_data.get('memo', '')

            if not nombre_pedido:
                errores.append("Un pedido del array no tiene campo 'pedido'.")
                continue

            # Spec 2.4 paso 1: buscar por codigo_unico, luego name, luego idPedidoWMS.
            # Acepta pickings en state='done' SOLO si su wms_estado es 'preparado'
            # (= confirmacionMercaderiaPreparada los validó). Si están en done con
            # otro wms_estado (ej. 'despachado', 'enviado'), se bloquea con error
            # claro — no se permite re-confirmar.
            pickings_todos = request.env['stock.picking'].sudo().search(
                [('codigo_unico', '=', nombre_pedido)]
            )
            pickings_validos = pickings_todos.filtered(
                lambda p: p.wms_estado != 'sin_enviar' and p.state != 'cancel'
            )
            pickings_activos = pickings_validos.filtered(
                lambda p: p.state != 'done'
            )
            pickings_done_preparado = pickings_validos.filtered(
                lambda p: p.state == 'done' and p.wms_estado == 'preparado'
            )
            pickings_done_otro = pickings_validos.filtered(
                lambda p: p.state == 'done' and p.wms_estado != 'preparado'
            )
            picking = False
            # Prioridad 1: un único picking activo (assigned/confirmed/waiting).
            if len(pickings_activos) == 1:
                picking = pickings_activos
            # Prioridad 2: sin activos + 1 picking en done con wms_estado='preparado'
            # (la preparada lo validó, ahora solo se aplican metadatos de transporte).
            elif not pickings_activos and len(pickings_done_preparado) == 1:
                picking = pickings_done_preparado
            # Bloqueo: el picking está en done pero NO con wms_estado='preparado'
            # (probablemente 'despachado' o 'enviado'). No permitir re-confirmación.
            elif not pickings_activos and not pickings_done_preparado and pickings_done_otro:
                nombres = ', '.join(pickings_done_otro.mapped('name'))
                estados = ', '.join(sorted(set(pickings_done_otro.mapped('wms_estado'))))
                _logger.warning(
                    "[WIS] confirmacionPedido | picking(s) con codigo_unico='%s' "
                    "están en done pero wms_estado no es 'preparado' (actual: %s): %s",
                    nombre_pedido, estados, nombres,
                )
                errores.append(
                    f"Picking(s) con codigo_unico='{nombre_pedido}' están en "
                    f"done pero su wms_estado no es 'preparado' (actual: {estados}): "
                    f"{nombres}. No se permite confirmar."
                )
                continue
            elif len(pickings_activos) > 1 or len(pickings_done_preparado) > 1:
                conflicto = pickings_activos | pickings_done_preparado
                nombres = ', '.join(conflicto.mapped('name'))
                _logger.error(
                    "[WIS] confirmacionPedido | Múltiples pickings con codigo_unico='%s': %s",
                    nombre_pedido, nombres,
                )
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"confirmacionPedido: múltiples pickings para '{nombre_pedido}'. "
                        f"Intervención manual requerida."
                    ),
                    'picking_id': False,
                    'resultado': 'error',
                    'detalle': f"Pickings en conflicto: {nombres}",
                })
                errores.append(
                    f"Múltiples pickings para '{nombre_pedido}': {nombres}. "
                    f"Intervención manual requerida."
                )
                continue

            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('name', '=', nombre_pedido)], limit=1
                )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('idPedidoWMS', '=', nombre_pedido)], limit=1
                )
            if not picking:
                errores.append(f"No se encontró picking para pedido '{nombre_pedido}'.")
                continue

            if picking.state == 'cancel':
                errores.append(f"Picking '{picking.name}' está cancelado, no se puede despachar.")
                continue

            # Si el picking ya está en 'done' (probablemente porque
            # confirmacionMercaderiaPreparada lo validó antes), permitimos que
            # confirmacionPedido aplique igualmente los metadatos de transporte.
            # El bloque de button_validate más abajo está condicionado a
            # state == 'assigned', así que no se intenta re-validar.
            if picking.state == 'done':
                _logger.info(
                    "[WIS] confirmacionPedido | picking %s ya está en done "
                    "(validado por confirmacionMercaderiaPreparada). Aplicando solo "
                    "metadatos de transporte.",
                    picking.name,
                )

            if picking.wms_estado in ('despachado', 'anulado'):
                errores.append(
                    f"Picking '{picking.name}' ya tiene wms_estado '{picking.wms_estado}', se omite."
                )
                continue

            try:
                # Spec 2.3: emisión de eRemito ANTES de validar.
                # Si el picking maneja CFE y aún no fue emitido, se intenta emitir aquí.
                # Una falla NO bloquea el despacho — se loggea para intervención manual.
                self._intentar_emitir_eremito(picking, origen='confirmacionPedido')

                # Spec 2.4 paso 2: registrar datos de transporte
                picking.with_context(skip_wms_integration=True).write({
                    'wms_estado':            'despachado',
                    'wms_origen':            'despacho',
                    'wms_fecha_despacho':    fecha_despacho,
                    'wms_transportadora':    str(transportadora) if transportadora else matricula,
                    'wms_descripcion_camion': descripcion_camion,
                    'wms_nro_remito':        codigo_origen,
                    'wms_cantidad_bultos':   total_bultos,
                    'wms_peso_total':        peso_total,
                    'wms_observaciones_preparacion': (
                        f"Despacho — Camión: {matricula} | "
                        f"Precintos: {precintos or '—'} | Memo: {memo or '—'}"
                    ),
                })

                # Punto 4: autocompletar document_type antes de validar (si aplica CFE).
                picking.sudo()._wis_complete_document_type()

                # Spec 2.4 paso 3 + 2.5: crear paquetes con mapa de cantidades por producto.
                paquetes_creados = self._crear_paquetes_desde_contenedores(
                    picking, contenedores
                )

                # Asignar cantidad por defecto a las move_lines sin qty_done
                for move_line in picking.move_line_ids:
                    if move_line.qty_done == 0:
                        move_line.sudo().qty_done = (
                            move_line.quantity_product_uom or move_line.qty_done or move_line.move_id.product_uom_qty
                        )

                # Spec 2.4 paso 4: validar (sin backorder; despachos no generan backorder)
                if picking.state == 'assigned':
                    res = picking.with_context(
                        skip_wms_integration=True,
                        skip_backorder=True,
                    ).button_validate()

                    if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                        backorder_wiz = (
                            request.env['stock.backorder.confirmation']
                            .with_context(**res.get('context', {}))
                            .sudo()
                            .create({})
                        )
                        backorder_wiz.process_cancel_backorder()

                _logger.info(
                    "Picking %s despachado y validado por WMS. "
                    "Paquetes: %s | Bultos: %s | Peso: %s kg",
                    picking.name,
                    len(paquetes_creados),
                    total_bultos,
                    peso_total,
                )

                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'info',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"Webhook confirmacionPedido: picking {picking.name} "
                        f"despachado y validado. Paquetes: {len(paquetes_creados)}, "
                        f"Bultos: {total_bultos}, Peso: {peso_total} kg"
                    ),
                    'picking_id': picking.id,
                    'resultado': 'exito',
                    'detalle': (
                        f"Transportadora: {transportadora or matricula} | "
                        f"Precintos: {precintos or '—'} | "
                        f"Fecha despacho: {fecha_despacho} | "
                        f"Camión: {descripcion_camion or '—'}"
                    ),
                    'payload_webhook': json.dumps({
                        'pedido': pedido_data,
                        'contenedores': contenedores,
                    }, ensure_ascii=False, indent=2),
                })

            except Exception as e:
                _logger.exception(
                    "Error procesando despacho para picking '%s': %s", nombre_pedido, e
                )
                errores.append(f"Error en picking '{nombre_pedido}': {str(e)}")

        if errores:
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': (
                    f"confirmacionPedido: {len(errores)} error(s) "
                    f"de {len(pedidos)} pedido(s)."
                ),
                'picking_id': False,
                'resultado': 'error',
                'detalle': ' | '.join(errores),
            })
            raise ValueError(
                f"Se procesaron {len(pedidos) - len(errores)}/{len(pedidos)} pedidos. "
                f"Errores: {' | '.join(errores)}"
            )

    def _intentar_emitir_eremito(self, picking, origen=''):
        """Intenta emitir el eRemito de un picking que use CFE.

        Sólo actúa si picking.uses_cfe=True y picking.cfe_emitido=False. Una falla
        no se propaga: se loggea como advertencia para intervención manual.
        """
        if not hasattr(picking, 'uses_cfe') or not picking.uses_cfe:
            return False
        if getattr(picking, 'cfe_emitido', False):
            return False
        # Punto 3: si la location destino está marcada con wis_no_requiere_eremito,
        # no se envía CFE para este movimiento (movimiento interno sin traslado externo).
        if getattr(picking, 'wis_skip_eremito', False):
            _logger.info(
                "[WIS] %s | omito eRemito de picking=%s por location_dest_id.wis_no_requiere_eremito",
                origen or 'eRemito', picking.name,
            )
            return False
        try:
            res = picking._delivery_guide() if hasattr(picking, '_delivery_guide') else False
            if res:
                picking.with_context(skip_wms_integration=True).write({'cfe_emitido': True})
            _logger.info(
                "[WIS] %s | eRemito emitido para picking=%s",
                origen or 'eRemito', picking.name,
            )
            return True
        except Exception as e:
            _logger.exception(
                "[WIS] %s | error emitiendo eRemito para picking=%s",
                origen or 'eRemito', picking.name,
            )
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'warning',
                'modelo': 'stock.picking',
                'texto': (
                    f"{origen or 'eRemito'}: falla al emitir eRemito para picking "
                    f"{picking.name}. El despacho continúa; el operador debe emitirlo manualmente."
                ),
                'picking_id': picking.id,
                'resultado': 'error',
                'detalle': str(e),
            })
            return False

    def _crear_paquetes_desde_contenedores(self, picking, contenedores):
        """Crea stock.quant.package por contenedor y asigna move_lines según
        el mapa de cantidades por producto (spec 2.4 paso 3 + 2.5).

        - Name del paquete: CodigoBarras si existe, sino str(NumeroContenedor).
        - IdExternoContenedor se guarda en stock.quant.package.wis_id_externo.
        - Por contenedor se suma CantidadPreparada por producto sin superar la
          demanda del move; si la suma supera la demanda se trunca y se loggea
          advertencia.
        - Las move_lines se asignan al paquete en orden de aparición de los moves
          (ordenados por id ascendente para consistencia).
        """
        paquetes_creados = []
        # Snapshot de cantidades restantes por move (capacidad pendiente de asignar a paquete).
        capacidad_restante = {
            ml.id: (ml.quantity_product_uom or ml.move_id.product_uom_qty or 0)
            for ml in picking.move_line_ids
        }

        for contenedor in contenedores:
            codigo_barras = contenedor.get('codigoBarras', '') or ''
            nro_contenedor = contenedor.get('numeroContenedor', '')
            id_externo = contenedor.get('idExternoContenedor', '')
            detalles_contenedor = contenedor.get('detalles', []) or []

            # Spec 2.2: name = CodigoBarras (o str(NumeroContenedor) si vacío)
            nombre_paquete = codigo_barras or (str(nro_contenedor) if nro_contenedor else f"PKG-{picking.name}-{len(paquetes_creados) + 1}")

            paquete = request.env['stock.quant.package'].sudo().search(
                [('name', '=', nombre_paquete)], limit=1
            )
            if not paquete:
                paquete = request.env['stock.quant.package'].sudo().create({
                    'name': nombre_paquete,
                    'wis_id_externo': str(id_externo) if id_externo else False,
                })
            elif id_externo and not paquete.wis_id_externo:
                paquete.sudo().write({'wis_id_externo': str(id_externo)})

            paquetes_creados.append(paquete)

            # Spec 2.5: sumar CantidadPreparada por producto dentro del contenedor.
            mapa_cantidades = {}
            for det in detalles_contenedor:
                prod = det.get('producto', '')
                cant = det.get('cantidadPreparada', 0) or 0
                if not prod or cant <= 0:
                    continue
                mapa_cantidades[prod] = mapa_cantidades.get(prod, 0) + cant

            # Asignar move_lines al paquete según las cantidades por producto.
            # Los moves se procesan en orden ascendente de id.
            for codigo_producto, cantidad_total in mapa_cantidades.items():
                cant_restante = cantidad_total
                moves_match = picking.move_ids.filtered(
                    lambda m: m.product_id.codigo_unico == codigo_producto
                ).sorted('id')

                if not moves_match:
                    _logger.warning(
                        "[WIS] confirmacionPedido | Contenedor con producto '%s' "
                        "sin move correspondiente en picking %s.",
                        codigo_producto, picking.name,
                    )
                    continue

                # Suma de demanda total del producto (para advertir si excede)
                demanda_total = sum(moves_match.mapped('product_uom_qty'))
                if cant_restante > demanda_total:
                    _logger.warning(
                        "[WIS] confirmacionPedido | Contenedor %s tiene %s del producto %s "
                        "pero la demanda del picking %s es %s. Se trunca al máximo permitido.",
                        nombre_paquete, cant_restante, codigo_producto,
                        picking.name, demanda_total,
                    )
                    cant_restante = demanda_total

                for ml in moves_match.move_line_ids.sorted('id'):
                    if cant_restante <= 0:
                        break
                    if ml.result_package_id:
                        continue
                    capacidad = capacidad_restante.get(ml.id, ml.quantity_product_uom or 0)
                    if capacidad <= 0:
                        continue
                    asignar = min(capacidad, cant_restante)
                    ml.sudo().write({
                        'result_package_id': paquete.id,
                    })
                    capacidad_restante[ml.id] = capacidad - asignar
                    cant_restante -= asignar

        return paquetes_creados

    def _handle_mercaderia_preparada(self, data):
        """Spec sección 3: WIS preparó físicamente la mercadería del pedido.

        Sólo procesa los pickings cuyo picking_type tenga metodo_preparacion_wis=True;
        si no lo tiene, loggea advertencia y continúa SIN error (el tipo simplemente
        no acepta preparaciones desde WMS).

        Para operaciones con eRemito (uses_cfe=True) y aún no emitido, este es el
        momento de emitirlo. Una falla en la emisión NO bloquea el flujo — sólo
        se loggea para intervención manual antes del despacho.
        """
        from datetime import datetime

        pedidos = data.get('pedidos', [])
        if not pedidos:
            raise ValueError("El payload no contiene pedidos.")

        # Campos opcionales: además de Pedidos[].Pedido y FechaPreparacion
        # (obligatorios), el spec permite enviar contenedores en este evento. Si
        # vienen, se crean los paquetes acá (en la preparación), y cuando llegue
        # después confirmacionPedido el helper los reusa por idempotencia.
        fecha_preparacion_raw = (
            data.get('fechaPreparacion', '')
            or data.get('fechaFacturacion', '')
        )
        fecha_preparacion = fields.Datetime.now()
        if fecha_preparacion_raw:
            for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y'):
                try:
                    fecha_preparacion = datetime.strptime(fecha_preparacion_raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                _logger.warning(
                    "No se pudo parsear fechaPreparacion '%s', usando now()",
                    fecha_preparacion_raw,
                )

        errores = []

        for pedido_data in pedidos:
            nombre_pedido = pedido_data.get('pedido')

            if not nombre_pedido:
                errores.append("Un pedido del array no tiene campo 'pedido'.")
                continue

            # Spec 3.3 paso 1: buscar por codigo_unico, luego name.
            picking = request.env['stock.picking'].sudo().search(
                [('codigo_unico', '=', nombre_pedido)], limit=1
            )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('name', '=', nombre_pedido)], limit=1
                )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('idPedidoWMS', '=', nombre_pedido)], limit=1
                )
            if not picking:
                errores.append(
                    f"No se encontró picking para pedido '{nombre_pedido}'."
                )
                continue

            # Spec 3.3 paso 1: si el tipo de operación no acepta preparaciones,
            # warning y continuar SIN error.
            if not picking.picking_type_id.metodo_preparacion_wis:
                _logger.warning(
                    "[WIS] confirmacionMercaderiaPreparada | picking %s: tipo '%s' "
                    "no tiene metodo_preparacion_wis. Se omite.",
                    picking.name, picking.picking_type_id.name,
                )
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'warning',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"confirmacionMercaderiaPreparada: tipo de operación "
                        f"'{picking.picking_type_id.name}' no acepta preparaciones desde WMS."
                    ),
                    'picking_id': picking.id,
                    'resultado': 'omitido',
                    'detalle': f"Pedido WIS: {nombre_pedido}",
                })
                continue

            if picking.state in ('done', 'cancel'):
                errores.append(
                    f"Picking '{picking.name}' está en estado '{picking.state}', "
                    "no puede recibir actualizaciones."
                )
                continue

            if picking.wms_estado in ('preparado', 'despachado', 'anulado'):
                errores.append(
                    f"Picking '{picking.name}' ya tiene wms_estado "
                    f"'{picking.wms_estado}', se omite."
                )
                continue

            try:
                # Spec 3.3 paso 2: actualizar estado y fecha.
                picking.with_context(skip_wms_integration=True).write({
                    'wms_estado':            'preparado',
                    'wms_origen':            'mercaderia_preparada',
                    'wms_fecha_preparacion': fecha_preparacion,
                })

                # Si el payload trae contenedores, crear los paquetes acá (en la
                # preparación). confirmacionPedido va a llamar al mismo helper más
                # tarde y los reutiliza por idempotencia (search por name +
                # `if ml.result_package_id: continue`).
                contenedores_prep = (
                    pedido_data.get('contenedores')
                    or data.get('contenedores')
                    or []
                )
                paquetes_creados = []
                if contenedores_prep:
                    paquetes_creados = self._crear_paquetes_desde_contenedores(
                        picking, contenedores_prep
                    )

                # Autocompletar document_type y partner (consistente con los otros
                # handlers: confirmacionRecepcion, confirmacionPedido, almacenamiento).
                # Si el picking no tiene partner_id, lo completa con el partner de la
                # company del picking_type. Si ya tiene uno (ej. asignado por el módulo
                # de crossdocking con la dirección de la sucursal destino), lo respeta —
                # así la dirección de entrega en el e-Remito es la correcta del picking.
                picking.sudo()._wis_complete_document_type()

                # Spec 3.3 paso 3: emitir eRemito si corresponde.
                self._intentar_emitir_eremito(
                    picking, origen='confirmacionMercaderiaPreparada'
                )

                # Validar el picking para dejarlo en estado 'done', replicando EXACTAMENTE
                # el patrón de _handle_confirmacion_pedido (línea 644+):
                # 1) Setear qty_done en las move_lines (sino button_validate devuelve un
                #    wizard 'Immediate Transfer' o no hace nada).
                # 2) Verificar state=='assigned' antes de validar.
                # 3) Si button_validate devuelve un dict (wizard backorder), procesarlo
                #    con process_cancel_backorder — porque devolver dict NO levanta
                #    excepción, el picking quedaba en 'assigned' silenciosamente.
                try:
                    # Paso 1: asignar cantidad por defecto a las move_lines sin qty_done
                    for move_line in picking.move_line_ids:
                        if move_line.qty_done == 0:
                            move_line.sudo().qty_done = (
                                move_line.quantity_product_uom
                                or move_line.qty_done
                                or move_line.move_id.product_uom_qty
                            )

                    # Paso 2: validar solo si el picking está listo
                    if picking.state == 'assigned':
                        res = picking.with_context(
                            skip_wms_integration=True,
                            skip_backorder=True,
                        ).button_validate()

                        # Paso 3: manejar wizard de backorder si button_validate lo devuelve
                        if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                            backorder_wiz = (
                                request.env['stock.backorder.confirmation']
                                .with_context(**res.get('context', {}))
                                .sudo()
                                .create({})
                            )
                            backorder_wiz.process_cancel_backorder()
                except Exception as e:
                    _logger.warning(
                        "[WIS] confirmacionMercaderiaPreparada | no se pudo "
                        "validar picking=%s: %s. Estado actual: %s",
                        picking.name, e, picking.state,
                    )

                _logger.info(
                    "Picking %s marcado como 'preparado' por WMS (fecha=%s)",
                    picking.name, fecha_preparacion,
                )

                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'info',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"confirmacionMercaderiaPreparada: picking {picking.name} "
                        f"marcado como preparado."
                        + (
                            f" {len(paquetes_creados)} paquete(s) creados/reusados."
                            if contenedores_prep else ""
                        )
                    ),
                    'picking_id': picking.id,
                    'resultado': 'exito',
                    'detalle': (
                        f"Fecha preparación: {fecha_preparacion}"
                        + (
                            f" | Contenedores: {len(contenedores_prep)}"
                            if contenedores_prep else ""
                        )
                    ),
                    'payload_webhook': json.dumps(pedido_data, ensure_ascii=False, indent=2),
                })

            except Exception as e:
                _logger.exception(
                    "Error procesando preparación para picking '%s': %s",
                    nombre_pedido, e,
                )
                errores.append(f"Error en picking '{nombre_pedido}': {str(e)}")

        if errores:
            raise ValueError(
                f"Se procesaron {len(pedidos) - len(errores)}/{len(pedidos)} pedidos. "
                f"Errores: {' | '.join(errores)}"
            )

    
    def _handle_pedidos_anulados(self, data):
        """Spec sección 4: WIS anuló uno o más pedidos.

        Cancela el picking correspondiente vía action_cancel() y registra el detalle
        por producto en el log del picking (Producto + CantidadAnulada + Motivo +
        FechaAlta + Aplicacion). Verifica picking_type_id.metodo_cancelacion_wis;
        si False, warning y continuar sin error.
        """
        from datetime import datetime

        pedidos = data.get('pedidosAnulados', [])
        if not pedidos:
            raise ValueError("El payload no contiene pedidosAnulados.")

        errores = []

        for pedido_data in pedidos:
            nombre_pedido = pedido_data.get('pedido')
            codigo_agente = pedido_data.get('codigoAgente', '')

            if not nombre_pedido:
                errores.append("Un pedido del array no tiene campo 'pedido'.")
                continue

            detalles = pedido_data.get('detalles', []) or []
            motivo = next(
                (d.get('motivo', '') for d in detalles if d.get('motivo')),
                'Sin motivo informado'
            )
            fecha_alta_raw = next(
                (d.get('fechaAlta', '') for d in detalles if d.get('fechaAlta')),
                ''
            )

            fecha_anulacion = fields.Datetime.now()
            if fecha_alta_raw:
                for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y'):
                    try:
                        fecha_anulacion = datetime.strptime(fecha_alta_raw, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    _logger.warning(
                        "No se pudo parsear fechaAlta '%s', usando now()", fecha_alta_raw
                    )

            # Spec 4.3 paso 1: buscar por codigo_unico, luego name, luego idPedidoWMS.
            picking = request.env['stock.picking'].sudo().search(
                [('codigo_unico', '=', nombre_pedido)], limit=1
            )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('name', '=', nombre_pedido)], limit=1
                )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('idPedidoWMS', '=', nombre_pedido)], limit=1
                )
            if not picking:
                errores.append(f"No se encontró picking para pedido '{nombre_pedido}'.")
                continue

            # Spec 4.3 paso 1: si el tipo no acepta cancelaciones, warning y continuar.
            # FIX: usar picking_type_id (operation_type_id no existe en stock.picking).
            if not picking.picking_type_id.metodo_cancelacion_wis:
                _logger.warning(
                    "[WIS] pedidosAnulados | picking %s: tipo '%s' no tiene "
                    "metodo_cancelacion_wis. Se omite.",
                    picking.name, picking.picking_type_id.name,
                )
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'warning',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"pedidosAnulados: tipo de operación "
                        f"'{picking.picking_type_id.name}' no acepta cancelaciones desde WMS."
                    ),
                    'picking_id': picking.id,
                    'resultado': 'omitido',
                    'detalle': f"Pedido WIS: {nombre_pedido}",
                })
                continue

            if picking.state == 'done':
                errores.append(
                    f"Picking '{picking.name}' ya fue validado (done), no se puede anular."
                )
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"pedidosAnulados: picking {picking.name} ya validado (done). "
                        f"Requiere intervención manual."
                    ),
                    'picking_id': picking.id,
                    'resultado': 'error',
                    'detalle': f"Motivo WIS: {motivo}",
                })
                continue

            if picking.state == 'cancel' or picking.wms_estado == 'anulado':
                _logger.warning(
                    "Picking '%s' ya estaba cancelado/anulado, se omite.", picking.name
                )
                continue

            try:
                # Spec 4.3 paso 2: registrar el detalle por producto en el log del picking.
                lineas_detalle_html = ""
                for det in detalles:
                    prod_cod = det.get('producto', '')
                    cant = det.get('cantidadAnulada', 0)
                    motivo_lin = det.get('motivo', '')
                    fecha_lin = det.get('fechaAlta', '')
                    aplicacion = det.get('aplicacion', '')
                    lineas_detalle_html += (
                        f"<tr>"
                        f"<td style='padding:4px 8px;'>{prod_cod}</td>"
                        f"<td style='padding:4px 8px;'>{cant}</td>"
                        f"<td style='padding:4px 8px;'>{motivo_lin}</td>"
                        f"<td style='padding:4px 8px;'>{fecha_lin}</td>"
                        f"<td style='padding:4px 8px;'>{aplicacion}</td>"
                        f"</tr>"
                    )
                    request.env['wms.integracion.log'].sudo().create({
                        'fecha': fields.Datetime.now(),
                        'nivel': 'warning',
                        'modelo': 'stock.picking',
                        'texto': (
                            f"pedidosAnulados (detalle): {prod_cod} cant={cant} "
                            f"motivo='{motivo_lin}' aplicacion='{aplicacion}'"
                        ),
                        'picking_id': picking.id,
                        'resultado': 'exito',
                        'detalle': (
                            f"Producto: {prod_cod} | CantidadAnulada: {cant} | "
                            f"FechaAlta: {fecha_lin} | Aplicacion: {aplicacion} | "
                            f"CodigoAgente: {codigo_agente}"
                        ),
                    })

                # Spec 4.3 paso 3: cancelar el picking
                picking.with_context(skip_wms_integration=True).write({
                    'wms_estado':           'anulado',
                    'wms_origen':           'anulacion',
                    'wms_fecha_anulacion':  fecha_anulacion,
                    'wms_motivo_anulacion': motivo,
                })
                picking.sudo().action_cancel()

                picking.sudo().message_post(
                    body=Markup(
                        f"<b>WMS — Pedido anulado</b><br/>"
                        f"Fecha de anulación: {fecha_anulacion}<br/>"
                        f"Motivo agregado: {motivo}<br/>"
                        f"Código agente: {codigo_agente or '—'}<br/>"
                        f"<br/>"
                        f"<table style='border-collapse:collapse; width:100%;'>"
                        f"<thead><tr style='background:#f0f0f0;'>"
                        f"<th style='text-align:left; padding:4px 8px;'>Producto</th>"
                        f"<th style='text-align:left; padding:4px 8px;'>Cantidad anulada</th>"
                        f"<th style='text-align:left; padding:4px 8px;'>Motivo</th>"
                        f"<th style='text-align:left; padding:4px 8px;'>Fecha</th>"
                        f"<th style='text-align:left; padding:4px 8px;'>Aplicación</th>"
                        f"</tr></thead>"
                        f"<tbody>{lineas_detalle_html}</tbody>"
                        f"</table>"
                    ),
                    subtype_xmlid='mail.mt_note',
                )

                _logger.warning(
                    "Picking %s ANULADO por WMS. Motivo: %s", picking.name, motivo
                )

                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'warning',
                    'modelo': 'stock.picking',
                    'texto': (
                        f"pedidosAnulados: picking {picking.name} cancelado por WMS."
                    ),
                    'picking_id': picking.id,
                    'resultado': 'exito',
                    'detalle': (
                        f"Motivo: {motivo} | Fecha anulación: {fecha_anulacion} | "
                        f"Líneas: {len(detalles)} | CodigoAgente: {codigo_agente}"
                    ),
                    'payload_webhook': json.dumps(pedido_data, ensure_ascii=False, indent=2),
                })

            except Exception as e:
                _logger.exception(
                    "Error al anular picking '%s': %s", nombre_pedido, e
                )
                errores.append(f"Error en picking '{nombre_pedido}': {str(e)}")

        if errores:
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': (
                    f"pedidosAnulados: {len(errores)} error(s) "
                    f"de {len(pedidos)} pedido(s)."
                ),
                'picking_id': False,
                'resultado': 'error',
                'detalle': ' | '.join(errores),
            })
            raise ValueError(
                f"Se procesaron {len(pedidos) - len(errores)}/{len(pedidos)} pedidos. "
                f"Errores: {' | '.join(errores)}"
            )

    def _handle_ajustes(self, data):
        """Spec sección 6: ajustes de stock generados por WIS sin relación a un picking.

        Por cada Ajustes[]:
        - Si TipoAjuste == config.tipo_ajuste_recuento: action_apply_inventory sobre
          stock.quant en la ubicación de existencias (spec 6.6).
        - Sino: crear picking de transferencia interna usando picking_type_ajuste_wis_id
          (entradas con CantidadMovimiento > 0) o su return_picking_type_id (salidas
          con CantidadMovimiento < 0) (spec 6.5).

        Lote/vencimiento: si Identificador != '*', se asigna al lote del move.
        Validación: skip_wms_integration=True.
        """
        from datetime import datetime

        ajustes = data.get('ajustes', []) or data
        if isinstance(ajustes, dict):
            ajustes = ajustes.get('ajustes', []) or []
        if not ajustes:
            raise ValueError("El payload no contiene ajustes.")

        # Spec 6.4: configuración obligatoria
        config = request.env['integracion_wis.integracion_wis'].sudo().search(
            [('company_id', '=', request.env.company.id)], limit=1,
        )
        if not config:
            config = request.env['integracion_wis.integracion_wis'].sudo().search([], limit=1)

        picking_type_ajuste = config.picking_type_ajuste_wis_id if config else False
        tipo_recuento = (config.tipo_ajuste_recuento or '').strip() if config else ''

        errores = []
        procesados_movimiento = 0
        procesados_recuento = 0

        for ajuste in ajustes:
            cod_producto    = ajuste.get('producto', '')
            cant_movimiento = ajuste.get('cantidadMovimiento', 0) or 0
            tipo_ajuste     = (ajuste.get('tipoAjuste') or '').strip()
            motivo          = ajuste.get('motivo', '')
            desc_motivo     = ajuste.get('descripcionMotivo', '')
            nro_ajuste      = ajuste.get('numeroAjusteStock', 0) or 0
            fecha_real_raw  = ajuste.get('fechaRealizacion', '')
            identificador   = (ajuste.get('identificador') or '').strip()
            vencimiento     = ajuste.get('fechaVencimiento', '')

            if not cod_producto:
                errores.append("Ajuste sin producto.")
                continue

            # Spec 6.5 paso 1: buscar el producto
            producto = request.env['product.product'].sudo().search(
                [('codigo_unico', '=', cod_producto)], limit=1,
            )
            if not producto:
                msg = f"ajustes: producto codigo_unico='{cod_producto}' no encontrado."
                _logger.error("[WIS] %s", msg)
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'product.product',
                    'texto': msg,
                    'picking_id': False,
                    'resultado': 'error',
                    'detalle': f"NumeroAjusteStock={nro_ajuste}",
                })
                errores.append(msg)
                continue

            # Parsear fecha
            fecha_real = fields.Datetime.now()
            if fecha_real_raw:
                for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y'):
                    try:
                        fecha_real = datetime.strptime(fecha_real_raw, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    _logger.warning(
                        "[WIS] ajustes | fechaRealizacion '%s' no parseable, usando now()",
                        fecha_real_raw,
                    )

            # Spec 6.3: si TipoAjuste coincide con recuento físico → action_apply_inventory.
            es_recuento = bool(tipo_recuento) and (tipo_ajuste == tipo_recuento)

            if es_recuento:
                try:
                    self._aplicar_recuento_fisico(
                        producto=producto,
                        cant_movimiento=cant_movimiento,
                        nro_ajuste=nro_ajuste,
                        desc_motivo=desc_motivo,
                        config=config,
                    )
                    procesados_recuento += 1
                except Exception as e:
                    _logger.exception(
                        "[WIS] ajustes | error en recuento físico de %s: %s",
                        producto.display_name, e,
                    )
                    errores.append(
                        f"Recuento físico {producto.display_name}: {str(e)}"
                    )
                continue

            # Spec 6.5: ajuste de movimiento (caso general)
            if not picking_type_ajuste:
                msg = (
                    f"ajustes: picking_type_ajuste_wis_id no configurado en "
                    f"integracion_wis. Ajuste NumeroAjusteStock={nro_ajuste} sin procesar."
                )
                _logger.error("[WIS] %s", msg)
                request.env['wms.integracion.log'].sudo().create({
                    'fecha': fields.Datetime.now(),
                    'nivel': 'error',
                    'modelo': 'integracion_wis.integracion_wis',
                    'texto': msg,
                    'picking_id': False,
                    'resultado': 'error',
                    'detalle': json.dumps(ajuste, ensure_ascii=False, indent=2),
                })
                errores.append(msg)
                continue

            # Spec 6.5 paso 2: determinar tipo de operación según signo
            if cant_movimiento > 0:
                tipo_op = picking_type_ajuste
            else:
                tipo_op = picking_type_ajuste.return_picking_type_id
                if not tipo_op:
                    msg = (
                        f"ajustes: picking_type_ajuste_wis_id '{picking_type_ajuste.name}' "
                        f"sin return_picking_type_id configurado para salidas."
                    )
                    _logger.error("[WIS] %s", msg)
                    errores.append(msg)
                    continue

            try:
                self._crear_picking_ajuste(
                    producto=producto,
                    cant_abs=abs(cant_movimiento),
                    tipo_op=tipo_op,
                    nro_ajuste=nro_ajuste,
                    desc_motivo=desc_motivo,
                    motivo=motivo,
                    fecha_real=fecha_real,
                    identificador=identificador,
                    vencimiento=vencimiento,
                    tipo_ajuste_wis=tipo_ajuste,
                )
                procesados_movimiento += 1
            except Exception as e:
                _logger.exception(
                    "[WIS] ajustes | error creando picking de ajuste para %s: %s",
                    producto.display_name, e,
                )
                errores.append(
                    f"Ajuste {producto.display_name} ({nro_ajuste}): {str(e)}"
                )

        request.env['wms.integracion.log'].sudo().create({
            'fecha': fields.Datetime.now(),
            'nivel': 'info' if not errores else 'warning',
            'modelo': 'integracion_wis.integracion_wis',
            'texto': (
                f"ajustes: procesados {procesados_movimiento} movimiento(s) y "
                f"{procesados_recuento} recuento(s); errores={len(errores)}."
            ),
            'picking_id': False,
            'resultado': 'exito' if not errores else 'error',
            'detalle': ' | '.join(errores) if errores else '',
            'payload_webhook': json.dumps(data, ensure_ascii=False, indent=2),
        })

        if errores:
            raise ValueError(
                f"ajustes: {len(errores)} con error de {len(ajustes)} total. "
                f"{' | '.join(errores)}"
            )

    def _aplicar_recuento_fisico(self, producto, cant_movimiento, nro_ajuste, desc_motivo, config):
        """Spec 6.6: ajuste directo sobre stock.quant vía action_apply_inventory.

        La ubicación de existencias se toma de config.ubicacionesAConsultar (la primera
        configurada). Si no hay ubicación configurada, se busca el quant principal del
        producto en ubicaciones internas.
        """
        ubicacion = False
        if config and config.ubicacionesAConsultar:
            ubicacion = config.ubicacionesAConsultar[0]

        quant = False
        if ubicacion:
            quant = request.env['stock.quant'].sudo().search([
                ('product_id', '=', producto.id),
                ('location_id', '=', ubicacion.id),
            ], limit=1)
        if not quant:
            # Fallback: tomar el quant del producto en alguna ubicación interna.
            quant = request.env['stock.quant'].sudo().search([
                ('product_id', '=', producto.id),
                ('location_id.usage', '=', 'internal'),
            ], limit=1)

        if quant:
            cant_actual = quant.quantity
            cant_nueva = cant_actual + cant_movimiento
            if cant_nueva < 0:
                _logger.warning(
                    "[WIS] ajustes | recuento físico genera stock negativo para %s "
                    "(actual=%s + delta=%s = %s)",
                    producto.display_name, cant_actual, cant_movimiento, cant_nueva,
                )
            quant.sudo().inventory_quantity = cant_nueva
            quant.sudo().action_apply_inventory()
        else:
            # Si no existe quant, crear uno en la ubicación configurada.
            if not ubicacion:
                raise ValueError(
                    f"Recuento físico para {producto.display_name}: sin quant existente "
                    f"y sin ubicación de existencias configurada en integracion_wis."
                )
            quant = request.env['stock.quant'].sudo().create({
                'product_id': producto.id,
                'location_id': ubicacion.id,
                'inventory_quantity': cant_movimiento,
            })
            quant.sudo().action_apply_inventory()

        request.env['wms.integracion.log'].sudo().create({
            'fecha': fields.Datetime.now(),
            'nivel': 'info',
            'modelo': 'stock.quant',
            'texto': (
                f"ajustes (recuento físico): {producto.display_name} "
                f"delta={cant_movimiento} (NumeroAjusteStock={nro_ajuste})"
            ),
            'picking_id': False,
            'resultado': 'exito',
            'detalle': f"Motivo: {desc_motivo}",
        })

    def _crear_picking_ajuste(self, producto, cant_abs, tipo_op, nro_ajuste, desc_motivo,
                              motivo, fecha_real, identificador, vencimiento, tipo_ajuste_wis):
        """Spec 6.5 paso 3: crea y valida un picking de ajuste (entrada o salida)
        usando el tipo_op pasado por argumento.
        """
        from datetime import datetime

        origin = f"WIS-AJ-{nro_ajuste}"
        if desc_motivo:
            origin = f"{origin} - {desc_motivo}"

        # Punto 4: partner_id de la company es requerido para que el compute de
        # l10n_latam_document_type_id se dispare (depende de partner_id + punto_emision_id).
        company = tipo_op.company_id or request.env.company
        partner_fallback = company.partner_id.id if company and company.partner_id else False

        picking = request.env['stock.picking'].sudo().create({
            'picking_type_id': tipo_op.id,
            'location_id':     tipo_op.default_location_src_id.id,
            'location_dest_id': tipo_op.default_location_dest_id.id,
            'origin':          origin,
            'scheduled_date':  fecha_real,
            'partner_id':      partner_fallback,
            # Punto 2: tipo de evento WIS que generó este picking.
            'wms_origen':      'ajuste',
        })

        move = request.env['stock.move'].sudo().create({
            'name':            producto.display_name,
            'picking_id':      picking.id,
            'product_id':      producto.id,
            'product_uom':     producto.uom_id.id,
            'product_uom_qty': cant_abs,
            'location_id':     tipo_op.default_location_src_id.id,
            'location_dest_id': tipo_op.default_location_dest_id.id,
        })

        # Lote / vencimiento (spec 6.5 paso 3)
        lote_id = False
        if identificador and identificador != '*' and producto.tracking in ('lot', 'serial'):
            lote = request.env['stock.lot'].sudo().search([
                ('name', '=', identificador),
                ('product_id', '=', producto.id),
                ('company_id', '=', picking.company_id.id),
            ], limit=1)
            if not lote:
                lote_vals = {
                    'name': identificador,
                    'product_id': producto.id,
                    'company_id': picking.company_id.id,
                }
                if vencimiento:
                    try:
                        lote_vals['expiration_date'] = datetime.strptime(vencimiento, '%d/%m/%Y')
                    except ValueError:
                        _logger.warning(
                            "[WIS] ajustes | vencimiento '%s' no parseable para lote %s",
                            vencimiento, identificador,
                        )
                lote = request.env['stock.lot'].sudo().create(lote_vals)
            lote_id = lote.id

        picking.with_context(skip_wms_integration=True).sudo().action_confirm()
        picking.with_context(skip_wms_integration=True).sudo().action_assign()

        # Punto 4: forzar el cómputo de l10n_latam_document_type_id ahora que el picking
        # tiene partner_id + picking_type_id (los onchange de vista no se disparan en create()).
        picking.sudo()._wis_complete_document_type()

        # Si el move tiene move_line, asignar lote y cantidad; sino, crearla.
        if move.move_line_ids:
            for ml in move.move_line_ids.sorted('id'):
                ml_vals = {'quantity': cant_abs}
                if lote_id:
                    ml_vals['lot_id'] = lote_id
                ml.sudo().write(ml_vals)
                cant_abs = 0
        if cant_abs > 0:
            ml_vals = {
                'move_id': move.id,
                'product_id': producto.id,
                'product_uom_id': producto.uom_id.id,
                'location_id': move.location_id.id,
                'location_dest_id': move.location_dest_id.id,
                'quantity': cant_abs,
                'picking_id': picking.id,
            }
            if lote_id:
                ml_vals['lot_id'] = lote_id
            request.env['stock.move.line'].sudo().create(ml_vals)

        res = picking.with_context(
            skip_wms_integration=True,
            skip_backorder=True,
        ).sudo().button_validate()
        if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
            backorder_wiz = (
                request.env['stock.backorder.confirmation']
                .with_context(**res.get('context', {}))
                .sudo()
                .create({})
            )
            backorder_wiz.process_cancel_backorder()

        request.env['wms.integracion.log'].sudo().create({
            'fecha': fields.Datetime.now(),
            'nivel': 'info',
            'modelo': 'stock.picking',
            'texto': (
                f"ajustes (movimiento): picking {picking.name} validado "
                f"({tipo_op.name}, producto={producto.display_name}, qty={move.product_uom_qty})."
            ),
            'picking_id': picking.id,
            'resultado': 'exito',
            'detalle': (
                f"NumeroAjusteStock={nro_ajuste} | Motivo={motivo} | "
                f"DescMotivo={desc_motivo} | TipoAjuste WIS='{tipo_ajuste_wis}' | "
                f"Identificador='{identificador}'"
            ),
        })

    def _handle_consulta_stock(self, data):
        pass

    def _handle_almacenamiento(self, data):
        """Spec sección 5: WIS guardó físicamente la mercadería en depósito.

        Corresponde al movimiento interno Entrada → Existencias. Llega después del
        confirmacionRecepcion correspondiente — el picking de recepción ya estará
        validado (state=done).

        Paso 1: buscar el picking de recepción (IMPO) por Serializado / name.
        Paso 2: obtener la OC desde purchase_id; buscar el picking interno (INT)
                relacionado con esa OC en estado pendiente.
        Paso 3: mapear cantidades por producto y asignar lotes desde Identificador
                / Vencimiento.
        Paso 4: validar el picking interno con skip_wms_integration=True (sin
                backorder; la diferencia es crossdocking).
        """
        from datetime import datetime

        serializado    = data.get('serializado', '') or data.get('numeroSerializado', '')
        codigo_agente  = data.get('codigoAgente', '')
        detalles       = data.get('detalles', []) or []

        if not serializado:
            raise ValueError("Almacenamiento sin 'serializado' / 'numeroSerializado'.")
        if not detalles:
            raise ValueError(f"Almacenamiento '{serializado}' sin detalles.")

        # Paso 1: buscar picking de recepción
        picking_imp = request.env['stock.picking'].sudo().search(
            [('codigo_unico', '=', serializado)], limit=1
        )
        if not picking_imp:
            picking_imp = request.env['stock.picking'].sudo().search(
                [('name', '=', serializado)], limit=1
            )
        if not picking_imp:
            msg = f"almacenamiento: no se encontró picking de recepción para '{serializado}'."
            _logger.error("[WIS] %s", msg)
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': msg,
                'picking_id': False,
                'resultado': 'error',
                'detalle': f"CodigoAgente: {codigo_agente}",
                'payload_webhook': json.dumps(data, ensure_ascii=False, indent=2),
            })
            raise ValueError(msg)

        if picking_imp.state != 'done':
            msg = (
                f"almacenamiento: picking de recepción '{picking_imp.name}' está en "
                f"state='{picking_imp.state}', se espera 'done'."
            )
            _logger.error("[WIS] %s", msg)
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': msg,
                'picking_id': picking_imp.id,
                'resultado': 'error',
                'detalle': f"State actual: {picking_imp.state}",
            })
            raise ValueError(msg)

        # Paso 2: obtener grupo de abastecimiento y picking interno.
        # Usamos group_id (procurement group) en lugar de purchase_id: stock.picking.purchase_id
        # es un related no-stored (move_ids.purchase_line_id.order_id) que no resuelve cuando los
        # moves del INT chained no heredaron purchase_line_id de su move_orig. group_id en cambio
        # se propaga toda la cadena IMPO→INT.
        group = picking_imp.group_id
        purchase = picking_imp.purchase_id  # solo para mensajes de log
        if not group:
            msg = (
                f"almacenamiento: picking de recepción '{picking_imp.name}' sin "
                f"grupo de abastecimiento (group_id)."
            )
            _logger.error("[WIS] %s", msg)
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': msg,
                'picking_id': picking_imp.id,
                'resultado': 'error',
                'detalle': '',
            })
            raise ValueError(msg)

        pickings_int = request.env['stock.picking'].sudo().search([
            ('group_id', '=', group.id),
            ('picking_type_id.code', '=', 'internal'),
            ('state', 'in', ('waiting', 'confirmed', 'assigned')),
            ('location_id', '=', picking_imp.location_dest_id.id),
        ], order='id desc')
        # Si hay más de uno, tomar el de mayor ID (spec 5.4 paso 2).
        picking_int = pickings_int[:1]
        if not picking_int:
            ref_oc = purchase.name if purchase else group.name
            msg = (
                f"almacenamiento: no se encontró picking interno (Entrada→Existencias) "
                f"para grupo '{group.name}' (OC '{ref_oc}') en estado pendiente."
            )
            _logger.error("[WIS] %s", msg)
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': msg,
                'picking_id': picking_imp.id,
                'resultado': 'error',
                'detalle': f"Grupo: {group.name} | OC: {ref_oc} | Recepción: {picking_imp.name}",
            })
            raise ValueError(msg)

        # Paso 3: mapear cantidades por producto y asignar lotes/vencimientos
        cant_por_codigo = {}
        lote_por_codigo = {}
        venc_por_codigo = {}
        for det in detalles:
            cod = det.get('producto', '')
            cant = det.get('cantidadAlmacenada', 0) or 0
            identificador = (det.get('identificador') or '').strip()
            vencimiento = det.get('vencimiento', '')
            if not cod or cant <= 0:
                continue
            cant_por_codigo[cod] = cant_por_codigo.get(cod, 0) + cant
            if identificador and identificador != '*':
                # Si llega más de una línea por producto con distintos identificadores,
                # se queda con el primero (el handler de spec 5 no contempla split por lote).
                lote_por_codigo.setdefault(cod, identificador)
            if vencimiento:
                venc_por_codigo.setdefault(cod, vencimiento)

        # Procesar moves del picking interno en orden ascendente de id (spec 5.4 paso 3).
        moves_int = picking_int.move_ids.sorted('id')
        avisos = []
        for move in moves_int:
            cod_prod = move.product_id.codigo_unico
            cant_alm = cant_por_codigo.get(cod_prod, 0)
            if cant_alm <= 0:
                # Producto no presente en este almacenamiento — se deja qty_done=0
                # (la conciliación con la recepción es crossdocking, spec 5.4 paso 4).
                continue

            if cant_alm > move.product_uom_qty:
                _logger.warning(
                    "[WIS] almacenamiento | %s: cantidad %s supera demanda %s en move %s. "
                    "Se trunca al máximo.",
                    cod_prod, cant_alm, move.product_uom_qty, move.id,
                )
                avisos.append(
                    f"{cod_prod}: almacenado={cant_alm} > demanda={move.product_uom_qty}"
                )
                cant_alm = move.product_uom_qty

            # Resolver lote si el producto maneja tracking y llegó identificador
            lote_id = False
            lote_codigo = lote_por_codigo.get(cod_prod)
            venc_str = venc_por_codigo.get(cod_prod)
            if lote_codigo and move.product_id.tracking in ('lot', 'serial'):
                lote = request.env['stock.lot'].sudo().search([
                    ('name', '=', lote_codigo),
                    ('product_id', '=', move.product_id.id),
                    ('company_id', '=', picking_int.company_id.id),
                ], limit=1)
                if not lote:
                    lote_vals = {
                        'name': lote_codigo,
                        'product_id': move.product_id.id,
                        'company_id': picking_int.company_id.id,
                    }
                    if venc_str:
                        try:
                            lote_vals['expiration_date'] = datetime.strptime(venc_str, '%d/%m/%Y')
                        except ValueError:
                            _logger.warning(
                                "[WIS] almacenamiento | vencimiento '%s' no parseable para lote %s",
                                venc_str, lote_codigo,
                            )
                    lote = request.env['stock.lot'].sudo().create(lote_vals)
                lote_id = lote.id

            # Asignar cantidades a las move_lines
            cant_restante = cant_alm
            for ml in move.move_line_ids.sorted('id'):
                if cant_restante <= 0:
                    ml.sudo().quantity = 0
                    continue
                capacidad = ml.quantity_product_uom or move.product_uom_qty
                asignar = min(capacidad, cant_restante)
                update_vals = {'quantity': asignar}
                if lote_id:
                    update_vals['lot_id'] = lote_id
                ml.sudo().write(update_vals)
                cant_restante -= asignar

            # Si no había move_line pero hay que asignar, crear una.
            if not move.move_line_ids and cant_restante > 0:
                ml_vals = {
                    'move_id': move.id,
                    'product_id': move.product_id.id,
                    'product_uom_id': move.product_uom.id,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                    'quantity': cant_restante,
                    'picking_id': picking_int.id,
                }
                if lote_id:
                    ml_vals['lot_id'] = lote_id
                request.env['stock.move.line'].sudo().create(ml_vals)

        # Punto 2: marcar origen WIS y propagar location/panel si vienen en payload.
        almacen_vals = {'wms_origen': 'almacenamiento'}
        wis_loc = data.get('locationID') or data.get('LocationID') or data.get('location_id')
        wis_panel = data.get('panelID') or data.get('PanelID') or data.get('panel_id')
        if wis_loc:
            almacen_vals['wis_location_id'] = str(wis_loc)
        if wis_panel:
            almacen_vals['wis_panel_id'] = str(wis_panel)
        picking_int.with_context(skip_wms_integration=True).sudo().write(almacen_vals)

        # Punto 4: autocompletar document_type antes de validar (si aplica CFE).
        picking_int.sudo()._wis_complete_document_type()

        # Paso 4: validar el picking interno (sin backorder)
        try:
            if picking_int.state in ('waiting', 'confirmed'):
                picking_int.sudo().action_assign()
            res = picking_int.with_context(
                skip_wms_integration=True,
                skip_backorder=True,
            ).sudo().button_validate()
            if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                backorder_wiz = (
                    request.env['stock.backorder.confirmation']
                    .with_context(**res.get('context', {}))
                    .sudo()
                    .create({})
                )
                backorder_wiz.process_cancel_backorder()
        except Exception as e:
            _logger.exception(
                "[WIS] almacenamiento | error al validar picking interno %s: %s",
                picking_int.name, e,
            )
            request.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': (
                    f"almacenamiento: error validando picking interno {picking_int.name}"
                ),
                'picking_id': picking_int.id,
                'resultado': 'error',
                'detalle': str(e),
                'payload_webhook': json.dumps(data, ensure_ascii=False, indent=2),
            })
            raise

        request.env['wms.integracion.log'].sudo().create({
            'fecha': fields.Datetime.now(),
            'nivel': 'info',
            'modelo': 'stock.picking',
            'texto': (
                f"almacenamiento: picking interno {picking_int.name} validado "
                f"(recepción origen: {picking_imp.name}, OC: {purchase.name})."
            ),
            'picking_id': picking_int.id,
            'resultado': 'exito',
            'detalle': (
                f"Productos almacenados: {len(cant_por_codigo)} | "
                f"Avisos: {' | '.join(avisos) if avisos else 'ninguno'}"
            ),
            'payload_webhook': json.dumps(data, ensure_ascii=False, indent=2),
        })

    def _handle_confirmacion_produccion(self, data):
        pass

    def _handle_test(self, data):
        pass
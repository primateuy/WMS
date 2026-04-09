import json
import hmac
import hashlib
import logging
import time
from odoo import http, fields
from odoo.http import request
from markupsafe import Markup
_logger = logging.getLogger(__name__)

class WebhookWIS(http.Controller):

    @http.route('/webhook/wis/callback', type='json', auth='public', methods=['POST'], csrf=False)
    def callback(self, **kwargs):
        signature = request.httprequest.headers.get('X-Hub-Signature')
        body = request.httprequest.get_data()

        if not self._verify_signature(body, signature):
            return {'status': 401, 'detail': 'Firma inválida'}

        try:
            payload = json.loads(body)
        except Exception as e:
            response = {'status': 400, 'detail': 'JSON inválido'}
            self._create_log(body.decode('utf-8') if body else '', response, 'desconocido', 'error', 0)
            return response

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
            return False

        secret = request.env['ir.config_parameter'].sudo().get_param('wis.webhook_secret', '')
        if not secret:
            _logger.warning("No está configurado wis.webhook_secret")
            return False

        return hmac.compare_digest(secret, received_signature)

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

            picking = None


            picking = request.env['stock.picking'].sudo().search(
                [('codigo_unico', '=', numero_referencia)], limit=1
            )

            if not picking:
                _logger.info("No se encontró picking con codigo_unico %s", numero_referencia)
                errores.append(
                    f"No se encontró picking para referencia '{numero_referencia}' "
                    f"(tipo: {tipo_referencia})."
                )
                continue  
            


            try:
                mapa_cantidades = {}

                mapa_por_codigo = {}
                for detalle in detalles:
                    id_linea    = detalle.get('idLineaSistemaExterno', '')
                    cod_producto = detalle.get('producto', '')
                    cant_consumida = detalle.get('cantidadConsumida', 0)

                    move_id = None
                    if id_linea and '__stock.move__' in id_linea:
                        try:
                            move_id = int(id_linea.split('__stock.move__')[-1])
                        except (ValueError, IndexError):
                            pass

                    if move_id:
                        mapa_cantidades[move_id] = cant_consumida
                    if cod_producto:
                        mapa_por_codigo[cod_producto] = cant_consumida

                es_parcial = False

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

                    if cant_recibida < move.product_uom_qty:
                        es_parcial = True
                        _logger.info(
                            "Recepción parcial - Producto %s: solicitado=%s recibido=%s",
                            move.product_id.name, move.product_uom_qty, cant_recibida,
                        )

                    cant_restante = cant_recibida
                    for move_line in move.move_line_ids:
                        capacidad_linea = move_line.quantity or move.product_uom_qty
                        if cant_restante <= 0:
                            move_line.sudo().qty_done = 0
                        elif cant_restante >= capacidad_linea:
                            move_line.sudo().qty_done = capacidad_linea
                            cant_restante -= capacidad_linea
                        else:
                            move_line.sudo().qty_done = cant_restante
                            cant_restante = 0

                

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
       
        pedidos = data.get('pedidos', [])
        if not pedidos:
            raise ValueError("El payload no contiene pedidos.")

        matricula          = data.get('matricula', '')
        descripcion_camion = data.get('descripcionCamion', '')
        transportadora     = data.get('transportadora', '')
        fecha_cierre_raw   = data.get('fechaCierre', '') or data.get('fechaFacturacion', '')

        fecha_despacho = fields.Datetime.now()
        if fecha_cierre_raw:
            try:
                from datetime import datetime
                fecha_despacho = datetime.strptime(fecha_cierre_raw, '%d/%m/%Y %H:%M')
            except ValueError:
                _logger.warning(
                    "No se pudo parsear fechaCierre '%s', usando now()", fecha_cierre_raw
                )

        contenedores = data.get('contenedores', [])
        total_bultos = sum(c.get('cantidadBultos', 0) for c in contenedores)
        peso_total   = sum(c.get('pesoReal', 0.0)     for c in contenedores)
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

            picking = request.env['stock.picking'].sudo().search(
                [('name', '=', nombre_pedido)], limit=1
            )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('codigo_unico', '=', nombre_pedido)], limit=1
                )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('idPedidoWMS', '=', nombre_pedido)], limit=1
                )
            if not picking:
                errores.append(f"No se encontró picking para pedido '{nombre_pedido}'.")
                continue

            if picking.state == 'done':
                errores.append(f"Picking '{picking.name}' ya está validado, se omite.")
                continue
            if picking.state == 'cancel':
                errores.append(f"Picking '{picking.name}' está cancelado, no se puede despachar.")
                continue

            if picking.wms_estado in ('despachado', 'anulado'):
                errores.append(
                    f"Picking '{picking.name}' ya tiene wms_estado '{picking.wms_estado}', se omite."
                )
                continue

            try:
                picking.with_context(skip_wms_integration=True).write({
                    'wms_estado':        'despachado',
                    'wms_fecha_despacho': fecha_despacho,
                    'wms_transportadora': str(transportadora) if transportadora else matricula,
                    'wms_nro_remito':    codigo_origen,
                    'wms_cantidad_bultos': total_bultos,
                    'wms_peso_total':    peso_total,
                    'wms_observaciones_preparacion': (
                        f"Despacho — Camión: {matricula} ({descripcion_camion}) | "
                        f"Precintos: {precintos or '—'} | Memo: {memo or '—'}"
                    ),
                })

                paquetes_creados = []
                for contenedor in contenedores:
                    nro     = contenedor.get('numeroContenedor', '')
                    tipo    = contenedor.get('tipoContenedor', '')
                    peso    = contenedor.get('pesoReal', 0.0)
                    precinto1 = contenedor.get('precinto1', '')
                    precinto2 = contenedor.get('precinto2', '')
                    descripcion = contenedor.get('descripcionContenedor', '')

                    nombre_paquete = f"PKG-{nro}" if nro else f"PKG-{picking.name}"

                    paquete = request.env['stock.quant.package'].sudo().search(
                        [('name', '=', nombre_paquete)], limit=1
                    )
                    if not paquete:
                        paquete = request.env['stock.quant.package'].sudo().create({
                            'name': nombre_paquete,
                        })

                    paquetes_creados.append(paquete)

                    _logger.info(
                        "Paquete creado/encontrado: %s | Peso: %s kg | Precintos: %s / %s",
                        nombre_paquete, peso, precinto1, precinto2
                    )

                if paquetes_creados:
                    paquete_default = paquetes_creados[0]
                    for move_line in picking.move_line_ids:
                        if not move_line.result_package_id:
                            move_line.sudo().write({
                                'result_package_id': paquete_default.id,
                            })

                for move_line in picking.move_line_ids:
                    if move_line.qty_done == 0:
                        move_line.sudo().qty_done = (
                            move_line.quantity_product_uom or move_line.qty_done or move_line.move_id.product_uom_qty
                        )

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
                        f"Fecha despacho: {fecha_despacho}"
                    ),
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

    def _handle_mercaderia_preparada(self, data):
   
        pedidos = data.get('pedidos', [])
        if not pedidos:
            raise ValueError("El payload no contiene pedidos.")

        matricula        = data.get('matriculaCamion', '')
        descripcion_camion = data.get('descripcionCamion', '')
        fecha_facturacion  = data.get('fechaFacturacion', '')

        contenedores = data.get('contenedores', [])
        total_bultos = sum(c.get('cantidadBultos', 0) for c in contenedores)
        peso_total   = sum(c.get('pesoReal', 0.0)     for c in contenedores)
        precintos    = ', '.join(
            filter(None, [
                c.get('precinto1', '') for c in contenedores
            ] + [
                c.get('precinto2', '') for c in contenedores
            ])
        )

        errores = []
        fecha_preparacion = fields.Datetime.now()

        for pedido_data in pedidos:
            nombre_pedido  = pedido_data.get('pedido')
            codigo_origen  = pedido_data.get('codigoOrigen', '')
            memo           = pedido_data.get('memo', '')

            if not nombre_pedido:
                errores.append("Un pedido del array no tiene campo 'pedido'.")
                continue

            picking = request.env['stock.picking'].sudo().search(
                [('name', '=', nombre_pedido)], limit=1
            )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('codigo_unico', '=', nombre_pedido)], limit=1
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

            if not picking.picking_type_id.metodo_preparacion_wis:
                errores.append(
                    f"Picking '{picking.name}': tipo de operación "
                    f"'{picking.picking_type_id.name}' no tiene habilitada "
                    "la preparación por WMS."
                )
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

            if fecha_facturacion:
                try:
                    from datetime import datetime
                    fecha_preparacion = datetime.strptime(
                        fecha_facturacion, '%d/%m/%Y %H:%M'
                    )
                except ValueError:
                    _logger.warning(
                        "No se pudo parsear fechaFacturacion '%s', usando now()",
                        fecha_facturacion
                    )

            observaciones = (
                f"Camión: {matricula} ({descripcion_camion}) | "
                f"Origen WMS: {codigo_origen} | "
                f"Bultos: {total_bultos} | Peso: {peso_total} kg | "
                f"Precintos: {precintos or '—'} | "
                f"Memo: {memo or '—'}"
            )

            picking.with_context(skip_wms_integration=True).write({
                'wms_estado':                    'preparado',
                'wms_fecha_preparacion':         fecha_preparacion,
                'wms_observaciones_preparacion': observaciones,
                'wms_cantidad_bultos':           total_bultos,
                'wms_peso_total':                peso_total,
                'wms_transportadora':            matricula,
            })

            _logger.info(
                "Picking %s marcado como 'preparado' por WMS. "
                "Bultos: %s | Peso: %s kg",
                picking.name, total_bultos, peso_total,
            )

        if errores:
            raise ValueError(
                f"Se procesaron {len(pedidos) - len(errores)}/{len(pedidos)} pedidos. "
                f"Errores: {' | '.join(errores)}"
            )

    
    def _handle_pedidos_anulados(self, data):
        
        pedidos = data.get('pedidosAnulados', [])
        if not pedidos:
            raise ValueError("El payload no contiene pedidosAnulados.")

        errores = []

        for pedido_data in pedidos:
            nombre_pedido = pedido_data.get('pedido')

            if not nombre_pedido:
                errores.append("Un pedido del array no tiene campo 'pedido'.")
                continue

            detalles = pedido_data.get('detalles', [])
            motivo = next(
                (d.get('motivo', '') for d in detalles if d.get('motivo')),
                'Sin motivo informado'
            )
            fecha_alta_raw = next(
                (d.get('fechaAlta', '') for d in detalles if d.get('fechaAlta')),
                ''
            )

            from datetime import datetime
            fecha_anulacion = fields.Datetime.now()
            if fecha_alta_raw:
                try:
                    fecha_anulacion = datetime.strptime(fecha_alta_raw, '%d/%m/%Y %H:%M')
                except ValueError:
                    _logger.warning(
                        "No se pudo parsear fechaAlta '%s', usando now()", fecha_alta_raw
                    )

            picking = request.env['stock.picking'].sudo().search(
                [('name', '=', nombre_pedido)], limit=1
            )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('codigo_unico', '=', nombre_pedido)], limit=1
                )
            if not picking:
                picking = request.env['stock.picking'].sudo().search(
                    [('idPedidoWMS', '=', nombre_pedido)], limit=1
                )
            if not picking:
                errores.append(f"No se encontró picking para pedido '{nombre_pedido}'.")
                continue

            if picking.state == 'done':
                errores.append(
                    f"Picking '{picking.name}' ya fue validado (done), no se puede anular."
                )
                continue

            if picking.state == 'cancel' or picking.wms_estado == 'anulado':
                _logger.warning(
                    "Picking '%s' ya estaba cancelado/anulado, se omite.", picking.name
                )
                continue

            if picking.operation_type_id.metodo_cancelacion_wis is False:
                errores.append(
                    f"Picking '{picking.name}': tipo de operación "
                    f"'{picking.picking_type_id.name}' no permite cancelación por WMS."
                )
                continue

            try:
                picking.with_context(skip_wms_integration=True).write({
                    'wms_estado':          'anulado',
                    'wms_fecha_anulacion': fecha_anulacion,
                    'wms_motivo_anulacion': motivo,
                })

                picking.sudo().action_cancel()

                picking.sudo().message_post(
                    body=(
                        f"WMS — Pedido anulado<br/>"
                        f"Fecha de anulación: {fecha_anulacion}<br/>"
                        f"Motivo: {motivo}"
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
                        f"Webhook pedidosAnulados: picking {picking.name} "
                        f"cancelado por WMS."
                    ),
                    'picking_id': picking.id,
                    'resultado': 'exito',
                    'detalle': (
                        f"Motivo: {motivo} | "
                        f"Fecha anulación: {fecha_anulacion}"
                    ),
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
        pass

    def _handle_consulta_stock(self, data):
        pass

    def _handle_almacenamiento(self, data):
        pass

    def _handle_confirmacion_produccion(self, data):
        pass

    def _handle_test(self, data):
        pass
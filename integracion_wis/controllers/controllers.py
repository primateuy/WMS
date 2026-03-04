import json
import hmac
import hashlib
import logging
import time
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)

class WebhookWIS(http.Controller):

    @http.route('/webhook/wis/callback', type='json', auth='public', methods=['POST'], csrf=False)
    def callback(self, **kwargs):
        signature = request.httprequest.headers.get('X-Hub-Signature')
        body = request.httprequest.get_data()
        
            # if not self._verify_signature(body, signature):
            #     return {'status': 401, 'detail': 'Firma inválida'}

        try:
            payload = json.loads(body)
        except Exception as e:
            response = {'status': 400, 'detail': 'JSON inválido'}
            self._create_log(body.decode('utf-8') if body else '', response, 'desconocido', 'error')
            return response

        event_id = payload.get('id')
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
            self._create_log(payload, response, event_id or 'desconocido', 'error')
            return response

        try:
            handler_data = payload.get(event_id, {}) if event_id else payload
            handler(handler_data)
            response = {'status': 200}
            self._create_log(payload, response, event_id, 'exito')
            return response
        except Exception as e:
            _logger.exception("Error procesando evento WIS %s", event_id)
            response = {'status': 500, 'detail': str(e)}
            self._create_log(payload, response, event_id, 'error')
            return response

    def _verify_signature(self, body, received_signature):
        if not received_signature:
            return False
        
        secret = request.env['ir.config_parameter'].sudo().get_param('wis.webhook_secret', '')
        if not secret:
            _logger.warning("No está configurado wis.webhook_secret")
            return False

        expected = hmac.new(
            secret.encode('utf-8'),
            body,
            hashlib.sha512
        ).hexdigest()

        return hmac.compare_digest(expected, received_signature)

    def _create_log(self, data, respuesta, tipo, estado='exito'):
        try:
            # Manejar data como string o dict
            if isinstance(data, str):
                request_str = data
            else:
                request_str = json.dumps(data, indent=4, ensure_ascii=False)
                
            # Formatear respuesta como JSON
            if isinstance(respuesta, dict):
                respuesta_str = json.dumps(respuesta, indent=4, ensure_ascii=False)
            else:
                respuesta_str = json.dumps({'message': str(respuesta)}, indent=4, ensure_ascii=False)

            request.env['wis.webhook.log'].sudo().create({
                'fecha': fields.Date.today(),
                'hora': time.strftime('%H:%M:%S'),
                'request': request_str, 
                'respuesta': respuesta_str,
                'tipo': tipo,
                'estado': estado
            })
        except Exception as e:
            _logger.exception("Error guardando log de webhook WIS: %s", str(e))

    def _handle_confirmacion_recepcion(self, data):
        # agenda      = data.get('agenda')
        # empresa     = data.get('empresa')
        # tipo_agente = data.get('tipoAgente')
        # cod_agente  = data.get('codigoAgente')
        # referencias = data.get('referencias', [])
        # detalles    = data.get('detalles', [])
        # _logger.info("Confirmación recepción - Agenda: %s Empresa: %s", agenda, empresa)
        # for ref in referencias:
        #     if ref.get('tipoReferencia') == 'OC':
        #         purchase = request.env['purchase.order'].sudo().search([
        #             ('name', '=', ref.get('numeroReferencia'))
        #         ], limit=1)
        #         if purchase:
        #             _logger.info("Orden de compra encontrada: %s", purchase.name)
        pass

    def _handle_confirmacion_pedido(self, data):
        # camion  = data.get('camion')
        # pedidos = data.get('pedidos', [])
        # for pedido in pedidos:
        #     sale_order_name = pedido.get('pedido')
        #     detalles        = pedido.get('detalles', [])
        #     sale = request.env['sale.order'].sudo().search([
        #         ('name', '=', sale_order_name)
        #     ], limit=1)
        #     if sale:
        #         _logger.info("Confirmando pedido: %s", sale_order_name)
        pass

    def _handle_mercaderia_preparada(self, data):
        # _logger.info("Mercadería preparada recibida")
        pass

    def _handle_pedidos_anulados(self, data):
        # pedidos = data.get('pedidosAnulados', [])
        # for pedido in pedidos:
        #     sale_order_name = pedido.get('pedido')
        #     sale = request.env['sale.order'].sudo().search([
        #         ('name', '=', sale_order_name)
        #     ], limit=1)
        #     if sale and sale.state not in ('done', 'cancel'):
        #         sale.sudo().action_cancel()
        #         _logger.info("Pedido anulado: %s", sale_order_name)
        pass

    def _handle_ajustes(self, data):
        # ajustes = data.get('ajustes', [])
        # for ajuste in ajustes:
        #     producto = ajuste.get('producto')
        #     cantidad = ajuste.get('cantidadMovimiento', 0)
        #     _logger.info("Ajuste stock - Producto: %s Cantidad: %s", producto, cantidad)
        pass

    def _handle_consulta_stock(self, data):
        # for item in data.get('stock', []):
        #     _logger.info("Stock WIS - Producto: %s Disponible: %s", 
        #         item.get('producto'), item.get('stockDisponible'))
        pass

    def _handle_almacenamiento(self, data):
        # _logger.info("Almacenamiento recibido - Agenda: %s", data.get('agenda'))
        pass

    def _handle_confirmacion_produccion(self, data):
        # _logger.info("Confirmación producción - Externo: %s", data.get('idProduccionExterno'))
        pass

    def _handle_test(self, data):
        # _logger.info("Ping de test recibido desde WIS")
        pass
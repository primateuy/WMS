# -*- coding: utf-8 -*-
from odoo import http

from odoo.http import request
import hmac
import hashlib
import json

import logging
_logger = logging.getLogger(__name__)

class WisWebHookController(http.Controller):

    @http.route('/wis/webhook', type='json', auth='public', methods=['POST'], csrf=False)
    def WisWebHookHandler(self, **post):
        raw_data = request.httprequest.data
        signature_header = request.httprequest.headers.get('X-Hub-Signature', '')
        if not signature_header:
            return {'error': 'Falta la firma del header'}
        claveSecreta = request.env['ir.config_parameter'].sudo().get_param('integracion_wis.integracion_wis_webhook.claveSecreta')
        if not claveSecreta:
            return {'error': 'No clave secreta'}

        expected_signature = hmac.new(
            shared_secret.encode('utf-8'),
            raw_data,
            hashlib.sha512
        ).hexdigest()

        if signature_header != expected_signature:
            return {'error': 'ocurrio un error'}

        # Parsear el JSON
        try:
            data = json.loads(raw_data)
        except Exception as e:
            return {'error': 'formato invalido'}

        event_id = data.get('id')

        # confirmacionRecepcion, confirmacionPedido, pedidosAnulados, ajustes, confirmacionMercaderiaPreparada, almacenamiento, test, confirmacionProduccion, 
        
        if event_id == 'confirmacionMercaderiaPreparada':
            request.env['wms.event.processor'].confirmacionMercaderiaPreparada(data);

        if event_id == 'pedidosAnulados':
            request.env['wms.event.processor'].pedidosAnulados(data);


        if event_id == 'confirmacionRecepcion':
            request.env['wms.event.processor'].confirmacionRecepcion(data);

        if event_id == 'ajustes':
            request.env['wms.event.processor'].ajustes(data);
            



        return {'status': 'ok'}

   
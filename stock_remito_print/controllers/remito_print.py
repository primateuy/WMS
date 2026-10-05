# -*- coding: utf-8 -*-
import base64
import json
import logging
from collections import OrderedDict

from odoo import _, http
from odoo.exceptions import AccessError
from odoo.http import content_disposition, request
from odoo.tools.pdf import merge_pdf

_logger = logging.getLogger(__name__)


class RemitoPrintController(http.Controller):
    """Genera el PDF de un lote de remitos.

    La acción cliente llama a este endpoint una vez por lote y espera la
    respuesta antes de pedir el siguiente, así nunca hay más de un lote en
    vuelo por usuario.
    """

    @http.route(
        '/stock_remito_print/batch',
        type='http',
        auth='user',
        methods=['POST'],
        csrf=True,
    )
    def print_batch(self, **post):
        try:
            picking_ids = self._parse_ids(post.get('picking_ids'))
        except ValueError:
            return self._json_response(
                {'error': _('La lista de operaciones del lote es inválida.')},
                status=400)
        if not picking_ids:
            return self._json_response(
                {'error': _('El lote llegó vacío.')}, status=400)

        marcar = str(post.get('mark_printed', '0')) in ('1', 'true', 'True')
        indice = post.get('batch_index') or '1'
        fecha = post.get('batch_date') or ''

        Picking = request.env['stock.picking']
        try:
            Picking.check_access_rights('read')
        except AccessError:
            return self._json_response(
                {'error': _('No tenés permiso para leer operaciones de '
                            'inventario.')},
                status=403)

        # search() aplica las reglas de registro: lo que no vuelve acá es lo
        # que el usuario no puede ver.
        accesibles = set(Picking.search([('id', 'in', picking_ids)]).ids)

        resultados = []
        contenidos = []
        # Se agrupa por origen para escribir el marcado de una sola vez por
        # grupo en vez de una por operación.
        por_origen = OrderedDict()

        for picking_id in picking_ids:
            if picking_id not in accesibles:
                resultados.append({
                    'id': picking_id,
                    'name': '',
                    'source': 'error',
                    'error': _('No tenés permiso para ver esta operación.'),
                })
                continue
            picking = Picking.browse(picking_id)
            try:
                contenido, origen, error = picking._remito_get_pdf()
            except Exception as excepcion:
                # Una operación que falla no puede tumbar el lote entero.
                _logger.exception(
                    'Error inesperado al resolver el PDF de la operación %s',
                    picking_id)
                resultados.append({
                    'id': picking_id,
                    'name': picking.name or '',
                    'source': 'error',
                    'error': str(excepcion),
                })
                continue

            if not contenido:
                resultados.append({
                    'id': picking_id,
                    'name': picking.name or '',
                    'source': 'error',
                    'error': error or _('No se pudo generar el PDF.'),
                })
                continue

            contenidos.append(contenido)
            resultados.append({
                'id': picking_id,
                'name': picking.name or '',
                'source': origen,
                'error': False,
            })
            por_origen.setdefault(origen, []).append(picking_id)

        if not contenidos:
            return self._json_response({
                'results': resultados,
                'error': _('Ninguna operación de este lote pudo generar un '
                           'PDF.'),
            })

        try:
            pdf = merge_pdf(contenidos)
        except Exception as excepcion:
            _logger.exception('Error al unir los PDF del lote')
            return self._json_response({
                'results': resultados,
                'error': _('No se pudieron unir los PDF del lote: %s')
                         % excepcion,
            })

        # Se marcan solo las operaciones que efectivamente quedaron dentro
        # del PDF que se devuelve.
        if marcar:
            for origen, ids_origen in por_origen.items():
                Picking.browse(ids_origen)._remito_register_print(origen)

        return self._pdf_response(pdf, resultados, indice, fecha)

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def _parse_ids(self, crudo):
        """Convierte el parámetro recibido en una lista de ids enteros."""
        if not crudo:
            return []
        valores = json.loads(crudo)
        if not isinstance(valores, list):
            raise ValueError('picking_ids debe ser una lista')
        return [int(valor) for valor in valores]

    def _json_response(self, payload, status=200):
        return request.make_response(
            json.dumps(payload),
            headers=[('Content-Type', 'application/json')],
            status=status,
        )

    def _pdf_response(self, pdf, resultados, indice, fecha):
        """Devuelve el PDF del lote y el detalle por operación.

        El detalle viaja en una cabecera, codificado en base64: las cabeceras
        HTTP son latin-1 y los mensajes de error van en español.
        """
        payload = json.dumps({'results': resultados})
        codificado = base64.b64encode(payload.encode('utf-8')).decode('ascii')
        nombre = 'Remitos_%s_lote_%s.pdf' % (fecha or 'sin_fecha', indice)
        return request.make_response(
            pdf,
            headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Length', len(pdf)),
                ('Content-Disposition', content_disposition(nombre)),
                ('X-Remito-Result', codificado),
                ('X-Remito-Result-Encoding', 'base64'),
            ],
        )

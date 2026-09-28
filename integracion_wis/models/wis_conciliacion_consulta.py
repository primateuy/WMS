# -*- coding: utf-8 -*-
"""Cada request que la conciliación de stock le hace a WIS, con lo que contestó.

Existe porque «no se ve qué le mandamos a WIS»: durante horas la conciliación
de producción estuvo repitiendo la misma tanda y en pantalla no había nada que
lo mostrara. Acá queda el body enviado, el status, cuánto tardó y cuántas de
las filas recibidas eran variantes nuestras.
"""
import json

from odoo import models, fields, _


class WisConciliacionConsulta(models.Model):
    _name = 'wis.conciliacion.consulta'
    _description = 'Request de la conciliación de stock a WIS'
    _order = 'id desc'

    conciliacion_id = fields.Many2one(
        'conciliacion.stock', string='Conciliación', required=True,
        ondelete='cascade', index=True)
    fecha = fields.Datetime(string='Fecha', default=fields.Datetime.now, readonly=True)
    etapa = fields.Selection(
        [('sondeo', 'Medición del listado'),
         ('listado', 'Listado de stock'),
         ('por_codigo', 'Por código')],
        string='Etapa', readonly=True)
    endpoint = fields.Char(string='Endpoint', readonly=True)
    pagina = fields.Integer(string='Página', readonly=True)
    producto = fields.Char(string='Código', readonly=True)
    body = fields.Text(string='Enviado', readonly=True)
    http_status = fields.Integer(string='HTTP', readonly=True)
    duracion_ms = fields.Integer(string='Duración (ms)', readonly=True)
    filas = fields.Integer(string='Filas recibidas', readonly=True)
    nuestras = fields.Integer(
        string='Nuestras', readonly=True,
        help="Cuántas de las filas recibidas son variantes de la tabla de trabajo.")
    resultado = fields.Selection(
        [('ok', 'Con stock'),
         ('fin', 'Sin stock / fin del listado'),
         ('no_existe', 'No existe en WIS'),
         ('error', 'Error')],
        string='Resultado', readonly=True)
    mensaje = fields.Text(string='Mensaje', readonly=True)
    respuesta = fields.Text(string='Respuesta', readonly=True)

    def _desde_traza(self, conciliacion, etapa, traza, nuestras=0):
        """Registra un request a partir de la traza de `_consulta_stock_request`."""
        return self.create({
            'conciliacion_id': conciliacion.id,
            'etapa': etapa,
            'endpoint': traza.get('endpoint'),
            'pagina': traza.get('pagina') or 0,
            'producto': traza.get('producto') or False,
            'body': json.dumps(traza.get('body') or {}, ensure_ascii=False),
            'http_status': traza.get('status') or 0,
            'duracion_ms': traza.get('ms') or 0,
            'filas': len(traza.get('filas') or []),
            'nuestras': nuestras,
            'resultado': traza.get('clase') or 'error',
            'mensaje': traza.get('mensaje') or False,
            'respuesta': traza.get('respuesta') or False,
        })

    def _compute_display_name(self):
        for r in self:
            r.display_name = _("Request #%s") % r.id

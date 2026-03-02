from odoo import models, fields, api
import logging
import requests

_logger = logging.getLogger(__name__)


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    integracion_wms = fields.Boolean(
        string='Integración con WMS',
        help='Habilita la integración con el sistema WMS para este tipo de entrega.',
        default=False
    );

    

    tipo_pedido_wms = fields.Selection(
        selection=[
            ('AL', 'AL'),
            ('EC', 'EC'),
            ('OD', 'OD'),
            ('OC', 'OC')
        ])

    metodo_creacion_wms = fields.Selection(
        selection=[
            ('creacion', 'Creación'),
            ('creacion_actualizacion', 'Creación/Actualización'),
        ])


    estado_disparo_wms = fields.Selection([
        ('draft', 'Borrador'),
        ('waiting', 'Esperando'),
        ('confirmed', 'Confirmado'),
        ('assigned', 'Listo'),
        ('done', 'Hecho'),
        ('cancel', 'Cancelado')
    ], string='Estado para Disparo WMS', default='confirmed')


    metodo_cancelacion_wms = fields.Boolean(
        string='Método de Cancelación WMS',
        default=False,
        help='Habilita el método de cancelación WMS para este tipo de entrega.'

    )

    metodo_preparacion_wms = fields.Boolean(
        string='Método de Preparación WMS',
        default=False,
        help='Habilita el método de preparación WMS para este tipo de entrega.')


    emitir_factura_antes_envio = fields.Boolean(
        string='Emitir Factura Antes de Envío',
        help='Si está marcado, se emitirá la factura antes de enviar el pedido al WMS. Y la misma se adjuntara como documento hacia WIS.',
        default=False
        )
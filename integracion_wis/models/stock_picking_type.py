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

    

    tipo_pedido_wis = fields.Char(
        string='Tipo de Pedido WIS',
        help='Tipo de pedido WIS para este tipo de entrega.',
    )

    metodo_creacion_wis = fields.Selection(
        selection=[
            ('creacion', 'Creación'),
            ('creacion_actualizacion', 'Creación/Actualización'),
        ])


    estado_disparo_wis = fields.Selection([
        ('draft', 'Borrador'),
        ('waiting', 'Esperando'),
        ('confirmed', 'Confirmado'),
        ('assigned', 'Listo'),
        ('done', 'Hecho'),
        ('cancel', 'Cancelado')
    ], string='Estado para Disparo WIS', default='confirmed')


    metodo_cancelacion_wis = fields.Boolean(
        string='Método de Cancelación WIS',
        default=False,
        help='Habilita el método de cancelación WIS para este tipo de entrega.'

    )

    metodo_preparacion_wis = fields.Boolean(
        string='Método de Preparación WIS',
        default=False,
        help='Habilita el método de preparación WIS para este tipo de entrega.')


    emitir_factura_antes_envio = fields.Boolean(
        string='Emitir Factura Antes de Envío',
        help='Si está marcado, se emitirá la factura antes de enviar el pedido al WMS. Y la misma se adjuntara como documento hacia WIS.',
        default=False
        )
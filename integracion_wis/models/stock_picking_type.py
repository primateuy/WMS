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

    tipo_expedicion_wis = fields.Char(
        string='Tipo de Expedición WIS',
        help='Tipo de expedición que se envía a WIS en el pedido (campo "tipoExpedicion"). '
             'Si se deja vacío, se usa el valor por defecto: "WSF" para pedidos NORM y "WIS" para el resto.',
    )

    tipo_agente_wis = fields.Selection(
        selection=[
            ('CLI', 'Cliente (CLI)'),
            ('PRO', 'Proveedor (PRO)'),
        ],
        string='Tipo de Agente WIS',
        help='Define si las operaciones de este tipo usan el agente Cliente (CLI) o Proveedor (PRO) del contacto.',
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

    integra_parciales = fields.Boolean(
        string='Integrar parciales como nuevas operaciones',
        default=False,
        help='Si está activo, los backorders de este tipo se envían a WIS como operaciones nuevas. '
             'Si está inactivo (por defecto), el backorder hereda el código WIS del picking original '
             'y WIS sigue la operación con su saldo remanente.'
    )

    adquiere_codigo_unico_wms = fields.Boolean(
        string='Adquiere código único WMS',
        default=False,
        help='Si está activo, los pickings de este tipo NO generan su propio código WIS: '
             'adquieren (heredan) el código de la operación anterior en la cadena y quedan como '
             'operación interna (wms_estado="no_integrado", sin comunicarse con WIS). '
             'Ej.: la transferencia interna de crossdocking toma el código del paso entrada→salida.'
    )

    conciliar_al_anular = fields.Boolean(
        string='Conciliar stock al anular (WMS)',
        default=False,
        help='Si está activo, cuando WIS anula un pedido de este tipo (webhook pedidosAnulados) '
             'se dispara una conciliación de stock acotada a los productos anulados, '
             'inmediatamente después de cancelar el picking. Activar en tipos donde una anulación '
             'puede generar diferencias reales de stock (abastecimientos a tiendas, entregas '
             'mayoristas). NO activar en recepciones.'
    )

from odoo import api, models, fields
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class WizardStock(models.TransientModel):
    _name = 'integracion_wis.wizard_stock'
    _description = 'Actualizar Stock para coincidir con WIS'

    product_id = fields.Many2one('product.product', string='Producto', required=True)

    # Campos para mostrar la respuesta
    stock_disponible = fields.Float(string='Stock Disponible en WMS', readonly=True)
    resultado_consulta = fields.Text(string='Resultado', readonly=True)

# -*- coding: utf-8 -*-
from odoo import fields, models


class UomWis(models.Model):
    _inherit = 'uom.uom'

    wis_code = fields.Char(
        string='Código WIS',
        help='Código de la unidad de medida tal como lo espera la API de WIS (ej: UN, KG, L)',
    )

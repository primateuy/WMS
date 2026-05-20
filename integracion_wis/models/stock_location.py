# -*- coding: utf-8 -*-
from odoo import models, fields


class StockLocation(models.Model):
    _inherit = 'stock.location'

    wis_no_requiere_eremito = fields.Boolean(
        string="No requiere e-Remito",
        default=False,
        help="Si está activo, los pickings cuyo destino sea esta ubicación NO exigirán "
             "Document Type / e-Remito al validarse. Usar para movimientos internos sin "
             "traslado externo (ej.: Polo Entrada → Existencia).",
    )

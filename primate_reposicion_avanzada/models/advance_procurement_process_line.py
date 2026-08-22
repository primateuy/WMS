# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class AdvanceProcurementProcessLine(models.Model):
    _inherit = 'advance.procurement.process.line'

    multiplo_distribucion = fields.Float(
        string='Múltiplo',
        readonly=True,
        copy=False,
        help='Copia del múltiplo de distribución del producto al momento del cálculo, '
             'para poder auditar con qué valor se ajustó la cantidad.',
    )
    qty_adjusted = fields.Float(
        string='Cant. Ajustada',
        readonly=True,
        copy=False,
        digits='Product Unit of Measure',
        help='Demanda calculada por el motor de Setu, ajustada al múltiplo de distribución '
             'según la dirección de redondeo elegida en el proceso.',
    )
    qty_available_fulfiller = fields.Float(
        string='Stock Origen',
        readonly=True,
        copy=False,
        digits='Product Unit of Measure',
        help='Stock del producto en el almacén origen disponible para repartir, ya '
             'descontada la demanda propia del origen.',
    )
    qty_distributable = fields.Float(
        string='Cant. Distribuible',
        copy=False,
        digits='Product Unit of Measure',
        help='Cantidad que se va a transferir. Editable solo en líneas parciales.',
    )
    execution_status = fields.Selection(
        [('executable', 'Ejecutable'),
         ('partial', 'A definir criterio'),
         ('impossible', 'Sin ejecución')],
        string='Estado',
        readonly=True,
        copy=False,
        help='Ejecutable: el origen cubre toda la demanda del producto, la línea se cumple '
             'al 100%.\n'
             'A definir criterio: el origen no cubre la demanda total del producto, así que '
             'ninguna de sus líneas se cumple sola. El usuario decide el reparto.\n'
             'Sin ejecución: no hay stock en origen ni para un múltiplo.',
    )
    qty_shortfall = fields.Float(
        string='Faltante',
        compute='_compute_qty_shortfall',
        store=True,
        digits='Product Unit of Measure',
        help='Cuánto le falta a esta línea para cubrir su demanda ajustada. '
             'Se almacena para poder filtrar y agrupar por él.',
    )

    @api.depends('qty_adjusted', 'qty_distributable')
    def _compute_qty_shortfall(self):
        for line in self:
            line.qty_shortfall = max(line.qty_adjusted - line.qty_distributable, 0.0)
    warehouse_group_mismatch = fields.Boolean(
        string='Fuera de Grupo',
        readonly=True,
        copy=False,
        help='El almacén destino no pertenece al grupo de almacenes del producto. '
             'La línea se excluye del procesamiento.',
    )

    @api.constrains('qty_distributable')
    def _check_qty_distributable_multiple(self):
        """La cantidad ingresada a mano también tiene que respetar el múltiplo.

        Sin esto, una edición manual volvería a dejar la transferencia en una cantidad que
        stock_multiplos_validation rechaza al validar el picking, que es justamente lo que
        este módulo evita.
        """
        for line in self:
            if not line.product_id or line.qty_distributable <= 0:
                continue
            if not line.product_id.primate_is_valid_multiple(line.qty_distributable):
                raise ValidationError(_(
                    'La cantidad %(qty)s de %(product)s no es múltiplo de %(multiple)s. '
                    'Ajustá el valor a un múltiplo válido.',
                    qty=line.qty_distributable,
                    product=line.product_id.display_name,
                    multiple=line.product_id.mutiplos_distribucion,
                ))

    @api.constrains('qty_distributable', 'execution_status')
    def _check_qty_distributable_editable(self):
        """Solo las líneas parciales admiten una cantidad distinta de la calculada.

        Las ejecutables ya están en su cantidad correcta y las imposibles en cero: abrir la
        edición ahí no aporta y desalinea la cantidad con el estado.
        """
        for line in self:
            if line.execution_status == 'executable' and \
                    float(line.qty_distributable) != float(line.qty_adjusted):
                raise ValidationError(_(
                    'La línea de %(product)s en %(warehouse)s es ejecutable: su cantidad no '
                    'se edita, ya está en la cantidad calculada (%(qty)s).',
                    product=line.product_id.display_name,
                    warehouse=line.warehouse_id.display_name,
                    qty=line.qty_adjusted,
                ))
            if line.execution_status == 'impossible' and line.qty_distributable:
                raise ValidationError(_(
                    'La línea de %(product)s en %(warehouse)s no tiene stock suficiente en '
                    'origen para armar ni un múltiplo: su cantidad queda en cero.',
                    product=line.product_id.display_name,
                    warehouse=line.warehouse_id.display_name,
                ))

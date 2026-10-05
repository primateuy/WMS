# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

PARAM_BATCH_SIZE = 'stock_remito_print.batch_size'
PARAM_MAX_RECORDS = 'stock_remito_print.max_records'

DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_RECORDS = 500


class RemitoPrintWizard(models.TransientModel):
    _name = 'stock.remito.print.wizard'
    _description = 'Impresión masiva de remitos'

    mode = fields.Selection(
        selection=[
            ('print', 'Imprimir'),
            ('download', 'Descargar'),
        ],
        string='Modo',
        default='print',
        required=True,
    )
    mark_printed = fields.Boolean(
        string='Marcar como impresas',
        default=True,
        help='Marca cada operación como impresa a medida que su PDF se '
             'incluye en el lote.',
    )
    batch_size = fields.Integer(
        string='Tamaño de lote',
        required=True,
        default=lambda self: self._default_batch_size(),
        help='Cantidad de operaciones que se procesan por cada pedido al '
             'servidor. Un lote más chico tarda más pero carga menos el '
             'servidor.',
    )
    picking_ids = fields.Many2many(
        comodel_name='stock.picking',
        string='Operaciones',
    )
    picking_count = fields.Integer(
        string='Operaciones seleccionadas',
        compute='_compute_picking_count',
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Parámetros de sistema
    # ------------------------------------------------------------------

    @api.model
    def _get_int_param(self, clave, defecto):
        valor = self.env['ir.config_parameter'].sudo().get_param(clave, defecto)
        try:
            numero = int(valor)
        except (TypeError, ValueError):
            numero = defecto
        return numero if numero > 0 else defecto

    @api.model
    def _default_batch_size(self):
        return self._get_int_param(PARAM_BATCH_SIZE, DEFAULT_BATCH_SIZE)

    @api.model
    def _get_max_records(self):
        return self._get_int_param(PARAM_MAX_RECORDS, DEFAULT_MAX_RECORDS)

    # ------------------------------------------------------------------
    # Valores por defecto y cálculos
    # ------------------------------------------------------------------

    @api.model
    def default_get(self, fields_list):
        valores = super().default_get(fields_list)
        contexto = self.env.context
        if contexto.get('active_model') == 'stock.picking':
            ids = contexto.get('active_ids') or []
            if not ids and contexto.get('active_id'):
                ids = [contexto['active_id']]
            if ids:
                valores['picking_ids'] = [fields.Command.set(list(ids))]
        return valores

    @api.depends('picking_ids')
    def _compute_picking_count(self):
        for wizard in self:
            wizard.picking_count = len(wizard.picking_ids)

    @api.constrains('batch_size')
    def _check_batch_size(self):
        for wizard in self:
            if wizard.batch_size <= 0:
                raise ValidationError(
                    _('El tamaño de lote tiene que ser mayor que cero.'))

    # ------------------------------------------------------------------
    # Acción principal
    # ------------------------------------------------------------------

    def action_print(self):
        """Lanza la acción cliente que procesa las operaciones por lotes."""
        self.ensure_one()
        pickings = self.picking_ids
        if not pickings:
            raise UserError(
                _('No hay ninguna operación seleccionada para imprimir.'))

        maximo = self._get_max_records()
        if len(pickings) > maximo:
            raise UserError(
                _('Seleccionaste %(cantidad)s operaciones y el máximo por '
                  'ejecución es %(maximo)s. Afiná el filtro de la lista o '
                  'imprimí en varias tandas. El tope se configura en el '
                  'parámetro de sistema «%(parametro)s».')
                % {
                    'cantidad': len(pickings),
                    'maximo': maximo,
                    'parametro': PARAM_MAX_RECORDS,
                })

        return {
            'type': 'ir.actions.client',
            'tag': 'stock_remito_print.batch',
            'target': 'new',
            'name': _('Impresión de remitos'),
            'params': {
                'picking_ids': pickings.ids,
                'mode': self.mode,
                'mark_printed': self.mark_printed,
                'batch_size': self.batch_size,
            },
        }

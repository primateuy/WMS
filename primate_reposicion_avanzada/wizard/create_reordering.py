# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class CreateReordering(models.TransientModel):
    _inherit = 'create.reordering'

    warehouse_group_ids = fields.Many2many(
        'stock.warehouse.group',
        'primate_create_reordering_wh_group_rel',
        'wizard_id',
        'warehouse_group_id',
        string='Grupos de Almacenes',
        help='Opcional. Si se completa, la carga por categoría se limita a los productos '
             'que tengan alguno de estos grupos asignado.',
    )

    def action_load_products_by_category_and_group(self):
        """Expande categorías (y opcionalmente grupos) a product_ids.

        El campo product_category_ids ya venía declarado en el wizard de Setu pero sin
        efecto sobre product_ids: solo se usaba para filtrar el histórico de ventas. Como
        acá todo termina en product_ids, create_reorder_rule y prepare_orderpoint_domain lo
        recogen sin necesidad de tocarlos.

        Se filtra por productos almacenables, los únicos para los que Setu genera reglas.
        """
        self.ensure_one()
        if not self.product_category_ids and not self.warehouse_group_ids:
            raise UserError(_('Elegí al menos una categoría o un grupo de almacenes.'))

        candidatos = self.env['product.product'].primate_products_from_categories_and_groups(
            self.product_category_ids,
            self.warehouse_group_ids,
            extra_domain=[('type', '=', 'product')],
        )
        nuevos = candidatos - self.product_ids
        if nuevos:
            self.product_ids = [(4, product.id) for product in nuevos]

        # El wizard es un formulario modal: se reabre para que el usuario vea el resultado
        # y siga configurando la operación.
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'create.reordering',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'views': [(self.env.ref('setu_advance_reordering.form_create_reordering').id, 'form')],
        }

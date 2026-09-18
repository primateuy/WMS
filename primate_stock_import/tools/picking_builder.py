# -*- coding: utf-8 -*-
"""Creación de stock.picking / stock.move a partir del resultado validado."""
import logging

from odoo import _

_logger = logging.getLogger(__name__)


class PickingBuilder:
    """Crea las operaciones en borrador a partir de los grupos validados."""

    def __init__(self, env):
        self.env = env

    def build(self, groups, session):
        """Crea un picking por grupo con sus movimientos, en la compañía del tipo.

        Los grupos se agrupan por compañía y se crean en lote con un solo
        ``create`` por compañía, para no disparar computes fila a fila.

        Args:
            groups (list[dict]): clave ``groups`` del resultado del validador.
            session (recordset): sesión de importación a vincular.

        Returns:
            recordset: los ``stock.picking`` creados, en el orden del archivo.
        """
        pickings = self.env['stock.picking']
        by_company = {}
        for group in groups:
            by_company.setdefault(group['company_id'], []).append(group)
        for company_id, company_groups in by_company.items():
            vals_list = [self._prepare_picking_vals(group, session) for group in company_groups]
            created = self.env['stock.picking'].with_company(company_id).create(vals_list)
            pickings |= created
            _logger.info(
                "Sesión %s: creadas %s operaciones en compañía %s",
                session.name, len(created), company_id,
            )
        return pickings

    def _prepare_picking_vals(self, group, session):
        """Arma los valores del picking y sus movimientos (comandos One2many)."""
        products = self.env['product.product'].browse(
            [line['product_id'] for line in group['lines']]
        )
        product_names = {product.id: product.display_name for product in products}
        move_commands = []
        for line in group['lines']:
            move_vals = {
                'name': product_names[line['product_id']],
                'product_id': line['product_id'],
                'product_uom_qty': line['quantity'],
                'product_uom': line['product_uom_id'],
                'location_id': group['location_id'],
                'location_dest_id': group['location_dest_id'],
                'picking_type_id': group['picking_type_id'],
                'company_id': group['company_id'],
            }
            if group['scheduled_date']:
                move_vals['date'] = group['scheduled_date']
            if line['lot_name']:
                # El lote no se asigna en esta versión: se deja visible en la
                # descripción para quien complete las operaciones detalladas.
                move_vals['description_picking'] = _("Lote: %s", line['lot_name'])
            move_commands.append((0, 0, move_vals))
        vals = {
            'picking_type_id': group['picking_type_id'],
            'company_id': group['company_id'],
            'partner_id': group['partner_id'],
            'location_id': group['location_id'],
            'location_dest_id': group['location_dest_id'],
            'origin': group['origin'] or group['operation_ref'],
            'import_session_id': session.id,
            'move_ids_without_package': move_commands,
        }
        if group['scheduled_date']:
            vals['scheduled_date'] = group['scheduled_date']
        return vals

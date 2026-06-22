# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import models


class Move(models.Model):
    """Stock Move"""
    _inherit = 'stock.move'

    def sh_unreseve_qty(self):
        """Unreserve quantity"""
        for move_line in self.sudo().mapped('move_line_ids'):
            if move_line.state not in ['draft', 'cancel', 'assigned', 'waiting']:
                # unreserve qty
                quant = self.env['stock.quant'].sudo().search([
                    ('location_id', '=', move_line.location_id.id),
                    ('product_id', '=', move_line.product_id.id),
                    ('lot_id', '=', move_line.lot_id.id)
                ], limit=1)

                if quant:
                    quant.write(
                        {'quantity': quant.quantity + move_line.quantity})

                quant = self.env['stock.quant'].sudo().search([
                    ('location_id', '=', move_line.location_dest_id.id),
                    ('product_id', '=', move_line.product_id.id),
                    ('lot_id', '=', move_line.lot_id.id)
                ], limit=1)

                if quant:
                    quant.write(
                        {'quantity': quant.quantity - move_line.quantity})

    def action_move_cancel(self):
        """Cancel move"""
        for rec in self:
            rec.sh_unreseve_qty()
            rec.sudo().write({'state': 'cancel'})
            rec.mapped('move_line_ids').sudo().write({'state': 'cancel'})

    def action_move_cancel_draft(self):
        """Cancel move and set to draft"""
        for rec in self:
            rec.sh_unreseve_qty()
            rec.sudo().write({'state': 'draft'})
            rec.mapped('move_line_ids').sudo().write({'state': 'draft'})

    def action_move_cancel_delete(self):
        """Cancel move and delete"""
        for rec in self:
            rec.sh_unreseve_qty()
            rec.sudo().write({'state': 'draft'})
            rec.mapped('move_line_ids').sudo().write({'state': 'draft'})
            rec.mapped('move_line_ids').sudo().unlink()
            rec.sudo().unlink()

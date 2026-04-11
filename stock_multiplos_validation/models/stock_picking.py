# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    tiene_multiplos_incumplidos = fields.Boolean(
        string='Tiene múltiplos incumplidos',
        compute='_compute_tiene_multiplos_incumplidos',
        store=True,
        help='Indica que al menos una línea no respeta el múltiplo de distribución.',
    )

    @api.depends('move_ids.multiplo_incumplido')
    def _compute_tiene_multiplos_incumplidos(self):
        for picking in self:
            picking.tiene_multiplos_incumplidos = any(
                picking.move_ids.filtered(
                    lambda m: m.state not in ('done', 'cancel') and m.multiplo_incumplido
                )
            )

    def _moves_con_multiplo_incumplido(self):
        """Devuelve los moves activos que no respetan su múltiplo."""
        if not self.picking_type_id.respeta_multiplos:
            return self.env['stock.move']

        resultado = self.env['stock.move']
        for move in self.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            multiplo = getattr(move.product_id, 'mutiplos_distribucion', 1) or 1
            if multiplo <= 1:
                continue
            cantidad = move.quantity
            if cantidad <= 0:
                continue
            if cantidad % multiplo != 0:
                resultado |= move
        return resultado

    def _actualizar_flags_multiplos(self):
        """Marca/desmarca multiplo_incumplido en cada move del picking."""
        if not self.picking_type_id.respeta_multiplos:
            self.move_ids.write({'multiplo_incumplido': False})
            return

        moves_con_problema = self._moves_con_multiplo_incumplido()
        moves_ok = self.move_ids - moves_con_problema

        if moves_con_problema:
            moves_con_problema.write({'multiplo_incumplido': True})
        if moves_ok:
            moves_ok.write({'multiplo_incumplido': False})

    def _mensaje_multiplos_incumplidos(self, moves):
        """Genera el texto descriptivo de los moves problemáticos."""
        lineas = []
        for move in moves:
            multiplo = move.product_id.mutiplos_distribucion or 1
            cantidad = move.quantity
            proximo = int(cantidad // multiplo + 1) * multiplo
            lineas.append(
                f"• {move.product_id.display_name}: "
                f"cantidad {cantidad:.0f} no es múltiplo de {multiplo} "
                f"(próximo válido: {proximo:.0f})"
            )
        return "\n".join(lineas)

    # -------------------------------------------------------------------------
    # Marcar como por realizar (action_assign)
    # -------------------------------------------------------------------------
    def action_assign(self):
        res = super().action_assign()
        for picking in self:
            picking._actualizar_flags_multiplos()
            moves_problema = picking._moves_con_multiplo_incumplido()
            if moves_problema:
                detalle = picking._mensaje_multiplos_incumplidos(moves_problema)
                raise UserError(
                    "Advertencia: las siguientes líneas no respetan el múltiplo "
                    "de distribución. Podés continuar corrigiendo las cantidades "
                    "antes de validar:\n\n" + detalle
                )
        return res

    # -------------------------------------------------------------------------
    # Creación automática: marcar flags sin interrumpir
    # -------------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        pickings = super().create(vals_list)
        for picking in pickings:
            picking._actualizar_flags_multiplos()
        return pickings

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in ('move_ids', 'state')):
            for picking in self:
                picking._actualizar_flags_multiplos()
        return res

    # -------------------------------------------------------------------------
    # Validación final (button_validate)
    # -------------------------------------------------------------------------
    def button_validate(self):
        for picking in self:
            moves_problema = picking._moves_con_multiplo_incumplido()
            if not moves_problema:
                continue
            if self.env.user.has_group(
                'stock_multiplos_validation.group_bypass_multiplos'
            ):
                continue
            detalle = picking._mensaje_multiplos_incumplidos(moves_problema)
            raise ValidationError(
                "No se puede validar la operación porque los siguientes productos "
                "no respetan su múltiplo de distribución:\n\n" + detalle +
                "\n\nSi necesitás validar de todas formas, contactá a un usuario "
                "con permiso para saltear esta restricción."
            )
        return super().button_validate()

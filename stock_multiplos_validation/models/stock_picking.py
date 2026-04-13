# -*- coding: utf-8 -*-
from odoo import models
from odoo.exceptions import ValidationError, UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _moves_con_multiplo_incumplido(self):
        """Devuelve los moves activos que no respetan su múltiplo de distribución."""
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

    def _mensaje_multiplos_incumplidos(self, moves):
        """Genera el texto descriptivo de los moves problemáticos."""
        lineas = []
        for move in moves:
            multiplo = move.product_id.mutiplos_distribucion or 1
            cantidad = move.quantity
            proximo = int(cantidad // multiplo + 1) * multiplo
            lineas.append(
                f"  • {move.product_id.display_name}: "
                f"cantidad {cantidad:.0f} no es múltiplo de {multiplo} "
                f"(próximo válido: {proximo:.0f})"
            )
        return "\n".join(lineas)

    def action_assign(self):
        res = super().action_assign()
        for picking in self:
            moves_problema = picking._moves_con_multiplo_incumplido()
            if moves_problema:
                detalle = picking._mensaje_multiplos_incumplidos(moves_problema)
                raise UserError(
                    "Advertencia: las siguientes líneas no respetan el múltiplo "
                    "de distribución. Corregí las cantidades antes de validar:\n\n"
                    + detalle
                )
        return res

    def button_validate(self):
        for picking in self:
            moves_problema = picking._moves_con_multiplo_incumplido()
            if not moves_problema:
                continue
            if self.env.user.has_group('stock_multiplos_validation.group_bypass_multiplos'):
                continue
            detalle = picking._mensaje_multiplos_incumplidos(moves_problema)
            raise ValidationError(
                "No se puede validar la operación porque los siguientes productos "
                "no respetan su múltiplo de distribución:\n\n" + detalle +
                "\n\nContactá a un usuario con permiso para saltear esta restricción."
            )
        return super().button_validate()

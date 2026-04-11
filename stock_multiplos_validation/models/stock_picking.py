# -*- coding: utf-8 -*-
from odoo import models
from odoo.exceptions import ValidationError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _check_multiplos_distribucion(self):
        """
        Verifica que las cantidades de los moves respeten el múltiplo de
        distribución del producto cuando el tipo de operación tiene
        'respeta_multiplos' activo.

        Solo aplica a productos cuya UoM pertenece a la categoría "Unidades"
        (discreta). Productos medidos en kg, litros, metros, etc. se omiten.

        Acumula todos los productos con cantidades inválidas y lanza un único
        ValidationError con el detalle completo.
        """
        if not self.picking_type_id.respeta_multiplos:
            return

        uom_unit_category = self.env.ref('uom.product_uom_categ_unit', raise_if_not_found=False)

        errores = []
        for move in self.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            producto = move.product_id
            multiplo = getattr(producto, 'mutiplos_distribucion', 1) or 1

            # Sin restricción de múltiplo
            if multiplo <= 1:
                continue

            # Solo validar UoM de categoría "Unidades" (discreta)
            if uom_unit_category and move.product_uom.category_id != uom_unit_category:
                continue

            cantidad = move.quantity
            if cantidad <= 0:
                continue

            if cantidad % multiplo != 0:
                errores.append(
                    f"  • {producto.display_name} "
                    f"[{move.product_uom.name}]: "
                    f"cantidad {cantidad:.0f} no es múltiplo de {multiplo} "
                    f"(próximo válido: {int(cantidad // multiplo + 1) * multiplo:.0f})"
                )

        if errores:
            detalle = "\n".join(errores)
            raise ValidationError(
                "No se puede validar la operación porque los siguientes productos "
                "no respetan su múltiplo de distribución:\n\n"
                + detalle
            )

    def button_validate(self):
        for picking in self:
            picking._check_multiplos_distribucion()
        return super().button_validate()

# -*- coding: utf-8 -*-
import logging
import math

from odoo import api, models
from odoo.tools import float_compare, float_round

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def primate_round_to_multiple(self, qty, rounding_method='ceil', max_qty=None):
        """Ajusta una cantidad al múltiplo de distribución del producto.

        El múltiplo (``mutiplos_distribucion``, definido en automatic_crossdocking) es una
        restricción física: no se puede mover 7 unidades si el producto viaja en packs de 6.
        stock_multiplos_validation lo exige al validar el picking; acá se resuelve antes,
        al calcular la cantidad.

        :param qty: cantidad a ajustar.
        :param rounding_method: 'ceil' (asegura cubrir la necesidad) o 'floor' (evita
            pasarse cuando el stock es ajustado).
        :param max_qty: tope opcional. Si el resultado lo supera, se baja al múltiplo
            inferior. Se usa cuando la cantidad no puede exceder el stock del origen.
        :return: cantidad ajustada, o 0.0 si ni el múltiplo inferior entra en el tope.
        """
        self.ensure_one()
        multiple = self.mutiplos_distribucion or 1
        if multiple <= 1 or qty <= 0:
            return qty

        rounding = self.uom_id.rounding or 0.01
        # float_round sobre el ratio evita que 6.0000001 / 6 escale a dos múltiplos.
        ratio = float_round(qty / multiple, precision_digits=6)
        if rounding_method == 'floor':
            ajustada = math.floor(ratio) * multiple
        else:
            ajustada = math.ceil(ratio) * multiple

        if max_qty is not None and float_compare(ajustada, max_qty, precision_rounding=rounding) > 0:
            ratio_tope = float_round(max_qty / multiple, precision_digits=6)
            ajustada = math.floor(ratio_tope) * multiple

        return float(max(ajustada, 0.0))

    def primate_is_valid_multiple(self, qty):
        """Indica si una cantidad respeta el múltiplo de distribución del producto."""
        self.ensure_one()
        multiple = self.mutiplos_distribucion or 1
        if multiple <= 1:
            return True
        rounding = self.uom_id.rounding or 0.01
        resto = float_round(qty % multiple, precision_rounding=rounding)
        return float_compare(resto, 0.0, precision_rounding=rounding) == 0

    @api.model
    def primate_products_from_categories_and_groups(self, categories, warehouse_groups,
                                                    extra_domain=None):
        """Productos que cumplen categoría Y grupo de almacenes, con las hijas incluidas.

        Las dos condiciones se aplican en AND (regla 5.1 de la especificación): un producto
        sin ``warehouse_group_id`` asignado nunca entra por esta vía, aunque su categoría
        coincida, porque no hay forma de verificar a qué almacenes puede ir.

        :param categories: recordset de product.category (puede estar vacío).
        :param warehouse_groups: recordset de stock.warehouse.group (puede estar vacío).
        :param extra_domain: dominio adicional propio de cada flujo.
        :return: recordset de product.product.
        """
        domain = []
        if categories:
            domain.append(('categ_id', 'child_of', categories.ids))
        if warehouse_groups:
            domain.append(('warehouse_group_id', 'in', warehouse_groups.ids))
        if not domain:
            return self.browse()
        if extra_domain:
            domain += extra_domain
        return self.search(domain)

from odoo import models, api, fields


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    mutiplos_distribucion = fields.Integer(
        string='Múltiplos de Distribución',
        default=1,
        help='Cantidad mínima de unidades que se deben pedir o distribuir en múltiplos de este número.'
    )

    def write(self, vals):
        if 'mutiplos_distribucion' in vals:
            if vals['mutiplos_distribucion'] < 1:
                raise ValueError("El campo 'Múltiplos de Distribución' debe ser un número entero positivo mayor o igual a 1.")

            # Sincronizar a las variantes que NO tienen un valor propio, en una sola escritura
            # y avisando que viene de la plantilla: antes se escribía variante por variante y
            # cada escritura las marcaba como «valor propio», así que desde el segundo cambio
            # la plantilla dejaba de propagarles el múltiplo.
            variantes = self.product_variant_ids.filtered(
                lambda v: not v.mutiplos_distribucion_override
                and v.mutiplos_distribucion != vals['mutiplos_distribucion'])
            if variantes:
                variantes.with_context(multiplo_desde_plantilla=True).write(
                    {'mutiplos_distribucion': vals['mutiplos_distribucion']})

        return super().write(vals)

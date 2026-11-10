from odoo import fields, api, models;




class ProductProduct(models.Model):
    _inherit = 'product.product'

    mutiplos_distribucion = fields.Integer(
        string='Múltiplos de Distribución',
        default=1,
        help='Cantidad mínima de unidades. Si está vacío, se usa el valor de la plantilla.'
    )
    
    mutiplos_distribucion_override = fields.Boolean(
        string='Override Múltiplos',
        default=False,
        help='Indica si esta variante tiene un valor personalizado que no debe sincronizarse con la plantilla.'
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Una variante nueva hereda el múltiplo de su plantilla (si no trae uno propio):
        # así no queda en el default 1 cuando la plantilla ya definió un múltiplo, que es
        # lo que dejaba la línea de OC en 1 pese a la plantilla en 4.
        for vals in vals_list:
            if 'mutiplos_distribucion' not in vals and not vals.get('mutiplos_distribucion_override'):
                tmpl_id = vals.get('product_tmpl_id')
                if tmpl_id:
                    tmpl = self.env['product.template'].browse(tmpl_id)
                    if tmpl.mutiplos_distribucion and tmpl.mutiplos_distribucion > 1:
                        vals['mutiplos_distribucion'] = tmpl.mutiplos_distribucion
        return super().create(vals_list)

    def _multiplo_distribucion_efectivo(self):
        """Múltiplo de distribución a aplicar para esta variante: el propio si la variante
        tiene un valor personalizado (override), de lo contrario el de la plantilla. Mínimo 1.
        Se resuelve en el momento para no depender de que el valor stored de la variante esté
        sincronizado (variantes nuevas, altas por import o plantilla cambiada después)."""
        self.ensure_one()
        if self.mutiplos_distribucion_override:
            valor = self.mutiplos_distribucion
        else:
            valor = self.product_tmpl_id.mutiplos_distribucion
        return valor if valor and valor > 1 else 1

    def write(self, vals):
        # Si se modifica mutiplos_distribucion directamente en la variante, marcar como override
        if 'mutiplos_distribucion' in vals:
            if vals['mutiplos_distribucion'] < 1:
                raise ValueError("El campo 'Múltiplos de Distribución' debe ser un número entero positivo mayor o igual a 1.")
            
            # Marcar que esta variante tiene un valor personalizado, salvo que el valor venga
            # de la plantilla (propagación).
            if not self.env.context.get('multiplo_desde_plantilla'):
                vals['mutiplos_distribucion_override'] = True
        
        # Si se desmarca el override, sincronizar con el template
        if 'mutiplos_distribucion_override' in vals and not vals['mutiplos_distribucion_override']:
            for record in self:
                vals['mutiplos_distribucion'] = record.product_tmpl_id.mutiplos_distribucion
        
        return super().write(vals)

    @api.onchange('mutiplos_distribucion_override')
    def _onchange_mutiplos_override(self):
        """Cuando se desmarca el override, restaurar el valor del template"""
        if not self.mutiplos_distribucion_override and self.product_tmpl_id:
            self.mutiplos_distribucion = self.product_tmpl_id.mutiplos_distribucion
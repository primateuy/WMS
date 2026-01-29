# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError


class CrossdockingWizard(models.TransientModel):
    _name = 'crossdocking.wizard'
    _description = 'Asistente para Configuración Masiva de Crossdocking'

    product_ids = fields.Many2many('product.product', string='Productos')
    partner_id = fields.Many2one('res.partner', string='Cliente/Sucursal Destino', required=True)
    location_id = fields.Many2one('stock.location', string='Ubicación Destino', required=True)
    porcentaje = fields.Float(string='Porcentaje a Distribuir (%)', default=100.0)
    cantidad_fija = fields.Float(string='Cantidad Fija (opcional)')
    usar_cantidad_fija = fields.Boolean(string='Usar Cantidad Fija')

    @api.constrains('porcentaje')
    def _check_porcentaje(self):
        for record in self:
            if record.porcentaje <= 0 or record.porcentaje > 100:
                raise ValidationError("El porcentaje debe estar entre 1 y 100")

    def crear_configuraciones(self):
        """Crear configuraciones de crossdocking para los productos seleccionados"""
        for product in self.product_ids:
            # Verificar si ya existe configuración para este producto
            config_existente = self.env['crossdocking.config'].search([
                ('product_id', '=', product.id),
                ('active', '=', True)
            ], limit=1)
            
            if config_existente:
                # Agregar destino a configuración existente
                destino_existente = config_existente.destino_ids.filtered(
                    lambda d: d.partner_id.id == self.partner_id.id and d.location_id.id == self.location_id.id
                )
                
                if not destino_existente:
                    vals_destino = {
                        'config_id': config_existente.id,
                        'partner_id': self.partner_id.id,
                        'location_id': self.location_id.id,
                        'porcentaje': 0 if self.usar_cantidad_fija else self.porcentaje,
                        'cantidad': self.cantidad_fija if self.usar_cantidad_fija else 0,
                    }
                    self.env['crossdocking.destino'].create(vals_destino)
            else:
                # Crear nueva configuración
                vals_config = {
                    'name': f"Crossdocking {product.name}",
                    'product_id': product.id,
                    'active': True,
                }
                
                nueva_config = self.env['crossdocking.config'].create(vals_config)
                
                # Crear destino
                vals_destino = {
                    'config_id': nueva_config.id,
                    'partner_id': self.partner_id.id,
                    'location_id': self.location_id.id,
                    'porcentaje': 0 if self.usar_cantidad_fija else self.porcentaje,
                    'cantidad': self.cantidad_fija if self.usar_cantidad_fija else 0,
                }
                self.env['crossdocking.destino'].create(vals_destino)
        
        return {
            'type': 'ir.actions.act_window',
            'name': 'Configuraciones de Crossdocking',
            'res_model': 'crossdocking.config',
            'view_mode': 'tree,form',
            'domain': [('product_id', 'in', self.product_ids.ids)],
        }
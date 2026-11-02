from odoo import api, fields, models




class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    cluster_id = fields.Many2one(
        'stock.warehouse.group', string='Cluster', compute='_compute_cluster_id',
        inverse='_inverse_cluster_id', domain=[('nivel_jerarquia_id', '!=', False)],
        help="Cluster de la sucursal. Una sucursal está en un solo Cluster; al cambiarlo sale "
             "del anterior y queda un cambio pendiente para «Actualizar Reglas».")

    # «Grupo de almacén» (`warehouse_group_ids`, de setu_advance_reordering) es la MISMA
    # relación vista desde la sucursal: editarla tiene que respetar la pertenencia única y
    # dejar el cambio pendiente igual que desde el grupo.
    @api.model_create_multi
    def create(self, vals_list):
        if not any('warehouse_group_ids' in v for v in vals_list):
            return super().create(vals_list)
        Grupo = self.env['stock.warehouse.group']
        antes = Grupo._cluster_pertenencia()
        almacenes = super().create(vals_list)
        almacenes._cluster_despues_de_editar_grupos(antes)
        return almacenes

    def write(self, vals):
        if 'warehouse_group_ids' not in vals:
            return super().write(vals)
        antes = self.env['stock.warehouse.group']._cluster_pertenencia()
        res = super().write(vals)
        self._cluster_despues_de_editar_grupos(antes)
        return res

    def _cluster_despues_de_editar_grupos(self, antes):
        despues = self.env['stock.warehouse.group']._cluster_pertenencia()
        nuevos = set()
        for almacen in self:
            nuevos |= despues.get(almacen.id, set()) - antes.get(almacen.id, set())
        self.env['stock.warehouse.group'].sudo().browse(sorted(nuevos))._cluster_despues_de_cambiar(antes)

    def _compute_cluster_id(self):
        clusters = self.env['stock.warehouse.group'].sudo().search([('nivel_jerarquia_id', '!=', False)])
        for almacen in self:
            almacen.cluster_id = clusters.filtered(lambda g: almacen in g.warehouse_ids)[:1]

    def _inverse_cluster_id(self):
        Grupo = self.env['stock.warehouse.group']
        for almacen in self:
            actuales = Grupo.search([('nivel_jerarquia_id', '!=', False),
                                     ('warehouse_ids', 'in', almacen.id)])
            if almacen.cluster_id:
                if almacen.cluster_id not in actuales:
                    # Entrar al nuevo lo saca del anterior (ver stock.warehouse.group.write).
                    almacen.cluster_id.write({'warehouse_ids': [(4, almacen.id)]})
                elif len(actuales) > 1:
                    (actuales - almacen.cluster_id).write({'warehouse_ids': [(3, almacen.id)]})
            elif actuales:
                actuales.write({'warehouse_ids': [(3, almacen.id)]})

    group_location_ids = fields.One2many(
        'warehouse.group.location',
        'warehouse_id', 
        string='Ubicaciones por Grupo'
    )


class WarehouseGroupLocation(models.Model):
    _name = 'warehouse.group.location'
    _description = 'Ubicaciones por Grupo en Almacén'

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Almacén',
        required=True,
        ondelete='cascade'
    )
    group_id = fields.Many2one(
        'stock.warehouse.group',
        string='Grupo de Almacenes',
        required=True
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Ubicación Designada',
        required=True,
        domain="[('usage', '=', 'internal')]"
    )

    default_min_qty = fields.Float(string='Cantidad mínima default', required=True)
    default_max_qty = fields.Float(string='Cantidad máxima default', required=True)

    automate_reordering = fields.Boolean(string="Reabastecimiento aútomatico", default=False);

    _sql_constraints = [
        ('warehouse_group_unique',
         'UNIQUE(warehouse_id, group_id)',
         'Ya existe una configuración para este grupo en el almacén'),
    ]
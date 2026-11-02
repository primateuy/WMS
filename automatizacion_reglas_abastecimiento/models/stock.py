from odoo import _, fields, models, api
from odoo.exceptions import ValidationError


import logging
_logger = logging.getLogger(__name__);

class StockLocation(models.Model):
    _inherit = 'stock.location'

    automate_reordering = fields.Boolean(
        string='Participa en automatización',
        help="Habilita esta ubicación para reglas de reabastecimiento automático"
    )
    
    location_src_id = fields.Many2one(
        'stock.location',
        string='Ubicación de abastecimiento',
        domain="[('usage', '=', 'internal')]",
        help="Desde dónde se repondrá el stock (ej: centro logístico)"
    )
    
    default_min_qty = fields.Float(
        string='Cantidad mínima por defecto',
        default=0,
        help="Cantidad mínima para trigger de reabastecimiento"
    )
    
    default_max_qty = fields.Float(
        string='Cantidad máxima por defecto',
        default=0,
        help="Cantidad máxima para reposición"
    )

class StockRule(models.Model):
    _inherit = 'stock.rule';

    auto_generated = fields.Boolean(string="Generado Automáticamente", default=False)
    is_hierarchical = fields.Boolean(string="Regla Jerárquica", default=False)


class StockWareHouseGroup(models.Model):
    _inherit = 'stock.warehouse.group'

    product_template_ids = fields.One2many(
        'product.template',
        'warehouse_group_id',
        string='Productos en el Grupo de Almacenes'
    )

    product_variant_ids = fields.One2many(
        'product.product',
        'warehouse_group_id',
        string='Variantes de Producto en el Grupo de Almacenes'
    )

    category_rule_ids = fields.One2many(
            'warehouse.group.category.rule',
            'warehouse_group_id',
            string='Reglas por Categoría'
        )
    
    nivel_jerarquia_id = fields.Many2one(
        'niveles.jerarquia',
        string='Nivel de Jerarquía',
        ondelete='set null'
    )

    nivel_jerarquia_nombre = fields.Char(
        string="Nombre del Nivel",
        related='nivel_jerarquia_id.nombre',
        store=False
    )


    def actualizarReglasWizardManual(self):
        """Abre el wizard para actualización manual de reglas"""
        return {
            'name': 'Actualizar Reglas de Abastecimiento',
            'type': 'ir.actions.act_window',
            'res_model': 'stock.warehouse.group.rules.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_warehouse_group_id': self.id,
            }
        }
    
    # ------------------------------------------------------------------
    # Una sola pertenencia por sucursal y registro de cambios de Cluster
    # (documento «Automatización de Rutas Productos», objetivos 1 y 2)
    # ------------------------------------------------------------------
    @api.model
    def _cluster_pertenencia(self):
        """{almacén id: {Cluster id}}. Cluster = grupo con nivel de jerarquía."""
        pertenencia = {}
        for grupo in self.sudo().search([('nivel_jerarquia_id', '!=', False)]):
            for almacen in grupo.warehouse_ids:
                pertenencia.setdefault(almacen.id, set()).add(grupo.id)
        return pertenencia

    @api.model_create_multi
    def create(self, vals_list):
        antes = self._cluster_pertenencia()
        grupos = super().create(vals_list)
        grupos._cluster_despues_de_cambiar(antes)
        return grupos

    def write(self, vals):
        if ('warehouse_ids' not in vals and 'nivel_jerarquia_id' not in vals) \
                or self.env.context.get('cluster_moviendo'):
            return super().write(vals)
        antes = self._cluster_pertenencia()
        res = super().write(vals)
        self._cluster_despues_de_cambiar(antes)
        return res

    def _cluster_despues_de_cambiar(self, antes):
        """Una sucursal que entra a un Cluster sale del que tenía: el resultado es una
        pertenencia única. Cada sucursal que cambió deja un cambio pendiente."""
        Grupo = self.sudo()
        for grupo in self.filtered('nivel_jerarquia_id'):
            for almacen in grupo.warehouse_ids:
                if grupo.id in antes.get(almacen.id, set()):
                    continue
                otros = Grupo.search([('nivel_jerarquia_id', '!=', False), ('id', '!=', grupo.id),
                                      ('warehouse_ids', 'in', almacen.id)])
                if otros:
                    otros.with_context(cluster_moviendo=True).write(
                        {'warehouse_ids': [(3, almacen.id)]})
        despues = self._cluster_pertenencia()
        repetidas = [a for a, g in despues.items() if len(g) > 1
                     and any(gr.id in g for gr in self)]
        if repetidas:
            nombres = ', '.join(self.env['stock.warehouse'].browse(repetidas).mapped('display_name'))
            raise ValidationError(_("Una sucursal puede estar en un solo Cluster: %s") % nombres)
        Cambio = self.env['cluster.cambio.almacen'].sudo()
        for almacen_id in set(antes) | set(despues):
            ya, va = antes.get(almacen_id, set()), despues.get(almacen_id, set())
            if ya == va:
                continue
            # Si estaba en dos (configuración vieja), recibía lo de los dos: vale el de
            # nivel más alto, que es el que más productos le daba.
            anterior = Grupo.browse(list(ya)).sorted(lambda g: g.nivel_jerarquia_id.seq)[:1]
            Cambio.create({
                'warehouse_id': almacen_id, 'tipo': 'cambio',
                'grupo_anterior_id': anterior.id,
                'grupo_nuevo_id': next(iter(va)) if va else False,
            })


class StockWarehouseGroupRulesWizard(models.TransientModel):
    _name = 'stock.warehouse.group.rules.wizard'
    _description = 'Wizard para actualización en almacenes seleccionados'

    warehouse_group_id = fields.Many2one(
        'stock.warehouse.group',
        string='Grupo de Almacenes',
        required=True,
        readonly=True,
    )

    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        'wizard_warehouse_rel',
        'wizard_id',
        'warehouse_id',
        string='Almacenes',
        required=True,
        help='Seleccione los almacenes donde desea actualizar las reglas',
    )

    available_warehouse_ids = fields.Many2many(
        'stock.warehouse',
        compute='_compute_available_warehouse_ids',
        store=False,
    )

    @api.depends('warehouse_group_id')
    def _compute_available_warehouse_ids(self):
        for record in self:
            if record.warehouse_group_id:
                record.available_warehouse_ids = record.warehouse_group_id.warehouse_ids
            else:
                record.available_warehouse_ids = False

    @api.model
    def default_get(self, fields_list):
        res = super(StockWarehouseGroupRulesWizard, self).default_get(fields_list)
        warehouse_group_id = self.env.context.get('default_warehouse_group_id')
        if warehouse_group_id:
            res['warehouse_group_id'] = warehouse_group_id
        return res

    @api.onchange('warehouse_group_id')
    def _onchange_warehouse_group_id(self):
        if self.warehouse_group_id:
            warehouse_ids = self.warehouse_group_id.warehouse_ids.ids
            if self.warehouse_ids:
                self.warehouse_ids = self.warehouse_ids.filtered(
                    lambda w: w.id in warehouse_ids
                )
            return {'domain': {'warehouse_ids': [('id', 'in', warehouse_ids)]}}
        
        self.warehouse_ids = False
        return {'domain': {'warehouse_ids': [('id', '=', False)]}}

    @api.model
    def default_get(self, fields_list):
        """Establece valores por defecto incluyendo el dominio de almacenes"""
        res = super().default_get(fields_list)
        
        # Si viene el grupo desde el contexto
        warehouse_group_id = self.env.context.get('default_warehouse_group_id')
        if warehouse_group_id:
            grupo = self.env['stock.warehouse.group'].browse(warehouse_group_id)
            _logger.info("Grupo cargado: %s", grupo)
            _logger.info("ALMACENES DEL GRUPO: %s", grupo.warehouse_ids.ids)
            
            # Pre-selecciona todos los almacenes del grupo
            res['warehouse_ids'] = [(6, 0, grupo.warehouse_ids.ids)]
        
        return res

    @api.onchange('warehouse_group_id')
    def _onchange_warehouse_group_id(self):
        """Filtra los almacenes disponibles según el grupo seleccionado"""
        _logger.info("Onchange triggered for warehouse_group_id: %s", self.warehouse_group_id)
        
        if self.warehouse_group_id:
            warehouse_ids = self.warehouse_group_id.warehouse_ids.ids
            _logger.info("Available warehouses in group: %s", warehouse_ids)
            
            # Limpia la selección actual
            self.warehouse_ids = [(5, 0, 0)]  # Elimina todos los registros
            
            return {
                'domain': {
                    'warehouse_ids': [('id', 'in', warehouse_ids)]
                },
                'value': {
                    'warehouse_ids': [(6, 0, warehouse_ids)]  # Pre-selecciona todos los almacenes del grupo
                }
            }
        
        return {
            'domain': {
                'warehouse_ids': []
            },
            'value': {
                'warehouse_ids': [(5, 0, 0)]  # Elimina todos los registros
            }
        }
    
    def actualizarReglas(self):
        """Actualiza las reglas del grupo en los almacenes elegidos, en segundo plano.

        Antes borraba y volvía a crear todas las reglas de los productos, una por una y dentro
        de la petición (41.313 reglas y ≈ 10 minutos para el grupo más chico), con commits a
        medias que dejaban almacenes actualizados y otros no cuando el servidor lo cortaba.
        Ahora encola un `cluster.proceso` que compara contra lo que hay y sólo crea, modifica
        o borra lo que cambia, por tandas, con progreso visible.

        Qué productos: los que tienen reglas en los almacenes de este grupo, con el mismo
        criterio que el cambio de Cluster (`_cluster_reglas_objetivo`): un producto lleva los
        almacenes de su grupo y de los grupos de nivel de jerarquía menor o igual. Entonces
        los afectados son los de este grupo y los de los grupos de nivel MAYOR o igual.
        """
        self.ensure_one()
        if not self.warehouse_ids:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Advertencia'),
                    'message': _('Debe seleccionar al menos un almacén'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        # Primero los cambios de Cluster pendientes de TODAS las sucursales, no sólo las de
        # este grupo: la que se fue también tiene que perder lo que ya no le corresponde.
        procesos_cambio = self.env['cluster.cambio.almacen']._aplicar_pendientes()
        grupo = self.warehouse_group_id
        Grupo = self.env['stock.warehouse.group']
        if grupo.nivel_jerarquia_id:
            grupos = Grupo.search([('nivel_jerarquia_id.seq', '>=', grupo.nivel_jerarquia_id.seq)]) | grupo
        else:
            grupos = grupo
        variantes = self.env['product.product'].search([('warehouse_group_id', 'in', grupos.ids)])
        # Con las rutas derivadas del Cluster, de paso se corrigen las rutas de sucursal de
        # esos productos: es lo que hace que las reglas creadas tengan con qué abastecerse.
        Proceso = self.env['cluster.proceso']
        con_rutas = Proceso._rutas_desde_cluster()
        proceso = Proceso._encolar(
            templates=variantes.product_tmpl_id if con_rutas else None,
            variants=variantes, con_rutas=con_rutas, warehouses=self.warehouse_ids,
            origen=_("Actualizar Reglas del grupo %s") % grupo.display_name)
        if procesos_cambio and not proceso:
            return procesos_cambio[:1]._notificacion_encolado(len(procesos_cambio.template_ids))
        return proceso._notificacion_encolado(len(variantes))

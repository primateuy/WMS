# -*- coding: utf-8 -*-
"""Cambios de Cluster de las sucursales y cómo se aplican.

POR QUÉ EXISTE (documento de Forum «Automatización de Rutas Productos», 15/21-10-2026).
Pasar una sucursal de Cluster no cambiaba nada más: las automatizaciones «Auto Seleccion
de Rutas …» seguían con sus acciones viejas (17 desalineadas en la copia del 22-10) y los
productos con las rutas de antes. «Actualizar Reglas» procesaba sólo el grupo destino y
los niveles mayores: a la sucursal que se iba no le sacaba nada.

QUÉ ES «CORRESPONDER». El criterio vigente, el mismo de las reglas de abastecimiento
(`product.product._cluster_reglas_objetivo`): un producto de un Cluster con nivel de
jerarquía va a las sucursales de los Clusters de nivel `seq` MENOR O IGUAL; sin nivel, a
las de su Cluster. Con los niveles cargados en Forum (CLUSTER 5 = 1 … MIX BASE = 6), una
tienda de CLUSTER 3 recibe MIX BASE, C1, C2 y C3.

CÓMO SE APLICA. Mover una sucursal deja un cambio pendiente; «Actualizar Reglas»:

1. rearma las acciones de las automatizaciones de Cluster desde la pertenencia, para que
   lo que se asigne de ahora en más salga bien;
2. por cada sucursal con cambios, encola un `cluster.proceso` que a los productos de los
   Clusters afectados —sólo los que cambian de «corresponde» a «no corresponde» o al
   revés— les agrega o les saca LA RUTA DE ESA SUCURSAL (las demás rutas no se tocan) y
   sincroniza sus reglas SÓLO EN ESA SUCURSAL.

Volver a aplicar no cambia nada: todo se calcula contra el estado objetivo.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


def corresponde(grupo_sucursal, grupo_producto):
    """¿Un producto de `grupo_producto` va a una sucursal de `grupo_sucursal`?"""
    if not grupo_sucursal or not grupo_producto:
        return False
    if grupo_producto.nivel_jerarquia_id:
        return bool(grupo_sucursal.nivel_jerarquia_id) and \
            grupo_sucursal.nivel_jerarquia_id.seq <= grupo_producto.nivel_jerarquia_id.seq
    return grupo_sucursal == grupo_producto


class ClusterCambioAlmacen(models.Model):
    _name = 'cluster.cambio.almacen'
    _description = 'Cambio de Cluster de una sucursal'
    _order = 'id desc'
    _rec_name = 'warehouse_id'

    warehouse_id = fields.Many2one('stock.warehouse', string='Sucursal', required=True,
                                   readonly=True, ondelete='cascade', index=True)
    tipo = fields.Selection([
        ('cambio', 'Cambio de Cluster'),
        ('alineacion', 'Alineación'),
    ], required=True, default='cambio', readonly=True,
        help="Alineación: diferencia entre una automatización de rutas y la pertenencia "
             "vigente, detectada al instalar esta versión.")
    grupo_anterior_id = fields.Many2one('stock.warehouse.group', string='Cluster anterior',
                                        readonly=True, ondelete='set null')
    grupo_nuevo_id = fields.Many2one('stock.warehouse.group', string='Cluster nuevo',
                                     readonly=True, ondelete='set null')
    grupo_producto_id = fields.Many2one(
        'stock.warehouse.group', string='Productos del Cluster', readonly=True, ondelete='cascade',
        help="Alineación: los productos de este Cluster.")
    operacion = fields.Selection([('add', 'Agregar la ruta'), ('remove', 'Quitar la ruta')],
                                 string='Operación', readonly=True)
    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('en_proceso', 'En proceso'),
        ('aplicado', 'Aplicado'),
    ], default='pendiente', required=True, readonly=True, index=True)
    proceso_id = fields.Many2one('cluster.proceso', string='Proceso', readonly=True,
                                 ondelete='set null')
    user_id = fields.Many2one('res.users', string='Usuario', readonly=True,
                              default=lambda self: self.env.user, ondelete='set null')
    resumen = fields.Char(compute='_compute_resumen')

    @api.depends('tipo', 'grupo_anterior_id', 'grupo_nuevo_id', 'grupo_producto_id', 'operacion')
    def _compute_resumen(self):
        for rec in self:
            if rec.tipo == 'alineacion':
                rec.resumen = _("%(op)s en los productos de %(g)s",
                                op=dict(self._fields['operacion'].selection).get(rec.operacion, ''),
                                g=rec.grupo_producto_id.display_name or '-')
            else:
                rec.resumen = "%s → %s" % (rec.grupo_anterior_id.display_name or _("sin Cluster"),
                                          rec.grupo_nuevo_id.display_name or _("sin Cluster"))

    # ------------------------------------------------------------------
    # Pertenencia
    # ------------------------------------------------------------------
    @api.model
    def _clusters(self):
        """Los grupos de almacenes que son Clusters: los que tienen nivel de jerarquía."""
        return self.env['stock.warehouse.group'].sudo().search([('nivel_jerarquia_id', '!=', False)])

    @api.model
    def _cluster_de(self, almacen, clusters=None):
        clusters = clusters if clusters is not None else self._clusters()
        return clusters.filtered(lambda g: almacen in g.warehouse_ids)[:1]

    # ------------------------------------------------------------------
    # Aplicar
    # ------------------------------------------------------------------
    @api.model
    def _operaciones_por_almacen(self, pendientes):
        """{almacén: {grupo de producto: 'add' | 'remove'}} de los cambios pendientes.

        Si una sucursal se movió varias veces, vale desde el Cluster que tenía antes del
        primer cambio pendiente hasta el que tiene ahora."""
        Grupo = self.env['stock.warehouse.group'].sudo()
        todos = Grupo.search([])
        clusters = todos.filtered('nivel_jerarquia_id')
        resultado = {}
        for almacen in pendientes.mapped('warehouse_id'):
            propios = pendientes.filtered(lambda c: c.warehouse_id == almacen).sorted('id')
            ops = resultado.setdefault(almacen, {})
            cambios = propios.filtered(lambda c: c.tipo == 'cambio')
            if cambios:
                antes = cambios[0].grupo_anterior_id
                ahora = self._cluster_de(almacen, clusters)
                for grupo in todos:
                    ya, va = corresponde(antes, grupo), corresponde(ahora, grupo)
                    if ya != va:
                        ops[grupo] = 'add' if va else 'remove'
            for alin in propios.filtered(lambda c: c.tipo == 'alineacion' and c.grupo_producto_id):
                # Contra la pertenencia de AHORA, por si después se movió.
                va = corresponde(self._cluster_de(almacen, clusters), alin.grupo_producto_id)
                ops[alin.grupo_producto_id] = 'add' if va else 'remove'
        return {a: o for a, o in resultado.items() if o}

    @api.model
    def _aplicar_pendientes(self, origen=''):
        """Rearma las automatizaciones y encola un proceso por sucursal con cambios.

        Devuelve los procesos encolados."""
        pendientes = self.sudo().search([('estado', '=', 'pendiente')])
        self.env['base.automation']._cluster_alinear_acciones(pendientes)
        if not pendientes:
            return self.env['cluster.proceso']
        Proceso = self.env['cluster.proceso']
        procesos = Proceso.browse()
        operaciones = self._operaciones_por_almacen(pendientes)
        for almacen, ops in operaciones.items():
            plantillas = self.env['product.template'].with_context(active_test=False).search(
                [('warehouse_group_id', 'in', [g.id for g in ops])])
            proceso = Proceso._encolar_almacen(
                almacen, {g.id: op for g, op in ops.items()}, plantillas,
                origen=origen or _("Cambio de Cluster de %s") % almacen.display_name)
            pendientes.filtered(lambda c: c.warehouse_id == almacen).write(
                {'estado': 'en_proceso', 'proceso_id': proceso.id} if proceso
                else {'estado': 'aplicado'})
            procesos |= proceso
        # Sucursales con cambios que no mueven nada (fue y volvió): quedan aplicados.
        pendientes.filtered(lambda c: c.estado == 'pendiente').write({'estado': 'aplicado'})
        return procesos

    def action_aplicar(self):
        procesos = self._aplicar_pendientes()
        if not procesos:
            return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
                'title': _("Cambios de Cluster"), 'type': 'info', 'sticky': False,
                'message': _("Automatizaciones alineadas. No había productos para actualizar.")}}
        return {
            'type': 'ir.actions.act_window', 'name': _("Actualización por Cluster"),
            'res_model': 'cluster.proceso', 'view_mode': 'tree,form',
            'domain': [('id', 'in', procesos.ids)],
        }

    # ------------------------------------------------------------------
    # Alineación inicial (migración 0.5)
    # ------------------------------------------------------------------
    @api.model
    def _registrar_alineacion_inicial(self):
        """Deja como pendientes las diferencias entre las automatizaciones y la pertenencia.

        No aplica nada: se aplica con «Actualizar Reglas», como cualquier cambio."""
        por_almacen = self._rutas_por_almacen()
        clusters = self._clusters()
        creados = self.browse()
        for automatizacion in self.env['base.automation']._cluster_automatizaciones():
            grupo = automatizacion._cluster_grupo_producto()
            if not grupo:
                continue
            for accion, almacen in automatizacion._cluster_acciones_por_almacen(por_almacen):
                cluster = self._cluster_de(almacen, clusters)
                if not cluster:
                    continue
                debe = 'add' if corresponde(cluster, grupo) else 'remove'
                if accion.update_m2m_operation != debe:
                    creados |= self.sudo().create({
                        'warehouse_id': almacen.id, 'tipo': 'alineacion',
                        'grupo_producto_id': grupo.id, 'operacion': debe,
                        'grupo_nuevo_id': cluster.id})
        return creados

    @api.model
    def _rutas_por_almacen(self):
        """{almacén id: {ruta id}}: las rutas de reabastecimiento de cada sucursal, las que
        Odoo marca con `supplied_wh_id`. Todas, también las de sucursales sin Cluster."""
        rutas = self.env['stock.route'].sudo().search([('supplied_wh_id', '!=', False),
                                                       ('product_selectable', '=', True)])
        por_almacen = {}
        for ruta in rutas:
            por_almacen.setdefault(ruta.supplied_wh_id.id, set()).add(ruta.id)
        return por_almacen

    @api.model
    def _sucursales_repetidas(self):
        """{almacén: grupos} de las sucursales que están en más de un Cluster."""
        repetidas = {}
        for cluster in self._clusters():
            for almacen in cluster.warehouse_ids:
                repetidas.setdefault(almacen, self.env['stock.warehouse.group'])
                repetidas[almacen] |= cluster
        return {a: g for a, g in repetidas.items() if len(g) > 1}


class BaseAutomation(models.Model):
    _inherit = 'base.automation'

    def _cluster_grupo_producto(self):
        """El Cluster de productos de la automatización, leído de su dominio."""
        self.ensure_one()
        try:
            dominio = safe_eval(self.filter_domain or '[]', {})
        except Exception:
            return self.env['stock.warehouse.group']
        for hoja in dominio:
            if (isinstance(hoja, (list, tuple)) and len(hoja) == 3 and hoja[0] == 'warehouse_group_id'
                    and hoja[1] in ('in', '=')):
                ids = list(hoja[2]) if isinstance(hoja[2], (list, tuple)) else [hoja[2]]
                if len(ids) == 1:
                    return self.env['stock.warehouse.group'].sudo().browse(ids).exists()
        return self.env['stock.warehouse.group']

    def _cluster_acciones_por_almacen(self, por_almacen):
        """[(acción, sucursal)] de las acciones que agregan o quitan la ruta de una sucursal."""
        self.ensure_one()
        por_ruta = {}
        for almacen_id, rutas in por_almacen.items():
            for ruta_id in rutas:
                por_ruta[ruta_id] = almacen_id
        Almacen = self.env['stock.warehouse'].sudo()
        resultado = []
        for accion in self.sudo().action_server_ids:
            if (accion.state != 'object_write' or accion.update_path != 'route_ids'
                    or accion.update_m2m_operation not in ('add', 'remove')
                    or not (accion.value or '').isdigit()):
                continue
            almacen_id = por_ruta.get(int(accion.value))
            if almacen_id:
                resultado.append((accion, Almacen.browse(almacen_id)))
        return resultado

    @api.model
    def _cluster_alinear_acciones(self, pendientes=None):
        """Deja cada acción de las automatizaciones de Cluster como corresponde a la
        pertenencia vigente, y agrega la de una sucursal que no tenía.

        Las rutas de sucursales que no están en ningún Cluster no se tocan, salvo las que
        salieron de su Cluster en un cambio pendiente: esas se quitan."""
        Cambio = self.env['cluster.cambio.almacen']
        por_almacen = Cambio._rutas_por_almacen()
        clusters = Cambio._clusters()
        salieron = set((pendientes or Cambio).filtered(
            lambda c: c.tipo == 'cambio' and not c.grupo_nuevo_id).mapped('warehouse_id').ids)
        campo = self.env['ir.model.fields']._get('product.template', 'route_ids')
        cambiadas = 0
        for automatizacion in self._cluster_automatizaciones():
            grupo = automatizacion._cluster_grupo_producto()
            if not grupo:
                continue
            con_accion = set()
            for accion, almacen in automatizacion._cluster_acciones_por_almacen(por_almacen):
                con_accion.add(almacen.id)
                cluster = Cambio._cluster_de(almacen, clusters)
                if cluster:
                    debe = 'add' if corresponde(cluster, grupo) else 'remove'
                elif almacen.id in salieron:
                    debe = 'remove'
                else:
                    continue
                if accion.update_m2m_operation != debe:
                    accion.sudo().update_m2m_operation = debe
                    cambiadas += 1
            for cluster in clusters:
                for almacen in cluster.warehouse_ids:
                    if almacen.id in con_accion or not corresponde(cluster, grupo):
                        continue
                    for ruta_id in sorted(por_almacen.get(almacen.id, ())):
                        self.env['ir.actions.server'].sudo().create({
                            'name': _("Ruta %s") % almacen.display_name,
                            'model_id': automatizacion.model_id.id, 'state': 'object_write',
                            'base_automation_id': automatizacion.id, 'usage': 'base_automation',
                            'update_path': 'route_ids', 'update_field_id': campo.id,
                            'update_m2m_operation': 'add', 'evaluation_type': 'value',
                            'value': str(ruta_id), 'resource_ref': 'stock.route,%d' % ruta_id,
                        })
                        cambiadas += 1
        if cambiadas:
            _logger.info("[Cluster] automatizaciones alineadas: %d acciones cambiadas", cambiadas)
        return cambiadas

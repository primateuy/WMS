# -*- coding: utf-8 -*-
"""Las automatizaciones de rutas por Cluster corren en segundo plano, no al guardar.

Las «Auto Seleccion de Rutas …» (una por Cluster) no tienen campos disparadores:
corrían con CUALQUIER guardado de un producto del Cluster, 37 escrituras de
`route_ids` cada vez. Se marcan con `es_de_cluster` y quedan fuera del guardado;
las corre `cluster.proceso` cuando cambia el Cluster, leyendo sus acciones: la
configuración sigue viviendo en la automatización y se edita desde la pantalla.
"""
from odoo import api, fields, models

from .cluster_proceso import CTX_APLICANDO


class BaseAutomation(models.Model):
    _inherit = 'base.automation'

    es_de_cluster = fields.Boolean(
        string='Rutas por Cluster (en segundo plano)',
        help="Si está marcada, no corre al guardar el producto: la aplica el "
             "proceso en segundo plano cuando cambia el Cluster (menú Inventario › "
             "Actualización por Cluster).")

    def _get_actions(self, records, triggers):
        automations = super()._get_actions(records, triggers)
        if records._name == 'product.template' and not self.env.context.get(CTX_APLICANDO):
            automations = automations.filtered(lambda a: not a.sudo().es_de_cluster)
        return automations

    @api.model
    def _cluster_automatizaciones(self):
        return self.sudo().search([('es_de_cluster', '=', True), ('active', '=', True),
                                   ('model_name', '=', 'product.template')], order='id')

    def _cluster_operaciones_rutas(self):
        """Las acciones de la automatización como operaciones sobre `route_ids`.

        Devuelve la lista de (operación, id) en el orden en que Odoo las
        ejecuta, o None si alguna acción no es una escritura simple de
        `route_ids` —en ese caso la automatización se corre como siempre—.
        """
        self.ensure_one()
        operaciones = []
        for accion in self.sudo().action_server_ids:
            if (accion.state != 'object_write' or accion.evaluation_type != 'value'
                    or accion.update_path != 'route_ids'
                    or accion.update_m2m_operation not in ('add', 'remove', 'set', 'clear')):
                return None
            valor = int(accion.value) if accion.update_m2m_operation != 'clear' else 0
            operaciones.append((accion.update_m2m_operation, valor))
        return operaciones

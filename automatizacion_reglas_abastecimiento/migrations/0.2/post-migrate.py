# -*- coding: utf-8 -*-
"""Marca las automatizaciones de rutas por Cluster para que corran en segundo plano.

Son las de `product.template` cuyo dominio filtra por `warehouse_group_id` y
cuyas acciones escriben `route_ids`. Viven en la base (no en el módulo), así que
se identifican por su contenido y no por id: en cada base tienen otros.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE base_automation a SET es_de_cluster = true
          FROM ir_model m
         WHERE m.id = a.model_id AND m.model = 'product.template'
           AND a.filter_domain LIKE '%%warehouse_group_id%%'
           AND EXISTS (SELECT 1 FROM ir_act_server s
                        WHERE s.base_automation_id = a.id AND s.update_path = 'route_ids')
        RETURNING a.id
    """)
    ids = [r[0] for r in cr.fetchall()]
    _logger.info("automatizacion_reglas_abastecimiento: %d automatización(es) de rutas por "
                 "Cluster pasan a segundo plano: %s", len(ids), ids)

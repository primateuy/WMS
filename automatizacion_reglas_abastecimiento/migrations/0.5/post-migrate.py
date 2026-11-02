# -*- coding: utf-8 -*-
"""Cambios de Cluster: alineación inicial y sucursales repetidas.

- Las diferencias entre las automatizaciones «Auto Seleccion de Rutas …» y la pertenencia
  vigente quedan como cambios PENDIENTES de tipo «Alineación». No se aplica nada: se aplican
  con «Actualizar Reglas» (o desde Inventario › Cambios de Cluster), como cualquier cambio.
- Las sucursales que estén en más de un Cluster se listan en el log: no se elige sola cuál
  queda. Hasta resolverlas, guardar uno de esos Clusters avisa.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Cambio = env['cluster.cambio.almacen']
    for almacen, grupos in Cambio._sucursales_repetidas().items():
        _logger.warning("[Cluster] %s está en más de un Cluster: %s. Hay que dejarla en uno.",
                        almacen.display_name, ', '.join(grupos.mapped('display_name')))
    creados = Cambio._registrar_alineacion_inicial()
    _logger.info("[Cluster] alineación inicial: %d cambio(s) pendiente(s) en %d sucursal(es).",
                 len(creados), len(creados.mapped('warehouse_id')))

# -*- coding: utf-8 -*-
"""La conciliación semanal de productos pasa a correr los viernes a las 00:30 (hora de Uruguay).

Forum, 18-11-2026: corría a media tarde del miércoles, con el sistema cargado, y ese día no
entró en los 10 minutos del cron. Ahora va por tandas (ver
`conciliacion.maestros._procesar_productos`) y, además, de madrugada. El cron es `noupdate`:
el horario sólo se puede cambiar acá. No se lo prende ni se lo apaga: queda como esté.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref('integracion_wis.ir_cron_wis_conciliar_productos', raise_if_not_found=False)
    if not cron:
        return
    cron.with_context(active_test=False).write({
        'nextcall': env['conciliacion.maestros']._proximo_viernes_0030(),
        'interval_number': 1,
        'interval_type': 'weeks',
    })

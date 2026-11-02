# -*- coding: utf-8 -*-
"""Las notificaciones procesadas hasta acá no registraban qué partes aplicaban: no se marcan
como protegidas contra un reproceso (ver wis.notificacion.action_reprocesar)."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("UPDATE wis_notificacion SET marca_aplicado = false")

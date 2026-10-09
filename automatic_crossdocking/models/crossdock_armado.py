# -*- coding: utf-8 -*-
"""Armado en segundo plano de las OC con crossdock.

Una OC de crossdock típica (200-500 líneas a 30-40 sucursales) arma ≈ 120 pickings y más de
10.000 movimientos. Eso no entra en el tiempo de una petición (240 s de reloj y 120 s de
CPU en producción), así que la confirmación sólo cambia el estado de la OC y el armado lo
hace `_cron_armar_crossdock`, una OC por corrida, en su propia transacción.

El recálculo de la «cantidad a pedir» de las reglas de reabastecimiento lo difiere el
módulo `reorden_rendimiento`; para estas OC se encola recién al terminar el armado, que es
cuando sirve.
"""
import logging
import threading
import time

from markupsafe import Markup

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

MAX_INTENTOS_ARMADO = 3


def _en_test():
    return getattr(threading.current_thread(), 'testing', False)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    crossdock_armado_estado = fields.Selection([
        ('pendiente', 'Armando en segundo plano'),
        ('listo', 'Armado'),
        ('error', 'Error en el armado'),
    ], string="Armado del crossdock", copy=False, readonly=True, index=True)
    crossdock_armado_error = fields.Text(
        string="Error del armado", copy=False, readonly=True)
    crossdock_armado_intentos = fields.Integer(
        string="Intentos de armado", copy=False, readonly=True, default=0)

    # ------------------------------------------------------------------
    # Recálculo diferido de las reglas (reorden_rendimiento)
    # ------------------------------------------------------------------
    def _reorden_ordenes_a_encolar(self):
        """Las que quedaron para armar en segundo plano lo encolan al terminar el armado:
        recalcular antes sería hacerlo dos veces y competir con el armado por la base."""
        ordenes = super()._reorden_ordenes_a_encolar()
        return ordenes.filtered(lambda o: o.crossdock_armado_estado != 'pendiente')

    # ------------------------------------------------------------------
    # Armado en segundo plano
    # ------------------------------------------------------------------
    @api.model
    def _crossdock_disparar_cron_armado(self):
        cron = self.env.ref('automatic_crossdocking.ir_cron_armar_crossdock',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _cron_armar_crossdock(self):
        """Arma UNA OC pendiente por corrida y vuelve a dispararse si quedan más.

        El intento se cuenta y se graba ANTES de armar: si el worker se corta por tiempo, la
        transacción del armado se pierde pero el intento queda, y a los
        `MAX_INTENTOS_ARMADO` la OC pasa a error en lugar de reintentarse para siempre
        (un cron que se corta y no avanza vuelve a correr cada pocos minutos).
        """
        orden = self.search([('crossdock_armado_estado', '=', 'pendiente')], order='id', limit=1)
        if not orden:
            return True

        Recalculo = self.env['reorden.recalculo.pendiente']
        plantillas = orden.order_line.product_id.product_tmpl_id
        if orden.state not in ('purchase', 'done'):
            orden.write({'crossdock_armado_estado': False})
        elif orden.crossdock_armado_intentos >= MAX_INTENTOS_ARMADO:
            orden._crossdock_marcar_error(_(
                "El armado se interrumpió %s veces sin terminar (límite de tiempo del "
                "servidor). Revisar el log del servidor antes de reintentar."
            ) % orden.crossdock_armado_intentos)
        else:
            orden.crossdock_armado_intentos += 1
            self._crossdock_commit()
            inicio = time.time()
            try:
                # Crear los moves también dispara el recálculo de las reglas: se protegen y
                # se recalculan al final, con los moves ya creados.
                with self.env.cr.savepoint(), Recalculo.diferir(plantillas=plantillas, encolar=False):
                    orden._crossdock_armar()
            except Exception as e:
                _logger.exception("Crossdock %s: falló el armado", orden.name)
                orden._crossdock_marcar_error(str(e))
            else:
                orden.write({'crossdock_armado_estado': 'listo', 'crossdock_armado_error': False})
                orden.message_post(body=_(
                    "Crossdock armado: %(pickings)s operaciones en %(segundos)s s.",
                    pickings=len(orden.picking_ids), segundos=round(time.time() - inicio),
                ))
                _logger.info("Crossdock %s: armado en %.1f s", orden.name, time.time() - inicio)
        # El recálculo que la confirmación difirió se encola acá, termine como termine el
        # armado: la confirmación ya cambió las líneas y se debe hacer igual.
        if orden.crossdock_enabled:
            Recalculo._encolar(plantillas=plantillas, origen=_("Crossdock %s") % orden.name)
        self._crossdock_commit()

        if self.search_count([('crossdock_armado_estado', '=', 'pendiente')], limit=1):
            self._crossdock_disparar_cron_armado()
        return True

    def _crossdock_marcar_error(self, detalle):
        self.ensure_one()
        self.write({'crossdock_armado_estado': 'error', 'crossdock_armado_error': detalle})
        self.message_post(body=Markup(
            "<b>%s</b><br/>%s"
        ) % (_("No se pudo armar el crossdock."), detalle))

    @api.model
    def _crossdock_commit(self):
        if not _en_test():
            self.env.cr.commit()

    def action_crossdock_reintentar_armado(self):
        """Vuelve a encolar el armado de las OC en error."""
        ordenes = self.filtered(lambda o: o.crossdock_armado_estado == 'error')
        ordenes.write({
            'crossdock_armado_estado': 'pendiente',
            'crossdock_armado_error': False,
            'crossdock_armado_intentos': 0,
        })
        if ordenes:
            self._crossdock_disparar_cron_armado()
        return True

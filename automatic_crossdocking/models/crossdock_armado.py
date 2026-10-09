# -*- coding: utf-8 -*-
"""Armado en segundo plano de las OC con crossdock y trabajo diferido de la confirmación.

Una OC de crossdock típica (200-500 líneas a 30-40 sucursales) arma ≈ 120 pickings y más de
10.000 movimientos, y la confirmación además recalcula decenas de miles de puntos de reorden.
Nada de eso entra en el tiempo de una petición (240 s de reloj y 120 s de CPU en producción),
así que la confirmación sólo cambia el estado de la OC y el resto lo hacen dos crons:

* `_cron_armar_crossdock`: arma los pickings de una OC por corrida, en su propia transacción.
* `crossdock.reorden.pendiente`: recalcula la «cantidad a pedir» de los puntos de reorden por
  lotes, guardando el avance para retomar si una corrida se corta.
"""
import logging
import threading
import time

from markupsafe import Markup

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

MAX_INTENTOS_ARMADO = 3
LOTE_PUNTOS_REORDEN = 1000
# Presupuesto por corrida de cron, holgado respecto de los 120 s de CPU / 240 s de reloj.
SEGUNDOS_POR_CORRIDA = 90


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
            # Crear los moves también dispara el recálculo de los puntos de reorden: se
            # protegen y se recalculan al final, con los moves ya creados.
            puntos = orden._crossdock_puntos_reorden_a_diferir(estados=('purchase', 'done'))
            try:
                with self.env.cr.savepoint(), \
                        self.env.protecting(orden._crossdock_campos_reorden(), puntos):
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
        # El recálculo de los puntos de reorden que la confirmación difirió se encola recién
        # acá, termine como termine el armado: encolarlo al confirmar lo hacía correr en
        # paralelo con el armado, compitiendo por la base, y descartar el resultado.
        orden._crossdock_encolar_reorden()
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

    # ------------------------------------------------------------------
    # Puntos de reorden diferidos
    # ------------------------------------------------------------------
    def _crossdock_diferir_puntos_reorden(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'automatic_crossdocking.diferir_puntos_reorden', '1')
        return param not in ('0', 'False', 'false')

    @api.model
    def _crossdock_campos_reorden(self):
        """Campos almacenados del punto de reorden que la compra hace recalcular.

        `qty_to_order` (depende de las líneas de compra y de los proveedores de la
        plantilla) y todo lo que depende de él en el mismo modelo, como
        `forum_demand_effective`: si sólo se protegiera `qty_to_order`, el dependiente se
        recalcularía igual y en el camino calcularía `qty_to_order` punto por punto.
        """
        Orderpoint = self.env['stock.warehouse.orderpoint']
        nombres = {'qty_to_order'}
        candidatos = [f for f in Orderpoint._fields.values() if f.store and f.compute]
        agregado = True
        while agregado:
            agregado = False
            for campo in candidatos:
                if campo.name in nombres:
                    continue
                depende = {d.split('.')[0] for d in campo.get_depends(Orderpoint)[0]}
                if depende & nombres:
                    nombres.add(campo.name)
                    agregado = True
        return [Orderpoint._fields[n] for n in sorted(nombres)]

    def _crossdock_encolar_reorden(self):
        """Encola el recálculo diferido de los puntos de reorden de estas OC."""
        ordenes = self.filtered('crossdock_enabled')
        if ordenes and ordenes._crossdock_diferir_puntos_reorden():
            self.env['crossdock.reorden.pendiente'].sudo()._encolar(
                ordenes.order_line.product_id.product_tmpl_id, ordenes)

    def _crossdock_puntos_reorden_a_diferir(self, estados=('draft', 'sent')):
        """Puntos de reorden que la confirmación (o el armado) de estas OC haría recalcular.

        Son los de TODAS las variantes de las plantillas compradas, no sólo los de las
        variantes de la OC: agregar el proveedor a la ficha cambia la lista de proveedores
        de la plantilla, de la que depende `qty_to_order`.
        """
        ordenes = self.filtered(lambda o: o.crossdock_enabled and o.state in estados)
        if not ordenes or not ordenes._crossdock_diferir_puntos_reorden():
            return self.env['stock.warehouse.orderpoint']
        plantillas = ordenes.order_line.product_id.product_tmpl_id
        if not plantillas:
            return self.env['stock.warehouse.orderpoint']
        # Con los archivados: el core recalcula los campos almacenados de cualquier registro,
        # activo o no (en prod hay ≈ 85 mil puntos de reorden archivados).
        return self.env['stock.warehouse.orderpoint'].sudo().with_context(active_test=False).search([
            ('product_id.product_tmpl_id', 'in', plantillas.ids),
        ])


class CrossdockReordenPendiente(models.Model):
    _name = 'crossdock.reorden.pendiente'
    _description = 'Recálculo diferido de puntos de reorden (crossdock)'
    _order = 'id'

    product_tmpl_id = fields.Many2one(
        'product.template', string="Plantilla", required=True, ondelete='cascade', index=True)
    order_id = fields.Many2one(
        'purchase.order', string="Orden de compra", ondelete='set null')
    ultimo_orderpoint_id = fields.Integer(
        string="Último punto de reorden recalculado", default=0,
        help="Avance del recálculo: se retoma desde el siguiente id si una corrida se corta.")
    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('hecho', 'Hecho'),
    ], default='pendiente', required=True, index=True)

    @api.model
    def _encolar(self, plantillas, ordenes=None):
        """Encola el recálculo de los puntos de reorden de estas plantillas.

        Si la plantilla ya estaba pendiente se reinicia desde el principio: lo que cambió
        ahora puede afectar puntos de reorden que la corrida anterior ya había recalculado.
        """
        if not plantillas:
            return self
        orden = ordenes[:1] if ordenes else self.env['purchase.order']
        existentes = self.search([
            ('estado', '=', 'pendiente'), ('product_tmpl_id', 'in', plantillas.ids)])
        existentes.write({'ultimo_orderpoint_id': 0})
        nuevas = plantillas - existentes.product_tmpl_id
        self.create([{'product_tmpl_id': p.id, 'order_id': orden.id} for p in nuevas])
        cron = self.env.ref('automatic_crossdocking.ir_cron_recalcular_reorden_crossdock',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return True

    def _recalcular_lote(self, limite=LOTE_PUNTOS_REORDEN):
        """Recalcula el próximo lote de puntos de reorden de esta plantilla.

        Devuelve True si la plantilla terminó.
        """
        self.ensure_one()
        Orderpoint = self.env['stock.warehouse.orderpoint'].sudo().with_context(active_test=False)
        puntos = Orderpoint.search([
            ('product_id.product_tmpl_id', '=', self.product_tmpl_id.id),
            ('id', '>', self.ultimo_orderpoint_id),
        ], order='id', limit=limite)
        if not puntos:
            self.estado = 'hecho'
            return True
        campos = self.env['purchase.order']._crossdock_campos_reorden()
        for campo in campos:
            self.env.add_to_compute(campo, puntos)
        puntos.flush_recordset([campo.name for campo in campos])
        self.ultimo_orderpoint_id = puntos[-1].id
        if len(puntos) < limite:
            self.estado = 'hecho'
            return True
        return False

    @api.model
    def _cron_recalcular(self):
        inicio = time.time()
        while time.time() - inicio < SEGUNDOS_POR_CORRIDA:
            pendiente = self.search([('estado', '=', 'pendiente')], limit=1)
            if not pendiente:
                return True
            pendiente._recalcular_lote()
            if not _en_test():
                self.env.cr.commit()
        # Se acabó el presupuesto de la corrida: seguir en la próxima, enseguida.
        if self.search_count([('estado', '=', 'pendiente')], limit=1):
            cron = self.env.ref('automatic_crossdocking.ir_cron_recalcular_reorden_crossdock',
                                raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
        return True

    @api.autovacuum
    def _gc_hechos(self):
        self.search([
            ('estado', '=', 'hecho'),
            ('write_date', '<', fields.Datetime.subtract(fields.Datetime.now(), days=7)),
        ]).unlink()


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _push_apply(self):
        """En el armado del crossdock, no buscar regla push move por move si no hay ninguna.

        Los moves encadenados no se empujan (el core los saltea por tener destino), pero los
        del último eslabón —la recepción de cada sucursal— sí pasan por la búsqueda de regla,
        una por move: ≈ 30 s en una OC de 500 líneas. Si ninguna regla push sale de esas
        ubicaciones, el resultado es vacío de antemano.
        """
        if self and self.env.context.get('crossdock_armado'):
            hay_reglas = self.env['stock.rule'].sudo().search_count([
                ('location_src_id', 'in', self.location_dest_id.ids),
                ('action', 'in', ('push', 'pull_push')),
            ], limit=1)
            if not hay_reglas:
                return self.env['stock.move']
        return super()._push_apply()

    def _trigger_scheduler(self):
        """En el armado del crossdock, no buscar regla por regla si no hay ninguna automática.

        El core hace una búsqueda de puntos de reorden con disparo 'auto' por cada move
        confirmado: en una OC de 250 líneas son 13.532 búsquedas (≈ 55 s). Si para estos
        productos no existe ninguno, no hay nada que disparar.
        """
        if self and self.env.context.get('crossdock_armado'):
            hay_automaticos = self.env['stock.warehouse.orderpoint'].sudo().search_count([
                ('trigger', '=', 'auto'),
                ('product_id', 'in', self.product_id.ids),
            ], limit=1)
            if not hay_automaticos:
                return
        return super()._trigger_scheduler()

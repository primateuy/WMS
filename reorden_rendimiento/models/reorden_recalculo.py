# -*- coding: utf-8 -*-
"""Recálculo diferido de la «cantidad a pedir» de las reglas de reabastecimiento.

`qty_to_order` es un campo almacenado que Odoo recalcula cuando cambian las líneas de
compra del producto, los proveedores de la plantilla o los parámetros de la regla. Con
≈ 26 reglas por variante (más las archivadas, que también se recalculan), una operación
grande —cargar o confirmar una OC de cientos de líneas, cambiar el múltiplo de una
plantilla grande, actualizar las reglas de un grupo— recalcula decenas de miles de reglas
dentro de la petición del usuario y se corta por tiempo.

`diferir()` protege esas reglas mientras corre la operación y, al terminar, las deja en
cola para que un cron las recalcule por lotes, guardando el avance. Por debajo de un
umbral no hace nada: las operaciones chicas recalculan en el momento, como siempre.
"""
import contextlib
import logging
import threading
import time

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

LOTE = 1000
# Presupuesto por corrida de cron, holgado respecto de los 120 s de CPU / 240 s de reloj.
SEGUNDOS_POR_CORRIDA = 90
UMBRAL_POR_DEFECTO = 300


def _en_test():
    return getattr(threading.current_thread(), 'testing', False)


class ReordenRecalculoPendiente(models.Model):
    _name = 'reorden.recalculo.pendiente'
    _description = 'Recálculo diferido de la cantidad a pedir'
    _order = 'id'

    product_tmpl_id = fields.Many2one(
        'product.template', string="Plantilla", ondelete='cascade', index=True,
        help="Recalcular las reglas de todas las variantes de la plantilla.")
    product_id = fields.Many2one(
        'product.product', string="Variante", ondelete='cascade', index=True,
        help="Recalcular sólo las reglas de esta variante.")
    origen = fields.Char(string="Origen")
    ultimo_orderpoint_id = fields.Integer(
        string="Última regla recalculada", default=0,
        help="Avance: se retoma desde la regla siguiente si una corrida se corta.")
    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('hecho', 'Hecho'),
    ], default='pendiente', required=True, index=True)

    # ------------------------------------------------------------------
    # Qué se protege
    # ------------------------------------------------------------------
    @api.model
    def _campos(self):
        """`qty_to_order` y los campos almacenados del mismo modelo que dependen de él.

        Si sólo se protegiera `qty_to_order`, un dependiente como `forum_demand_effective`
        se recalcularía igual y en el camino calcularía `qty_to_order` regla por regla.
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

    @api.model
    def _dominio(self, productos=None, plantillas=None):
        dominio = []
        if productos:
            dominio = [('product_id', 'in', productos.ids)]
        if plantillas:
            parte = [('product_id.product_tmpl_id', 'in', plantillas.ids)]
            dominio = ['|'] + dominio + parte if dominio else parte
        return dominio

    @api.model
    def _puntos(self, productos=None, plantillas=None):
        """Reglas de esos productos o plantillas, ARCHIVADAS INCLUIDAS: el core recalcula
        los campos almacenados de cualquier registro, activo o no."""
        dominio = self._dominio(productos, plantillas)
        if not dominio:
            return self.env['stock.warehouse.orderpoint']
        return self.env['stock.warehouse.orderpoint'].sudo().with_context(
            active_test=False).search(dominio)

    @api.model
    def _umbral(self):
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param(
                'reorden_rendimiento.umbral', UMBRAL_POR_DEFECTO))
        except (TypeError, ValueError):
            return UMBRAL_POR_DEFECTO

    @api.model
    def _activo(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'reorden_rendimiento.diferir', '1')
        return param not in ('0', 'False', 'false')

    @contextlib.contextmanager
    def diferir(self, productos=None, plantillas=None, encolar=True, origen=''):
        """Protege las reglas de `productos`/`plantillas` durante el bloque.

        Devuelve (con `as`) True si difirió. Con `encolar=False` el que llama decide cuándo
        encolar (p. ej. el armado del crossdock, que lo hace al terminar el armado).
        """
        if self.env.context.get('reorden_no_diferir') or not self._activo():
            yield False
            return
        puntos = self._puntos(productos, plantillas)
        if len(puntos) < self._umbral():
            yield False
            return
        with self.env.protecting(self._campos(), puntos):
            yield True
        if encolar:
            self._encolar(productos=productos, plantillas=plantillas, origen=origen)

    # ------------------------------------------------------------------
    # Cola
    # ------------------------------------------------------------------
    @api.model
    def _encolar(self, productos=None, plantillas=None, origen=''):
        """Encola el recálculo. Lo ya pendiente se reinicia: lo que cambió ahora puede
        afectar reglas que la corrida anterior ya había recalculado."""
        Self = self.sudo()
        plantillas = plantillas or self.env['product.template']
        # Las variantes de plantillas ya encoladas enteras no hace falta encolarlas aparte.
        productos = (productos or self.env['product.product']).filtered(
            lambda p: p.product_tmpl_id not in plantillas)
        if not productos and not plantillas:
            return Self.browse()
        # Y al revés: lo que ya está pendiente por plantilla cubre a sus variantes, y una
        # plantilla que entra ahora deja de lado las variantes que estaban esperando (la
        # confirmación de una OC encola por plantilla lo que las líneas encolaron por variante:
        # sin esto, esas reglas se recalculaban dos veces).
        if productos:
            con_plantilla = Self.search([('estado', '=', 'pendiente'),
                                         ('product_tmpl_id', 'in', productos.product_tmpl_id.ids)])
            productos = productos.filtered(
                lambda p: p.product_tmpl_id not in con_plantilla.product_tmpl_id)
        if plantillas:
            Self.search([('estado', '=', 'pendiente'),
                         ('product_id.product_tmpl_id', 'in', plantillas.ids)]).write(
                {'estado': 'hecho'})
        if not productos and not plantillas:
            return Self.browse()
        existentes = Self.search([
            ('estado', '=', 'pendiente'), '|',
            ('product_tmpl_id', 'in', plantillas.ids), ('product_id', 'in', productos.ids)])
        existentes.write({'ultimo_orderpoint_id': 0})
        nuevas_t = plantillas - existentes.product_tmpl_id
        nuevas_p = productos - existentes.product_id
        nuevas = Self.create(
            [{'product_tmpl_id': t.id, 'origen': origen} for t in nuevas_t]
            + [{'product_id': p.id, 'origen': origen} for p in nuevas_p])
        cron = self.env.ref('reorden_rendimiento.ir_cron_reorden_recalculo', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return existentes | nuevas

    def _recalcular_lote(self, limite=LOTE):
        """Recalcula juntas hasta `limite` reglas de estas entradas, en orden.

        Se juntan varias entradas en un mismo cálculo: el costo está en cada pasada (leer el
        pronóstico y lo que está en curso, agrupado por contexto), no en cada regla. Una OC de
        1000 líneas encola 1000 variantes de ≈ 30 reglas: de a una eran 1000 pasadas.
        """
        Orderpoint = self.env['stock.warehouse.orderpoint'].sudo().with_context(active_test=False)
        ids, terminadas = [], self.browse()
        for entrada in self:
            restante = limite - len(ids)
            if restante <= 0:
                break
            if entrada.product_tmpl_id:
                dominio = [('product_id.product_tmpl_id', '=', entrada.product_tmpl_id.id)]
            elif entrada.product_id:
                dominio = [('product_id', '=', entrada.product_id.id)]
            else:
                terminadas |= entrada
                continue
            puntos = Orderpoint.search(dominio + [('id', '>', entrada.ultimo_orderpoint_id)],
                                       order='id', limit=restante)
            ids.extend(puntos.ids)
            if puntos:
                entrada.ultimo_orderpoint_id = puntos[-1].id
            if len(puntos) < restante:
                terminadas |= entrada
        if ids:
            puntos = Orderpoint.browse(ids)
            campos = self._campos()
            for campo in campos:
                self.env.add_to_compute(campo, puntos)
            puntos.flush_recordset([campo.name for campo in campos])
        terminadas.write({'estado': 'hecho'})
        return len(ids)

    @api.model
    def _cron_recalcular(self):
        inicio = time.time()
        while time.time() - inicio < SEGUNDOS_POR_CORRIDA:
            pendientes = self.search([('estado', '=', 'pendiente')], limit=LOTE)
            if not pendientes:
                return True
            pendientes._recalcular_lote()
            if not _en_test():
                self.env.cr.commit()
        if self.search_count([('estado', '=', 'pendiente')], limit=1):
            cron = self.env.ref('reorden_rendimiento.ir_cron_reorden_recalculo',
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

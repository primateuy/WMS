# -*- coding: utf-8 -*-
"""Cola de envíos de pickings a WIS.

Los pickings que se arman en lote —el crossdock de una OC, decenas de pedidos por vez— no
se envían a WIS dentro de la transacción que los crea: se les asigna el código WIS (lo genera
Odoo, es determinístico), quedan `wms_estado='en_cola'` y este cron los envía de a uno,
grabando cada envío por separado. Así:

* la espera por WIS (≈ 1-2 s por pedido, minutos en una OC grande) no la paga el usuario;
* si algo se corta, lo ya enviado queda grabado y lo pendiente se reintenta, sin pedidos
  duplicados en WIS: el reintento usa el mismo código.
"""
import logging
import threading
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Presupuesto por corrida, holgado respecto de los 120 s de CPU / 240 s de reloj del servidor
# y del timeout de 30 s de cada llamada a WIS.
SEGUNDOS_POR_CORRIDA = 120


class WisPickingCola(models.Model):
    _name = 'wis.picking.cola'
    _description = 'Cola de envíos de pickings a WIS'
    _order = 'id'
    _rec_name = 'picking_id'

    picking_id = fields.Many2one(
        'stock.picking', string="Operación", required=True, ondelete='cascade', index=True)
    codigo_unico = fields.Char(related='picking_id.codigo_unico', string="Código WIS")
    origin = fields.Char(related='picking_id.origin', string="Origen")
    tipo = fields.Char(string="Tipo WIS", required=True)
    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('enviado', 'Enviado'),
        ('error', 'Error'),
        ('cancelado', 'Cancelado'),
    ], default='pendiente', required=True, index=True)
    intentos = fields.Integer(default=0, readonly=True)
    ultimo_error = fields.Text(readonly=True)
    fecha_envio = fields.Datetime(string="Fecha de envío", readonly=True)
    company_id = fields.Many2one(
        'res.company', related='picking_id.company_id', store=True, index=True)

    # ------------------------------------------------------------------
    # Encolado
    # ------------------------------------------------------------------
    @api.model
    def _encolar(self, picking, tipo):
        existente = self.search([
            ('picking_id', '=', picking.id), ('estado', '=', 'pendiente')], limit=1)
        if existente:
            return existente
        entrada = self.create({'picking_id': picking.id, 'tipo': tipo})
        self._disparar_cron()
        return entrada

    @api.model
    def _cancelar_de_pickings(self, pickings):
        self.search([
            ('picking_id', 'in', pickings.ids), ('estado', 'in', ('pendiente', 'error')),
        ]).write({'estado': 'cancelado'})

    @api.model
    def _disparar_cron(self):
        cron = self.env.ref('integracion_wis.ir_cron_wis_picking_cola', raise_if_not_found=False)
        if not cron:
            return
        try:
            cron.sudo()._trigger()
        except Exception as e:
            _logger.warning("[WIS] No se pudo disparar el cron de la cola de pickings: %s", e)

    # ------------------------------------------------------------------
    # Envío
    # ------------------------------------------------------------------
    def _enviar(self):
        """Envía esta entrada a WIS. No lanza: el resultado queda en la entrada."""
        self.ensure_one()
        picking = self.picking_id
        if picking.state == 'cancel':
            self.write({'estado': 'cancelado'})
            return False

        # Productos que todavía se están integrando (cola de productos): se espera sin gastar
        # intentos. Mandar ahora haría fallar el envío por «producto sin código WMS».
        sin_codigo = picking.move_ids.product_id.filtered(lambda p: not p.codigo_unico)
        if sin_codigo and self.env['wis.sync.queue'].search_count([
                ('product_id', 'in', sin_codigo.ids),
                ('estado', 'in', ('pendiente', 'procesando'))], limit=1):
            self.ultimo_error = _("Esperando la integración de %s producto(s) con WIS.") % len(sin_codigo)
            return False

        config = self.env['integracion_wis.integracion_wis']._get_config()
        max_intentos = (config.cola_max_intentos if config else 0) or 3
        codigo = picking.codigo_unico or picking._wis_codigo_esperado(self.tipo)
        try:
            with self.env.cr.savepoint():
                response = picking.with_context(
                    wis_envio_desde_cola=True, wis_encolar_envios=False,
                ).enviarWS(self.tipo)
                if response is False:
                    raise UserError(_("WIS no aceptó el envío (la operación no está en un "
                                      "estado que se pueda informar)."))
                wms_vals = {'wms_estado': 'enviado', 'codigo_unico': codigo}
                if isinstance(response, dict):
                    wms_vals['idPedidoWMS'] = response.get('numeroInterfaz', '')
                    wms_vals['codigo_unico'] = response.get('codigoUnico') or codigo
                picking.with_context(skip_wms_integration=True).write(wms_vals)
        except Exception as e:
            intentos = self.intentos + 1
            agotado = intentos >= max_intentos
            self.write({
                'intentos': intentos,
                'ultimo_error': str(e),
                'estado': 'error' if agotado else 'pendiente',
            })
            if agotado:
                # Mismo criterio que los envíos directos: el código queda y la operación vuelve
                # a 'sin_enviar'; se reintenta a mano desde la cola.
                picking.with_context(skip_wms_integration=True).write({'wms_estado': 'sin_enviar'})
            picking._wis_registrar_envio(codigo, self.tipo, ok=False, error=str(e))
            _logger.warning("[WIS] Cola de pickings: %s falló (intento %s/%s): %s",
                            picking.name, intentos, max_intentos, e)
            return False

        self.write({'estado': 'enviado', 'fecha_envio': fields.Datetime.now(), 'ultimo_error': False})
        picking._wis_registrar_envio(picking.codigo_unico, self.tipo)
        return True

    @api.model
    def _cron_procesar(self):
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            _logger.info("[WIS] Cola de pickings omitida: comunicación deshabilitada.")
            return True
        inicio = time.time()
        en_test = getattr(threading.current_thread(), 'testing', False)
        # Lo que falla en esta corrida se reintenta en la próxima, no enseguida: si WIS está
        # caído, reintentar en el acto agota los intentos en segundos.
        intentadas = []
        while time.time() - inicio < SEGUNDOS_POR_CORRIDA:
            entrada = self.search(
                [('estado', '=', 'pendiente'), ('id', 'not in', intentadas)], limit=1)
            if not entrada:
                break
            intentadas.append(entrada.id)
            entrada._enviar()
            if not en_test:
                self.env.cr.commit()
        else:
            # Se acabó el presupuesto de la corrida: seguir enseguida con lo que falta.
            self._disparar_cron()
        return True

    # ------------------------------------------------------------------
    # Acciones de la vista
    # ------------------------------------------------------------------
    def action_reintentar(self):
        entradas = self.filtered(lambda e: e.estado in ('error', 'cancelado')
                                 and e.picking_id.state != 'cancel')
        entradas.write({'estado': 'pendiente', 'intentos': 0, 'ultimo_error': False})
        entradas.picking_id.filtered(lambda p: p.wms_estado == 'sin_enviar').with_context(
            skip_wms_integration=True).write({'wms_estado': 'en_cola'})
        if entradas:
            self._disparar_cron()
        return True

    def action_enviar_ahora(self):
        if not self:
            raise UserError(_("No hay entradas seleccionadas."))
        for entrada in self.filtered(lambda e: e.estado == 'pendiente'):
            entrada._enviar()
        return True

    def action_cancelar(self):
        entradas = self.filtered(lambda e: e.estado in ('pendiente', 'error'))
        entradas.write({'estado': 'cancelado'})
        # La operación deja de esperar en la cola: vuelve al estado de «no enviada».
        entradas.picking_id.filtered(lambda p: p.wms_estado == 'en_cola').with_context(
            skip_wms_integration=True).write({'wms_estado': 'sin_enviar'})
        return True

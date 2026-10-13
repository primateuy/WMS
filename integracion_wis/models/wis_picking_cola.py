# -*- coding: utf-8 -*-
"""Cola de envíos de pickings a WIS.

Los pickings que se arman en lote —el crossdock de una OC, decenas de pedidos por vez— no
se envían a WIS dentro de la transacción que los crea: se les asigna el código WIS (lo genera
Odoo, es determinístico), quedan `wms_estado='en_cola'` y este cron los envía de a uno,
grabando cada envío por separado. Así:

* la espera por WIS (≈ 1-2 s por pedido, minutos en una OC grande) no la paga el usuario;
* si algo se corta, lo ya enviado queda grabado y lo pendiente se reintenta, sin pedidos
  duplicados en WIS: el reintento usa el mismo código.

🔴 **Una operación no se le vuelve a mandar a WIS sin motivo** (12-oct-2026: WIS reportó
reintentos «infinitos»; en support una recepción de 1000 líneas llegó 16 veces en 25 min):

* **El cron es el único que envía.** «Enviar ahora» la pone primera en la fila. Antes
  enviaba en la petición del usuario, a la par del cron: al grabar chocaban («could not
  serialize») y Odoo repetía la petición entera, envío a WIS incluido.
* **La entrada se marca «procesando» y se confirma ANTES de llamar a WIS**, así nada que se
  caiga después puede volver a mandarla.
* **Según el error:**
  - *Rechazo de WIS* (400, 500 con LogID): reenviar el mismo dato no lo arregla. Queda con
    error y sale sólo con «Reintentar» (salvo «Reintentar solos los rechazos de WIS»).
  - *Timeout*: WIS lo recibió y lo sigue procesando. Queda «sin confirmar» y no se reenvía
    hasta pasada la espera configurada (30 min). Cuenta como intento.
  - *WIS sin atender* (conexión, 502/503/504): no cuenta como intento.
  En los dos últimos casos además **se pausan los envíos a WIS** (5, 15, 60 min) y se corta
  la corrida: seguir con la siguiente operación sólo le suma carga a un WIS que no responde.
"""
import logging
import threading
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .models import WisErrorGeneral, WisTimeout, log_id_wis

_logger = logging.getLogger(__name__)

PRIORIDAD_ENVIAR_AHORA = 0
PRIORIDAD_NORMAL = 10
# Espera entre reintentos automáticos de un rechazo de WIS (si están habilitados).
ESPERA_REINTENTO = timedelta(minutes=15)
# «Procesando» más vieja que esto quedó de una corrida que se cortó a mitad del envío.
PROCESANDO_COLGADA = timedelta(minutes=15)
MAX_INTENTOS_POR_DEFECTO = 5


def _en_test():
    return getattr(threading.current_thread(), 'testing', False)


class WisPickingCola(models.Model):
    _name = 'wis.picking.cola'
    _description = 'Cola de envíos de pickings a WIS'
    _order = 'prioridad, id'
    _rec_name = 'picking_id'

    picking_id = fields.Many2one(
        'stock.picking', string="Operación", required=True, ondelete='cascade', index=True)
    codigo_unico = fields.Char(related='picking_id.codigo_unico', string="Código WIS")
    origin = fields.Char(related='picking_id.origin', string="Origen")
    tipo = fields.Char(string="Tipo WIS", required=True)
    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('procesando', 'Enviando'),
        ('enviado', 'Enviado'),
        ('error', 'Error'),
        ('cancelado', 'Cancelado'),
    ], default='pendiente', required=True, index=True)
    tipo_error = fields.Selection([
        ('datos', 'Rechazada por WIS'),
        ('sin_confirmar', 'Sin confirmar (timeout)'),
        ('transitorio', 'WIS no atendió'),
        ('espera', 'Esperando productos'),
    ], string="Motivo", readonly=True, copy=False,
        help="Rechazada por WIS: error en los datos, no se reenvía sola. "
             "Sin confirmar: WIS no contestó a tiempo y probablemente la siga procesando; "
             "se reenvía pasada la espera. WIS no atendió: conexión o servicio caído, se "
             "reintenta cuando termina la pausa. Esperando productos: hay productos de la "
             "operación que todavía se están integrando.")
    prioridad = fields.Integer(
        default=PRIORIDAD_NORMAL, index=True,
        help="Menor número, sale antes. 0 = pedida con «Enviar ahora».")
    intentos = fields.Integer(default=0, readonly=True)
    proximo_intento = fields.Datetime(
        string="Próximo intento", readonly=True, copy=False,
        help="Antes de esta hora la cola no vuelve a mandarla.")
    ultimo_error = fields.Text(readonly=True)
    log_id = fields.Char(
        string="LogID de WIS", readonly=True, copy=False,
        help="Identificador que WIS adjunta a sus errores no controlados. Es lo que hay "
             "que pasarle a WIS para que revise el error.")
    fecha_envio = fields.Datetime(string="Fecha de envío", readonly=True)
    company_id = fields.Many2one(
        'res.company', related='picking_id.company_id', store=True, index=True)

    # ------------------------------------------------------------------
    # Encolado
    # ------------------------------------------------------------------
    @api.model
    def _encolar(self, picking, tipo):
        existente = self.search([
            ('picking_id', '=', picking.id), ('estado', 'in', ('pendiente', 'procesando'))],
            limit=1)
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
    def _confirmar_por_notificacion(self, pickings, evento):
        """WIS notificó algo sobre estos pickings: los tiene, aunque Odoo no lo haya podido
        confirmar (timeout) o se haya rendido después de varios intentos.

        Se da por enviada la entrada de la cola, para que no se reenvíe (WIS rechaza o duplica
        lo que ya recibió), y el picking pasa a 'enviado' para que la notificación se procese.
        Caso real: support, 12-10-2026, seis recepciones de importación quedaron en
        «sin enviar» tras los timeouts y WIS las tenía todas. Sólo cubre pickings que pasaron
        por la cola: un 'sin_enviar' que nunca se mandó no se toca.
        """
        entradas = self.search([
            ('picking_id', 'in', pickings.ids), ('estado', 'in', ('pendiente', 'error')),
        ])
        if not entradas:
            return self.browse()
        entradas.write({
            'estado': 'enviado',
            'tipo_error': False,
            'proximo_intento': False,
            'fecha_envio': fields.Datetime.now(),
            'ultimo_error': _("WIS lo notificó (%s): lo tiene aunque el envío no se había "
                              "podido confirmar.") % evento,
        })
        sin_confirmar = entradas.picking_id.filtered(
            lambda p: p.wms_estado in ('en_cola', 'sin_enviar'))
        sin_confirmar.with_context(skip_wms_integration=True).write({'wms_estado': 'enviado'})
        for entrada in entradas:
            self.env['wms.integracion.log'].sudo().create({
                'fecha': fields.Datetime.now(),
                'nivel': 'info',
                'modelo': 'stock.picking',
                'texto': _("%(picking)s: WIS lo notificó (%(evento)s); se da por enviado y no "
                           "se reenvía.", picking=entrada.picking_id.name, evento=evento),
                'picking_id': entrada.picking_id.id,
                'resultado': 'exito',
                'codigo_unico': entrada.picking_id.codigo_unico,
            })
        return entradas

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
    @api.model
    def _max_intentos(self, config):
        return (config.cola_max_intentos if config else 0) or MAX_INTENTOS_POR_DEFECTO

    def _enviar(self):
        """Envía esta entrada a WIS. No lanza: el resultado queda en la entrada.

        Devuelve True si salió, 'general' si WIS no respondió (timeout o sin atender:
        la corrida tiene que cortar) y False en cualquier otro caso.
        """
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
            self.write({
                'estado': 'pendiente',
                'tipo_error': 'espera',
                'ultimo_error': _("Esperando la integración de %s producto(s) con WIS.")
                % len(sin_codigo),
            })
            return False

        config = self.env['integracion_wis.integracion_wis']._get_config()
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
        except WisTimeout as e:
            self._registrar_falla(e, config, 'sin_confirmar', cuenta=True)
            config._wis_pausar(str(e))
            return 'general'
        except WisErrorGeneral as e:
            self._registrar_falla(e, config, 'transitorio', cuenta=False)
            config._wis_pausar(str(e))
            return 'general'
        except Exception as e:
            self._registrar_falla(e, config, 'datos', cuenta=True)
            return False

        self.write({
            'estado': 'enviado', 'fecha_envio': fields.Datetime.now(), 'ultimo_error': False,
            'tipo_error': False, 'proximo_intento': False, 'log_id': False,
        })
        picking._wis_registrar_envio(picking.codigo_unico, self.tipo)
        config._wis_levantar_pausa()
        return True

    def _registrar_falla(self, error, config, tipo_error, cuenta):
        """Deja la entrada como corresponde a la falla y la operación en su estado WIS."""
        self.ensure_one()
        picking = self.picking_id
        texto = str(error)
        ahora = fields.Datetime.now()
        intentos = self.intentos + (1 if cuenta else 0)
        maximo = self._max_intentos(config)

        if tipo_error == 'sin_confirmar':
            espera = timedelta(minutes=(config.espera_timeout_minutos if config else 0) or 30)
            agotado = intentos >= maximo
            proximo = ahora + espera
        elif tipo_error == 'transitorio':
            # No cuenta como intento: WIS no lo recibió. La pausa general marca cuándo.
            agotado = False
            proximo = False
        else:
            reintenta = bool(config and config.reintentar_errores_datos) and intentos < maximo
            agotado = not reintenta
            proximo = ahora + ESPERA_REINTENTO if reintenta else False

        self.write({
            'estado': 'error' if agotado else 'pendiente',
            'tipo_error': tipo_error,
            'intentos': intentos,
            'ultimo_error': texto,
            'log_id': log_id_wis(texto),
            'proximo_intento': False if agotado else proximo,
            # Lo pedido con «Enviar ahora» ya salió: si vuelve a la fila, va en su lugar.
            'prioridad': PRIORIDAD_NORMAL,
        })
        if agotado:
            # El código queda y la operación vuelve a 'sin_enviar'; se reintenta a mano.
            picking.with_context(skip_wms_integration=True).write({'wms_estado': 'sin_enviar'})
        picking._wis_registrar_envio(
            picking.codigo_unico or picking._wis_codigo_esperado(self.tipo),
            self.tipo, ok=False, error=texto)
        _logger.warning("[WIS] Cola de pickings: %s falló (%s, intento %s/%s%s): %s",
                        picking.name, tipo_error, intentos, maximo,
                        ", queda con error" if agotado else "", texto)

    @api.model
    def _recuperar_colgadas(self):
        """«Procesando» de una corrida que se cortó a mitad del envío: WIS pudo haberla
        recibido, así que se trata como un timeout y espera antes de reenviarse."""
        colgadas = self.search([
            ('estado', '=', 'procesando'),
            ('write_date', '<', fields.Datetime.now() - PROCESANDO_COLGADA),
        ])
        if not colgadas:
            return
        config = self.env['integracion_wis.integracion_wis']._get_config()
        espera = timedelta(minutes=(config.espera_timeout_minutos if config else 0) or 30)
        _logger.warning("[WIS] Cola de pickings: %d envíos quedaron a medias de una corrida "
                        "cortada; se reintentan pasada la espera.", len(colgadas))
        colgadas.write({
            'estado': 'pendiente',
            'tipo_error': 'sin_confirmar',
            'ultimo_error': _("El envío quedó a medias (la corrida se cortó). WIS pudo "
                              "haberlo recibido."),
            'proximo_intento': fields.Datetime.now() + espera,
        })

    @api.model
    def _siguiente(self, excluir):
        ahora = fields.Datetime.now()
        return self.search([
            ('estado', '=', 'pendiente'), ('id', 'not in', excluir),
            '|', ('proximo_intento', '=', False), ('proximo_intento', '<=', ahora),
        ], order='prioridad, id', limit=1)

    @api.model
    def _cron_procesar(self):
        Config = self.env['integracion_wis.integracion_wis']
        if not Config._comunicacion_habilitada():
            _logger.info("[WIS] Cola de pickings omitida: comunicación deshabilitada.")
            return True
        config = Config._get_config()
        self._recuperar_colgadas()
        if not _en_test():
            self.env.cr.commit()
        if config._wis_en_pausa():
            _logger.info("[WIS] Cola de pickings en espera: envíos a WIS en pausa hasta %s.",
                         config.pausa_hasta)
            return True

        inicio = time.time()
        presupuesto = Config._presupuesto_corrida(operaciones=True)
        # Las que esperan productos no tienen próximo intento: se saltean en esta corrida.
        intentadas = []
        while time.time() - inicio < presupuesto:
            entrada = self._siguiente(intentadas)
            if not entrada:
                break
            intentadas.append(entrada.id)
            entrada.write({'estado': 'procesando'})
            if not _en_test():
                # Confirmado antes de llamar a WIS: nada que falle después puede volver a
                # mandarla, y nadie más la toma mientras va en camino.
                self.env.cr.commit()
            resultado = entrada._enviar()
            if not _en_test():
                self.env.cr.commit()
            if resultado == 'general':
                # WIS no responde: seguir con la siguiente sólo le suma carga.
                return True
        else:
            # Se acabó el presupuesto de la corrida: seguir enseguida con lo que falta.
            self._disparar_cron()
        return True

    # ------------------------------------------------------------------
    # Acciones de la vista
    # ------------------------------------------------------------------
    def _volver_a_la_fila(self, prioridad=PRIORIDAD_NORMAL):
        entradas = self.filtered(
            lambda e: (e.estado in ('error', 'cancelado') or e.estado == 'pendiente')
            and e.picking_id.state != 'cancel')
        for entrada in entradas:
            vals = {'estado': 'pendiente', 'proximo_intento': False, 'prioridad': prioridad}
            if entrada.estado != 'pendiente':
                vals['intentos'] = 0
            entrada.write(vals)
        entradas.picking_id.filtered(lambda p: p.wms_estado == 'sin_enviar').with_context(
            skip_wms_integration=True).write({'wms_estado': 'en_cola'})
        if entradas:
            self._disparar_cron()
        return entradas

    def action_reintentar(self):
        """Vuelve a poner en la fila lo que falló o se canceló, con los intentos en cero.
        También adelanta un reintento que estaba esperando (timeout, rechazo)."""
        self._volver_a_la_fila()
        return True

    def action_enviar_ahora(self):
        """La pone primera en la fila y despierta al cron. No envía en esta petición:
        dos procesos mandando lo mismo era una de las causas de los envíos repetidos."""
        if not self:
            raise UserError(_("No hay entradas seleccionadas."))
        entradas = self._volver_a_la_fila(PRIORIDAD_ENVIAR_AHORA)
        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not entradas:
            mensaje, tipo = _("No hay nada para enviar en lo seleccionado."), 'warning'
        elif config._wis_en_pausa():
            mensaje, tipo = _("%s operación(es) primeras en la fila. Los envíos a WIS están en "
                              "pausa hasta %s porque WIS no respondió; salen ahí.") % (
                len(entradas), fields.Datetime.context_timestamp(
                    self, config.pausa_hasta).strftime('%H:%M')), 'warning'
        else:
            mensaje, tipo = _("%s operación(es) primeras en la fila: salen en unos "
                              "segundos.") % len(entradas), 'info'
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("Cola de operaciones"), 'message': mensaje, 'type': tipo,
                       'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    def action_cancelar(self):
        entradas = self.filtered(lambda e: e.estado in ('pendiente', 'error'))
        entradas.write({'estado': 'cancelado'})
        # La operación deja de esperar en la cola: vuelve al estado de «no enviada».
        entradas.picking_id.filtered(lambda p: p.wms_estado == 'en_cola').with_context(
            skip_wms_integration=True).write({'wms_estado': 'sin_enviar'})
        return True

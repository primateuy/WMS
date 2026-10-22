# -*- coding: utf-8 -*-
"""Notificaciones de WIS procesadas en segundo plano.

WIS manda la preparación y el despacho de decenas de pedidos en una sola notificación, y a
7-8 segundos por pedido (validar, emitir el e-Remito) una de 168 pedidos tarda unos 20 minutos.
Procesada dentro de la petición, el servidor la cortaba a los 10 (`limit_time_real`), WIS no
recibía respuesta y la reenviaba, y Odoo volvía a aplicar lo que ya había aplicado: 88 e-Remitos
duplicados en producción el 21 y 22-10-2026.

Ahora:

* **El webhook sólo guarda y responde.** La notificación queda acá y un cron la procesa.
* **Un reenvío no se vuelve a aplicar.** El número de interfaz (`NumeroInterfazEjecucion`) es
  único por tipo; si llega de nuevo se contesta OK y no se procesa otra vez. Además cada
  contenedor de una preparación se aplica una sola vez por pedido (`wis.preparacion.aplicada`),
  aunque venga en otra notificación.
* **Pedido por pedido, cada uno en su transacción.** La corrida respeta el corte del servidor:
  si no le alcanza el tiempo, graba lo hecho y sigue en la corrida siguiente.
* **El despacho avanza la cadena una vez por notificación**, con todos los pedidos del camión
  juntos (una caja compartida se mueve una sola vez).
"""
import json
import logging
import threading
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.http import _request_stack
from psycopg2.errors import UniqueViolation

_logger = logging.getLogger(__name__)

# Notificaciones que se procesan pedido por pedido.
TIPOS_POR_PEDIDO = ('confirmacionMercaderiaPreparada', 'confirmacionPedido')
PROCESANDO_COLGADA = timedelta(minutes=30)


def _en_test():
    return getattr(threading.current_thread(), 'testing', False)


class RequestCola:
    """Lo mínimo de `request` que usan los handlers del webhook (`request.env`), para correrlos
    fuera de una petición HTTP. Es falso en un `if request:`, así el resto de Odoo (el render
    QWeb del e-Remito, por ejemplo) se comporta como en un cron, sin sesión ni petición."""

    def __init__(self, env):
        self.env = env

    def update_env(self, user=None, context=None, su=None):
        pass

    def __bool__(self):
        return False


class WisNotificacion(models.Model):
    _name = 'wis.notificacion'
    _description = 'Notificación recibida de WIS'
    _order = 'id desc'
    _rec_name = 'display_name'

    numero_interfaz = fields.Integer(string="Número de interfaz", index=True, readonly=True)
    tipo = fields.Char(required=True, index=True, readonly=True)
    payload = fields.Text(readonly=True, help="La notificación tal como la mandó WIS.")
    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('procesando', 'Procesando'),
        ('hecho', 'Procesada'),
        ('con_errores', 'Procesada con errores'),
        ('retenida', 'Retenida: requiere aprobación'),
        ('error', 'Error'),
    ], default='pendiente', required=True, index=True, readonly=True)
    pedidos_total = fields.Integer(readonly=True)
    pedidos_hechos = fields.Integer(readonly=True)
    pendientes = fields.Text(readonly=True, help="Pedidos que faltan procesar (JSON).")
    fase = fields.Selection([('pedidos', 'Pedidos'), ('cadena', 'Entrega a tienda'), ('fin', 'Terminada')],
                            default='pedidos', readonly=True)
    despachado_ids = fields.Many2many(
        'stock.picking', 'wis_notificacion_picking_rel', 'notificacion_id', 'picking_id',
        string="Operaciones despachadas", readonly=True)
    errores = fields.Text(readonly=True)
    intentos = fields.Integer(readonly=True)
    recibida = fields.Datetime(default=fields.Datetime.now, readonly=True)
    inicio = fields.Datetime(readonly=True)
    fin = fields.Datetime(readonly=True)
    aprobada_por = fields.Many2one('res.users', readonly=True, ondelete='set null',
                                   help="Quien aprobó aplicar lo que superaba los topes.")

    def init(self):
        # Un reenvío de WIS trae el mismo número de interfaz: no se guarda dos veces.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS wis_notificacion_tipo_interfaz_uniq
            ON wis_notificacion (tipo, numero_interfaz) WHERE numero_interfaz > 0
        """)

    @api.depends('tipo', 'numero_interfaz')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.tipo} {rec.numero_interfaz or ''}".strip()

    # ------------------------------------------------------------------
    # Recepción
    # ------------------------------------------------------------------
    @api.model
    def _registrar(self, tipo, numero_interfaz, payload):
        """Guarda la notificación. Devuelve (registro, es_nueva).

        Si ya se recibió una con el mismo tipo y número de interfaz devuelve esa, sin crear
        nada: es un reenvío de WIS.
        """
        if numero_interfaz:
            existente = self.search([('tipo', '=', tipo), ('numero_interfaz', '=', numero_interfaz)], limit=1)
            if existente:
                return existente, False
        datos = payload.get(tipo) or payload.get(tipo[:1].upper() + tipo[1:]) or {}
        pedidos = self._pedidos_de(tipo, datos)
        vals = {
            'tipo': tipo,
            'numero_interfaz': numero_interfaz or 0,
            'payload': json.dumps(payload, ensure_ascii=False),
            'pedidos_total': len(pedidos),
            'pendientes': json.dumps(pedidos),
        }
        try:
            with self.env.cr.savepoint():
                rec = self.create(vals)
        except UniqueViolation:
            # Dos copias de la misma notificación al mismo tiempo: la otra ganó. Es un reenvío
            # (la de esta transacción puede no verla todavía).
            return self.search([('tipo', '=', tipo), ('numero_interfaz', '=', numero_interfaz)], limit=1), False
        return rec, True

    @api.model
    def _pedidos_de(self, tipo, datos):
        """Los pedidos de la notificación: los de la lista y los que sólo aparecen en las cajas.

        WIS mandó al menos una vez (642399, 20-10-2026) cajas de pedidos que no estaban en la
        lista `Pedidos`. Procesando sólo la lista, esos pedidos quedaban sin preparar y sin aviso.
        """
        if tipo not in TIPOS_POR_PEDIDO:
            return []
        vistos = []
        for p in datos.get('Pedidos') or datos.get('pedidos') or []:
            cod = p.get('Pedido') or p.get('pedido')
            if cod and cod not in vistos:
                vistos.append(cod)
        for c in datos.get('Contenedores') or datos.get('contenedores') or []:
            for d in c.get('Detalles') or c.get('detalles') or []:
                cod = d.get('Pedido') or d.get('pedido')
                if cod and cod not in vistos:
                    vistos.append(cod)
        return vistos

    @api.model
    def _disparar_cron(self):
        cron = self.env.ref('integracion_wis.ir_cron_wis_notificaciones', raise_if_not_found=False)
        if not cron:
            return
        try:
            cron.sudo()._trigger()
        except Exception as e:
            _logger.warning("[WIS] No se pudo disparar el cron de notificaciones: %s", e)

    # ------------------------------------------------------------------
    # Procesamiento
    # ------------------------------------------------------------------
    @api.model
    def _cron_procesar(self):
        colgadas = self.search([('estado', '=', 'procesando'),
                                ('write_date', '<', fields.Datetime.now() - PROCESANDO_COLGADA)])
        colgadas.write({'estado': 'pendiente'})
        if not _en_test():
            self.env.cr.commit()
        Config = self.env['integracion_wis.integracion_wis']
        inicio = time.time()
        presupuesto = Config._presupuesto_corrida(operaciones=True)
        while time.time() - inicio < presupuesto:
            notif = self._siguiente()
            if not notif:
                return True
            terminada = notif._procesar(inicio + presupuesto)
            if not _en_test():
                self.env.cr.commit()
            if not terminada:
                break
        # Quedó algo: la corrida siguiente arranca enseguida.
        self._disparar_cron()
        return True

    @api.model
    def _siguiente(self):
        self.env.cr.execute("""
            SELECT id FROM wis_notificacion
            WHERE estado = 'pendiente' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
        """)
        fila = self.env.cr.fetchone()
        return self.browse(fila[0]) if fila else self.browse()

    def _procesar(self, limite):
        """Procesa esta notificación hasta terminarla o hasta `limite` (epoch). True si terminó."""
        self.ensure_one()
        from odoo.addons.integracion_wis.controllers.controllers import WebhookWIS
        self.write({'estado': 'procesando', 'intentos': self.intentos + 1,
                    'inicio': self.inicio or fields.Datetime.now()})
        if not _en_test():
            self.env.cr.commit()
        payload = json.loads(self.payload or '{}')
        ctrl = WebhookWIS()
        env = self.env(su=True)
        _request_stack.push(RequestCola(env))
        try:
            ctrl._wis_preparar_evento(self.tipo, payload, self.numero_interfaz, cola=self)
            if self.tipo in TIPOS_POR_PEDIDO:
                terminada = self._procesar_por_pedido(ctrl, payload, limite)
            else:
                terminada = self._procesar_entera(ctrl, payload)
        finally:
            _request_stack.pop()
        return terminada

    def _procesar_entera(self, ctrl, payload):
        error = ctrl._wis_ejecutar_handler(self.tipo, payload)
        retenida = getattr(ctrl, '_wis_retenida', False)
        self._terminar([error] if error else [], retenida=retenida)
        return True

    def _procesar_por_pedido(self, ctrl, payload, limite):
        errores = json.loads(self.errores or '[]')
        pendientes = json.loads(self.pendientes or '[]')
        while pendientes and self.fase == 'pedidos':
            if time.time() > limite:
                self.write({'estado': 'pendiente', 'pendientes': json.dumps(pendientes),
                            'errores': json.dumps(errores, ensure_ascii=False)})
                return False
            pedido = pendientes.pop(0)
            error = ctrl._wis_ejecutar_handler(self.tipo, payload, pedido=pedido)
            if error:
                errores.append(f"{pedido}: {error}")
            despachados = getattr(ctrl, '_wis_despachados', None)
            vals = {'pendientes': json.dumps(pendientes), 'pedidos_hechos': self.pedidos_hechos + 1,
                    'errores': json.dumps(errores, ensure_ascii=False)}
            if despachados:
                vals['despachado_ids'] = [(4, pid) for pid in despachados.ids]
                ctrl._wis_despachados = None
            self.write(vals)
            if not _en_test():
                self.env.cr.commit()
        if self.tipo == 'confirmacionPedido' and self.fase == 'pedidos':
            self.write({'fase': 'cadena'})
        if self.fase == 'cadena':
            error = ctrl._wis_avanzar_cadena(self.despachado_ids, origen=f"confirmacionPedido {self.numero_interfaz}")
            if error:
                errores.append(error)
        self._terminar(errores)
        return True

    def _terminar(self, errores, retenida=False):
        estado = 'retenida' if retenida else ('con_errores' if errores else 'hecho')
        self.write({'estado': estado, 'fase': 'fin', 'fin': fields.Datetime.now(),
                    'errores': json.dumps(errores, ensure_ascii=False) if errores else False,
                    'pendientes': '[]'})
        respuesta = {'status': 200, 'detail': _("Procesada en segundo plano.")}
        if errores:
            respuesta = {'status': 500, 'detail': ' | '.join(errores)[:4000]}
        self.env['wis.webhook.log'].sudo().create({
            'fecha': fields.Date.today(),
            'hora': time.strftime('%H:%M:%S'),
            'numero_interfaz_ejecucion': self.numero_interfaz or 0,
            'request': self.payload,
            'respuesta': json.dumps(respuesta, indent=4, ensure_ascii=False),
            'tipo': self.tipo,
            'estado': 'error' if errores else 'exito',
        })

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def action_reprocesar(self):
        """Vuelve a la cola lo que terminó con errores. Lo ya aplicado no se repite: cada
        contenedor de una preparación se aplica una sola vez."""
        for rec in self.filtered(lambda r: r.estado in ('con_errores', 'error')):
            datos = json.loads(rec.payload or '{}')
            datos = datos.get(rec.tipo) or datos.get(rec.tipo[:1].upper() + rec.tipo[1:]) or {}
            rec.write({'estado': 'pendiente', 'fase': 'pedidos', 'errores': False, 'pedidos_hechos': 0,
                       'pendientes': json.dumps(self._pedidos_de(rec.tipo, datos))})
        self._disparar_cron()

    def action_aprobar(self):
        """Aplica una notificación retenida (por ejemplo un ajuste por encima del tope)."""
        for rec in self.filtered(lambda r: r.estado == 'retenida'):
            rec.with_context(wis_ajuste_aprobado=True)._aprobar_y_procesar()

    def _aprobar_y_procesar(self):
        self.write({'estado': 'pendiente', 'fase': 'pedidos', 'aprobada_por': self.env.user.id})
        from odoo.addons.integracion_wis.controllers.controllers import WebhookWIS
        payload = json.loads(self.payload or '{}')
        ctrl = WebhookWIS()
        ctrl._wis_ajuste_aprobado = True
        _request_stack.push(RequestCola(self.env(su=True)))
        try:
            ctrl._wis_preparar_evento(self.tipo, payload, self.numero_interfaz, cola=self)
            error = ctrl._wis_ejecutar_handler(self.tipo, payload)
        finally:
            _request_stack.pop()
        self._terminar([error] if error else [])


class WisPreparacionAplicada(models.Model):
    """Cada contenedor de una preparación de WIS, aplicado a un pedido. Un contenedor se aplica
    una sola vez: si WIS lo vuelve a mandar (reenvío por timeout, otra notificación) se ignora."""
    _name = 'wis.preparacion.aplicada'
    _description = 'Contenedor de WIS aplicado a un pedido'
    _order = 'id desc'

    pedido = fields.Char(required=True, index=True)
    contenedor = fields.Char(required=True, index=True, help="NumeroContenedor de WIS (o el código de barras).")
    nro_preparacion = fields.Char(index=True)
    picking_id = fields.Many2one('stock.picking', ondelete='set null', index=True)
    notificacion_id = fields.Many2one('wis.notificacion', ondelete='set null')
    despachado = fields.Boolean(index=True)

    _sql_constraints = [
        ('pedido_contenedor_uniq', 'unique(pedido, contenedor)',
         'Ese contenedor de WIS ya se aplicó a ese pedido.'),
    ]

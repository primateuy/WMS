# -*- coding: utf-8 -*-
"""Actualización por Cluster en segundo plano: rutas y reglas de abastecimiento.

POR QUÉ EXISTE. Cambiar el Cluster de una plantilla se hacía en línea, adentro
del guardado. Medido en `o17_support_forum` (28-09-2026) sobre una plantilla de
112 variantes: 29,6 s con la sesión tomada, las reglas de abastecimiento de cada
variante regeneradas TRES veces (336 regeneraciones) y 3.397 escrituras de rutas
de las automatizaciones. Con 500 o 1.386 variantes eran minutos, y el worker
HTTP terminaba muerto por límite de tiempo: «tumba el servidor».

QUÉ HACE, con el mismo resultado final que el camino anterior:

    Fase 1 · Rutas     por plantilla, las acciones de la automatización de su
                       Cluster (las «Auto Seleccion de Rutas …»), aplicadas en
                       memoria en el mismo orden y escritas UNA vez.
    Fase 2 · Reglas    por tandas de variantes, el conjunto de reglas que
                       generaría `generarReglasAbastecimiento`, comparado con lo
                       que existe: sólo se crea, modifica o borra lo que cambia.

Cada tanda es una ejecución del cron con su commit y un tope de tiempo: nunca
hay una transacción larga ni un worker HTTP ocupado.
"""
import logging
import time

from psycopg2.extensions import TransactionRollbackError

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError
from odoo.fields import Command

_logger = logging.getLogger(__name__)

# Tope de TIEMPO por tanda: el cron corre con límite de tiempo real en
# producción, y una tanda corta además suelta la base enseguida.
TIEMPO_MAX_TANDA = 20
REINTENTOS_TANDA = 3
# Contexto con el que el proceso corre las automatizaciones de Cluster. Fuera
# de él no corren nunca (ver `base_automation.py`).
CTX_APLICANDO = 'cluster_proceso_aplicando'
# Contexto para crear reglas sin chatter: una regla de abastecimiento creada
# dejaba un mensaje en su chatter, y eran miles.
CTX_SIN_CHATTER = {'tracking_disable': True, 'mail_create_nolog': True,
                   'mail_create_nosubscribe': True, 'mail_notrack': True}


class ClusterProceso(models.Model):
    _name = 'cluster.proceso'
    _description = 'Actualización por Cluster en segundo plano'
    _order = 'id desc'

    name = fields.Char(string='Proceso', required=True, readonly=True, copy=False,
                       default=lambda self: _('Nuevo'))
    template_ids = fields.Many2many(
        'product.template', 'cluster_proceso_template_rel', 'proceso_id', 'template_id',
        string='Productos', readonly=True)
    variant_ids = fields.Many2many(
        'product.product', 'cluster_proceso_variant_rel', 'proceso_id', 'product_id',
        string='Variantes sueltas', readonly=True,
        help="Variantes a procesar además de las de los productos, cuando se "
             "cambió el Cluster de variantes directamente.")
    con_rutas = fields.Boolean(
        string='Actualizar rutas', default=True, readonly=True,
        help="Si es falso, sólo se regeneran las reglas de abastecimiento (botón "
             "«Actualizar Reglas»).")
    user_id = fields.Many2one('res.users', string='Usuario', readonly=True,
                              default=lambda self: self.env.user, ondelete='set null')
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, readonly=True,
        default=lambda self: self.env.company, ondelete='cascade',
        help="🔴 La del usuario que hizo el cambio: el cálculo de reglas usa "
             "`env.company`, y en el cron sería la del usuario del cron.")
    origen = fields.Char(string='Origen', readonly=True)

    fase = fields.Selection(
        [('cola', 'En cola'), ('rutas', 'Rutas'), ('reglas', 'Reglas de abastecimiento'),
         ('terminado', 'Terminado')],
        string='Fase', default='cola', required=True, readonly=True, copy=False)
    estado = fields.Selection(
        [('en_proceso', 'En proceso'), ('detenido', 'Detenido por error'),
         ('cancelado', 'Cancelado'), ('terminado', 'Terminado')],
        string='Estado', default='en_proceso', required=True, readonly=True, copy=False)
    cancel_requested = fields.Boolean(string='Cancelación pedida', readonly=True, copy=False)
    error_msg = fields.Text(string='Motivo de la detención', readonly=True, copy=False)
    etapa = fields.Char(string='Etapa', readonly=True, copy=False)
    log = fields.Text(string='Registro', readonly=True, copy=False)
    variantes_por_tanda = fields.Integer(
        string='Variantes por tanda', default=50, required=True,
        help="Tope de variantes por tanda. Cada tanda además se corta a los "
             "%d segundos." % TIEMPO_MAX_TANDA)

    rutas_total = fields.Integer(string='Productos a procesar', readonly=True, copy=False)
    rutas_hecho = fields.Integer(string='Productos procesados', readonly=True, copy=False)
    rutas_cambiadas = fields.Integer(string='Productos con rutas cambiadas', readonly=True, copy=False)
    rutas_puntero = fields.Integer(readonly=True, copy=False)
    rutas_started_at = fields.Datetime(readonly=True, copy=False)
    rutas_ended_at = fields.Datetime(readonly=True, copy=False)

    reglas_total = fields.Integer(string='Variantes', readonly=True, copy=False)
    reglas_hecho = fields.Integer(string='Variantes procesadas', readonly=True, copy=False)
    reglas_puntero = fields.Integer(readonly=True, copy=False)
    reglas_creadas = fields.Integer(string='Reglas creadas', readonly=True, copy=False)
    reglas_modificadas = fields.Integer(string='Reglas modificadas', readonly=True, copy=False)
    reglas_borradas = fields.Integer(string='Reglas borradas', readonly=True, copy=False)
    reglas_sin_cambio = fields.Integer(string='Reglas sin cambio', readonly=True, copy=False)
    reglas_started_at = fields.Datetime(readonly=True, copy=False)
    reglas_ended_at = fields.Datetime(readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Encolado
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nuevo')) == _('Nuevo'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cluster.proceso') or _('Nuevo')
        return super().create(vals_list)

    @api.model
    def _encolar(self, templates=None, variants=None, con_rutas=True, origen=''):
        """Deja un proceso en cola y dispara el cron. No procesa nada acá."""
        templates = (templates or self.env['product.template']).exists()
        variants = (variants or self.env['product.product']).exists()
        if not templates and not variants:
            return self.browse()
        proceso = self.sudo().create({
            'template_ids': [Command.set(templates.ids)],
            'variant_ids': [Command.set(variants.ids)],
            'con_rutas': con_rutas and bool(templates),
            'user_id': self.env.user.id,
            'company_id': self.env.company.id,
            'origen': origen,
        })
        proceso._log(_("En cola: %(t)d producto(s), %(v)d variante(s) sueltas. Origen: %(o)s.",
                       t=len(templates), v=len(variants), o=origen or '-'))
        proceso._encolar_cron()
        return proceso

    def _encolar_cron(self):
        cron = self.env.ref('automatizacion_reglas_abastecimiento.ir_cron_cluster_proceso',
                            raise_if_not_found=False)
        if cron:
            try:
                cron.sudo()._trigger()
            except Exception as e:
                _logger.warning("[Cluster] no se pudo disparar el cron: %s", e)

    # ------------------------------------------------------------------
    # Botones
    # ------------------------------------------------------------------
    def action_cancelar(self):
        for proceso in self.filtered(lambda p: p.estado in ('en_proceso', 'detenido')):
            if proceso.estado == 'detenido':
                proceso.write({'estado': 'cancelado', 'etapa': False})
                proceso._log(_("Cancelado."))
            else:
                proceso.cancel_requested = True
                proceso._log(_("Cancelación pedida: se detiene al terminar la tanda en curso."))
        return True

    def action_reanudar(self):
        for proceso in self.filtered(lambda p: p.estado in ('detenido', 'cancelado')):
            proceso.write({'estado': 'en_proceso', 'cancel_requested': False,
                           'error_msg': False, 'etapa': _("En cola: sigue en segundos.")})
            proceso._log(_("Reanudado desde donde iba."))
        self._encolar_cron()
        return True

    # ------------------------------------------------------------------
    # Cron: una tanda por ejecución
    # ------------------------------------------------------------------
    @api.model
    def _cron_procesar(self):
        """Avanza de a UN proceso y UNA tanda por ejecución.

        🔴 Un proceso detenido no se retoma solo: reintentar lo que falla siempre
        igual es un bucle que carga la base sin avanzar.
        """
        proceso = self.sudo().search([('estado', '=', 'en_proceso')], order='id asc', limit=1)
        if proceso:
            proceso._una_tanda()
        return True

    def _una_tanda(self):
        self.ensure_one()
        self.invalidate_recordset(['estado', 'cancel_requested'])
        if self.estado != 'en_proceso':
            return
        if self.cancel_requested:
            self.write({'estado': 'cancelado', 'etapa': False, 'cancel_requested': False})
            self._log(_("Cancelado."))
            self.env.cr.commit()
            return
        quedan = False
        for intento in range(1, REINTENTOS_TANDA + 1):
            try:
                quedan = self._avanzar()
                self.env.cr.commit()
                break
            except Exception as e:
                self.env.cr.rollback()
                self.env.clear()
                if isinstance(e, TransactionRollbackError) and intento < REINTENTOS_TANDA:
                    _logger.warning("[Cluster %s] la tanda chocó (%s); reintento %d", self.id, e,
                                    intento + 1)
                    continue
                motivo = tools.ustr(e)[:2000]
                self.write({'estado': 'detenido', 'error_msg': motivo,
                            'etapa': _("Detenido por un error. Revisá el motivo y reanudá.")})
                self._log(_("Detenido: %s") % motivo[:500])
                self.env.cr.commit()
                _logger.exception("[Cluster %s] tanda fallida", self.id)
                return
        if quedan:
            self._encolar_cron()
        else:
            # Puede haber otro proceso en cola detrás de éste.
            if self.search_count([('estado', '=', 'en_proceso')]):
                self._encolar_cron()

    def _avanzar(self):
        """Una tanda de la fase en curso. Devuelve True si queda trabajo."""
        self.ensure_one()
        # 🔴 Todo el cálculo con la compañía y el usuario de quien hizo el cambio.
        proc = self.with_company(self.company_id).with_user(
            self.user_id or self.env.user).with_context(allowed_company_ids=self.company_id.ids)
        if self.fase == 'cola':
            proc._arrancar()
            return True
        if self.fase == 'rutas':
            return proc._tanda_rutas()
        if self.fase == 'reglas':
            return proc._tanda_reglas()
        return False

    def _arrancar(self):
        vals = {'rutas_total': len(self.template_ids) if self.con_rutas else 0,
                'reglas_total': len(self._variantes_a_procesar())}
        if self.con_rutas and self.template_ids:
            vals.update(fase='rutas', rutas_started_at=fields.Datetime.now(),
                        etapa=_("Actualizando rutas de los productos…"))
        else:
            vals.update(fase='reglas', reglas_started_at=fields.Datetime.now(),
                        etapa=_("Actualizando reglas de abastecimiento…"))
        self.sudo().write(vals)
        self._log(_("Arranca: %(t)d producto(s) para rutas, %(v)d variante(s) para reglas.",
                    t=vals['rutas_total'], v=vals['reglas_total']))

    def _variantes_a_procesar(self):
        """Variantes de los productos del proceso más las sueltas, por id.

        Se leen AL PROCESAR, no al encolar: si el Cluster cambió otra vez en el
        medio, vale el último.
        """
        variantes = self.env['product.product'].with_context(active_test=False).search(
            [('product_tmpl_id', 'in', self.template_ids.ids), ('active', '=', True)])
        return (variantes | self.variant_ids).sorted('id')

    # ------------------------------------------------------------------
    # Fase 1: rutas
    # ------------------------------------------------------------------
    def _tanda_rutas(self):
        t0 = time.time()
        plantillas = self.template_ids.filtered(lambda t: t.id > self.rutas_puntero).sorted('id')
        hechas, cambiadas, puntero = 0, 0, self.rutas_puntero
        automatizaciones = self.env['base.automation']._cluster_automatizaciones()
        for plantilla in plantillas:
            if time.time() - t0 >= TIEMPO_MAX_TANDA:
                break
            if plantilla.exists() and plantilla._cluster_aplicar_rutas(automatizaciones):
                cambiadas += 1
            hechas += 1
            puntero = plantilla.id
        pendientes = len(plantillas) - hechas
        vals = {'rutas_puntero': puntero, 'rutas_hecho': self.rutas_hecho + hechas,
                'rutas_cambiadas': self.rutas_cambiadas + cambiadas,
                'etapa': _("Rutas: %d de %d productos") % (self.rutas_hecho + hechas,
                                                            self.rutas_total)}
        if not pendientes:
            vals.update(fase='reglas', rutas_ended_at=fields.Datetime.now(),
                        reglas_started_at=fields.Datetime.now(),
                        etapa=_("Actualizando reglas de abastecimiento…"))
        self.sudo().write(vals)
        if not pendientes:
            self._log(_("Rutas listas: %d producto(s), %d con cambios.")
                      % (vals['rutas_hecho'], vals['rutas_cambiadas']))
        return True

    # ------------------------------------------------------------------
    # Fase 2: reglas de abastecimiento
    # ------------------------------------------------------------------
    def _tanda_reglas(self):
        t0 = time.time()
        variantes = self._variantes_a_procesar().filtered(lambda v: v.id > self.reglas_puntero)
        tanda = variantes[:max(self.variantes_por_tanda, 1)]
        if not tanda:
            self._terminar()
            return False
        cuenta = self.env['product.product']._cluster_sincronizar_reglas(tanda, deadline=t0 + TIEMPO_MAX_TANDA)
        procesadas = cuenta['procesadas']
        puntero = procesadas[-1].id if procesadas else self.reglas_puntero
        self.sudo().write({
            'reglas_puntero': puntero,
            'reglas_hecho': self.reglas_hecho + len(procesadas),
            'reglas_creadas': self.reglas_creadas + cuenta['creadas'],
            'reglas_modificadas': self.reglas_modificadas + cuenta['modificadas'],
            'reglas_borradas': self.reglas_borradas + cuenta['borradas'],
            'reglas_sin_cambio': self.reglas_sin_cambio + cuenta['sin_cambio'],
            'etapa': _("Reglas: %d de %d variantes") % (self.reglas_hecho + len(procesadas),
                                                         self.reglas_total),
        })
        _logger.info("[Cluster %s] tanda de reglas: %d variantes en %.1fs | +%d ~%d -%d =%d",
                     self.id, len(procesadas), time.time() - t0, cuenta['creadas'],
                     cuenta['modificadas'], cuenta['borradas'], cuenta['sin_cambio'])
        if len(variantes) <= len(procesadas):
            self._terminar()
            return False
        return True

    def _terminar(self):
        self.sudo().write({'fase': 'terminado', 'estado': 'terminado', 'etapa': False,
                           'reglas_ended_at': fields.Datetime.now()})
        self._log(_("Terminado. Reglas: %(c)d creadas, %(m)d modificadas, %(b)d borradas, "
                    "%(s)d sin cambio.", c=self.reglas_creadas, m=self.reglas_modificadas,
                    b=self.reglas_borradas, s=self.reglas_sin_cambio))

    # ------------------------------------------------------------------
    def _log(self, texto):
        for proceso in self.sudo():
            linea = "%s  %s" % (fields.Datetime.to_string(fields.Datetime.now()), texto)
            proceso.log = ((proceso.log + "\n") if proceso.log else "") + linea

    def _notificacion_encolado(self, variantes):
        """Aviso al usuario: el trabajo quedó en segundo plano."""
        if not self:
            return True
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("En segundo plano"),
                'message': _("%(p)s: las reglas de %(n)d variante(s) se actualizan en segundo "
                             "plano. Podés seguir trabajando; el avance se ve en Inventario › "
                             "Actualización por Cluster.", p=self.name, n=variantes),
                'type': 'info',
                'sticky': False,
            },
        }

    def action_abrir(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'current'}

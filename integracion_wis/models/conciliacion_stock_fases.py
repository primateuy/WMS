# -*- coding: utf-8 -*-
"""Conciliación de stock contra WIS, por fases y por tandas.

Reemplaza la corrida de un solo tiro por el mismo esquema que ya usa la
importación masiva de inventario: una tabla de trabajo, fases que el usuario
dispara **a mano**, y tandas con puntero para no dejar una transacción abierta
ni un cron consultando WIS por su cuenta.

    Fase 0  Armar la tabla     una fila por variante integrada con código WIS
    Fase 1  Consultar WIS      por tandas: stock de WIS y diferencia contra Odoo
    Fase 2  Generar el ajuste  se entrega al motor de inventario (ver README)

La fase 1 tiene tres etapas:

    sondeo      cuántas páginas tiene el listado de stock de WIS (búsqueda
                binaria, ~2·log2(N) requests). Da el total de la barra y
                decide la estrategia.
    listado     recorre `/ConsultaDeStock/GetData` de a páginas de 10. Conviene
                cuando el listado tiene menos páginas que variantes a consultar.
    por_codigo  una consulta filtrada por código. Es la estrategia cuando el
                listado es más largo que la tabla, y es SIEMPRE la segunda pasada
                para las variantes que el listado no trajo: con el filtro WIS
                distingue «existe y no tiene stock» de «no existe».

🔴 **El fin del listado es un 400**, no una lista vacía. Tomarlo como error
dejó la conciliación de producción repitiendo su última tanda durante horas
(28-09-2026): la tanda caía, el rollback deshacía el puntero y el cron de 5
minutos la volvía a levantar. Ver `IntegracionWIS._consulta_stock_request`.

🔴 **Sólo cuenta como cero lo que WIS dice que es cero.** Entra al ajuste el
stock que WIS informa y el «no se encontró stock» de una consulta POR CÓDIGO
—una respuesta explícita sobre ese producto—. Lo que WIS no conoce o no
contestó queda fuera: tomar el silencio como cero pondría en cero productos que
simplemente no se pudieron consultar, y eso no se deshace con un undo.
"""
import base64
import io
import logging
import time

from psycopg2.extras import execute_values

from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError
from psycopg2.extensions import TransactionRollbackError

from .models import ErrorConsultaStockWIS

_logger = logging.getLogger(__name__)

# Tope de filas del listado (o de códigos) por tanda.
LOTE_CONSULTA = 200
REINTENTOS_TANDA = 3
# Tope de TIEMPO por tanda. Cada tanda corre dentro del cron y hace commit al
# final: con los workers de producción el cron tiene límite de tiempo real, y
# 200 consultas por código a ~1 s cada una lo pasarían. Se corta antes.
TIEMPO_MAX_TANDA = 50
# En la pasada por código, errores seguidos que frenan la consulta: uno suelto
# deja esa variante fuera del ajuste; varios seguidos son WIS caído.
ERRORES_SEGUIDOS_MAX = 5
# Estados de la tabla de trabajo que entran al ajuste.
ESTADOS_AJUSTE = ('ok', 'sin_stock')


class ConciliacionStockFases(models.Model):
    _inherit = 'conciliacion.stock'

    fase = fields.Selection(
        [('borrador', 'Sin empezar'),
         ('tabla', 'Tabla armada'),
         ('consultando', 'Consultando WIS'),
         ('consultado', 'Diferencias listas'),
         ('ajuste', 'Ajuste generado')],
        string='Fase', default='borrador', required=True, readonly=True, copy=False,
        help="En qué fase está la conciliación. Decide qué retoma «Reanudar».",
    )
    cs_batch_size = fields.Integer(
        string='Variantes por tanda', default=LOTE_CONSULTA, required=True,
        help="Tope de filas del listado (o de códigos) por tanda. Cada tanda "
             "además se corta a los %d segundos, así que esto rara vez se "
             "alcanza: WIS pagina de a 10 y cada request tarda ~1 s." % TIEMPO_MAX_TANDA,
    )
    cs_total = fields.Integer(string='Variantes a consultar', readonly=True, copy=False)
    cs_done = fields.Integer(string='Clasificadas', readonly=True, copy=False)
    cs_encontradas = fields.Integer(string='Con stock en WIS', readonly=True, copy=False)
    cs_sin_stock = fields.Integer(
        string='Sin stock en WIS', readonly=True, copy=False,
        help="WIS conoce el producto y, consultado por código, dice que no "
             "tiene stock. Entran al ajuste como cero.")
    cs_no_existe = fields.Integer(string='No existen en WIS', readonly=True, copy=False)
    cs_err_filas = fields.Integer(string='Con error', readonly=True, copy=False)
    cs_errors = fields.Integer(
        string='Fuera del ajuste', readonly=True, copy=False,
        help="Las que WIS no conoce o no pudo contestar. No entran al ajuste: "
             "un silencio de WIS no es un cero.")
    cs_con_diferencia = fields.Integer(string='Con diferencia', readonly=True, copy=False)
    cs_step = fields.Char(string='Etapa', readonly=True, copy=False)
    cs_started_at = fields.Datetime(string='Inicio de la consulta', readonly=True, copy=False)
    cs_ended_at = fields.Datetime(string='Fin de la consulta', readonly=True, copy=False)
    cs_cancel_requested = fields.Boolean(string='Cancelación pedida', readonly=True, copy=False)
    cs_pagina = fields.Integer(string='Última página leída', readonly=True, copy=False)
    cs_paginas_total = fields.Integer(
        string='Páginas del listado', readonly=True, copy=False,
        help="Cuántas páginas tiene el listado de stock de WIS, medido al "
             "arrancar la consulta.")
    cs_etapa_consulta = fields.Selection(
        [('sondeo', 'Midiendo el listado'),
         ('listado', 'Recorriendo el listado'),
         ('por_codigo', 'Consultando por código')],
        string='Etapa de la consulta', readonly=True, copy=False)
    cs_estrategia = fields.Selection(
        [('listado', 'Recorrer el listado'),
         ('por_codigo', 'Por código')],
        string='Estrategia', readonly=True, copy=False,
        help="Se elige sola al medir el listado: la que necesita menos requests.")
    cs_pc_total = fields.Integer(string='Códigos a consultar', readonly=True, copy=False)
    cs_pc_done = fields.Integer(string='Códigos consultados', readonly=True, copy=False)
    cs_pc_started_at = fields.Datetime(string='Inicio por código', readonly=True, copy=False)
    cs_error_msg = fields.Text(string='Motivo de la detención', readonly=True, copy=False)
    cs_tabla_started_at = fields.Datetime(string='Inicio del armado', readonly=True, copy=False)
    cs_tabla_ended_at = fields.Datetime(string='Fin del armado', readonly=True, copy=False)
    cs_ajuste_started_at = fields.Datetime(string='Inicio del ajuste', readonly=True, copy=False)
    cs_ajuste_ended_at = fields.Datetime(string='Ajuste generado', readonly=True, copy=False)
    cs_ajuste_celdas = fields.Integer(string='Celdas del ajuste', readonly=True, copy=False)
    cs_tope_a_cero_pct = fields.Integer(
        string='Tope de variantes a cero (%)', default=10,
        help="Si el ajuste dejaría en cero más de este porcentaje de las "
             "variantes consultadas, no se genera y hay que revisar. Un WIS a "
             "medio responder se parece mucho a un inventario vacío.",
    )
    cs_consulta_ids = fields.One2many(
        'wis.conciliacion.consulta', 'conciliacion_id', string='Consultas a WIS')
    cs_requests = fields.Integer(string='Requests a WIS', compute='_compute_cs_requests')
    cs_requests_error = fields.Integer(string='Requests con error',
                                       compute='_compute_cs_requests')
    cs_ultimo_request = fields.Char(string='Último request', compute='_compute_cs_requests')

    # 🔴 Referencia SUELTA al batch de inventario, a propósito: un Many2one
    # obligaría a este módulo —que es compartido entre clientes— a depender de
    # `forum_partner_import`, que es de Forum. Se guarda el id y se abre por
    # acción, que no necesita el comodel declarado.
    cs_batch_id = fields.Integer(string='Batch de ajuste', readonly=True, copy=False)
    cs_batch_nombre = fields.Char(string='Nombre del batch', readonly=True, copy=False)
    cs_batch_estado = fields.Char(
        string='Estado del ajuste', compute='_compute_cs_batch_estado',
        help="En qué fase está el ajuste. Se lee del batch: acá no se duplica.",
    )
    cs_batch_avance = fields.Char(string='Avance del ajuste',
                                  compute='_compute_cs_batch_estado')
    # Espejo del avance del batch, para las barras de esta pantalla. Mismo
    # criterio: se leen del batch, no se guardan.
    cs_b_fase = fields.Char(compute='_compute_cs_batch_estado')
    cs_b_apply_total = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_apply_done = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_applied = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_apply_errors = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_apply_started_at = fields.Datetime(compute='_compute_cs_batch_estado')
    cs_b_apply_ended_at = fields.Datetime(compute='_compute_cs_batch_estado')
    cs_b_post_total = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_post_done = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_post_errors = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_post_started_at = fields.Datetime(compute='_compute_cs_batch_estado')
    cs_b_post_ended_at = fields.Datetime(compute='_compute_cs_batch_estado')
    cs_b_rec_total = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_rec_done = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_rec_errors = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_rec_lines = fields.Integer(compute='_compute_cs_batch_estado')
    cs_b_rec_started_at = fields.Datetime(compute='_compute_cs_batch_estado')
    cs_b_rec_ended_at = fields.Datetime(compute='_compute_cs_batch_estado')

    # Campos del batch que se espejan: (campo acá, campo en el batch).
    _CS_ESPEJO_BATCH = [
        ('cs_b_fase', 'current_phase'),
        ('cs_b_apply_total', 'apply_total'), ('cs_b_apply_done', 'apply_processed'),
        ('cs_b_applied', 'applied_count'), ('cs_b_apply_errors', 'apply_errors'),
        ('cs_b_apply_started_at', 'apply_started_at'), ('cs_b_apply_ended_at', 'apply_ended_at'),
        ('cs_b_post_total', 'post_total'), ('cs_b_post_done', 'post_done'),
        ('cs_b_post_errors', 'post_errors'),
        ('cs_b_post_started_at', 'post_started_at'), ('cs_b_post_ended_at', 'post_ended_at'),
        ('cs_b_rec_total', 'rec_total'), ('cs_b_rec_done', 'rec_done'),
        ('cs_b_rec_errors', 'rec_errors'), ('cs_b_rec_lines', 'rec_lines'),
        ('cs_b_rec_started_at', 'rec_started_at'), ('cs_b_rec_ended_at', 'rec_ended_at'),
    ]

    def _compute_cs_batch_estado(self):
        """Lee el estado del batch sin declararlo como relación.

        `env.get` y no un Many2one: este módulo es compartido y no puede
        depender de `forum_partner_import`. Si el motor no está, los campos
        quedan vacíos y los botones no aparecen.
        """
        Batch = self.env.get('forum.import.batch')
        for conc in self:
            conc.cs_batch_estado = False
            conc.cs_batch_avance = False
            for campo, _origen in self._CS_ESPEJO_BATCH:
                conc[campo] = False
            if Batch is None or not conc.cs_batch_id:
                continue
            batch = Batch.browse(conc.cs_batch_id).exists()
            if not batch:
                continue
            conc.cs_batch_estado = batch.state
            for campo, origen in self._CS_ESPEJO_BATCH:
                if origen in batch._fields:
                    conc[campo] = batch[origen]
            if batch.state in ('applying', 'applied'):
                conc.cs_batch_avance = _("Celdas aplicadas: %s de %s") % (
                    batch.processed, batch.total_rows)
            elif batch.state in ('posting', 'posted'):
                conc.cs_batch_avance = _("Asientos publicados: %s de %s") % (
                    batch.post_done, batch.post_total)
            elif batch.state in ('reconciling', 'reconciled'):
                conc.cs_batch_avance = _("Grupos conciliados: %s de %s") % (
                    batch.rec_done, batch.rec_total)

    def _compute_cs_requests(self):
        """Requests de la consulta EN CURSO (o de la última): desde su inicio."""
        Consulta = self.env['wis.conciliacion.consulta']
        for conc in self:
            conc.cs_requests = conc.cs_requests_error = 0
            conc.cs_ultimo_request = False
            if not conc.id or not conc.cs_started_at:
                continue
            dominio = [('conciliacion_id', '=', conc.id),
                       ('fecha', '>=', conc.cs_started_at)]
            conc.cs_requests = Consulta.search_count(dominio)
            conc.cs_requests_error = Consulta.search_count(
                dominio + [('resultado', '=', 'error')])
            ultimo = Consulta.search(dominio, limit=1)
            if ultimo:
                que = (_("código %s") % ultimo.producto) if ultimo.producto \
                    else (_("página %s") % ultimo.pagina)
                conc.cs_ultimo_request = "%s · %s · HTTP %s · %s ms" % (
                    ultimo.endpoint, que, ultimo.http_status or '—', ultimo.duracion_ms)

    # ------------------------------------------------------------------
    # La tabla de trabajo
    # ------------------------------------------------------------------
    def _cs_tabla(self):
        self.ensure_one()
        return "wis_concil_stock_%d" % self.id

    def _cs_tabla_existe(self):
        self.env.cr.execute("SELECT to_regclass(%s)", (self._cs_tabla(),))
        return bool(self.env.cr.fetchone()[0])

    def _cs_config(self):
        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not config or not config.apiLink:
            raise ValidationError(_("No se encuentran todos los datos para una consulta a la API"))
        return config

    def _cs_vals_consulta_en_cero(self):
        """Los contadores y punteros de la consulta, en blanco."""
        return {
            'cs_done': 0, 'cs_errors': 0, 'cs_con_diferencia': 0, 'cs_encontradas': 0,
            'cs_sin_stock': 0, 'cs_no_existe': 0, 'cs_err_filas': 0,
            'cs_started_at': False, 'cs_ended_at': False, 'cs_step': False,
            'cs_cancel_requested': False, 'cs_pagina': 0, 'cs_paginas_total': 0,
            'cs_etapa_consulta': False, 'cs_estrategia': False,
            'cs_pc_total': 0, 'cs_pc_done': 0, 'cs_pc_started_at': False,
            'cs_error_msg': False,
        }

    def action_armar_tabla(self):
        """Fase 0: la tabla de trabajo, que es «el Excel» de este proceso.

        Una fila por variante integrada con código WIS, con el stock que hoy
        tiene Odoo en la ubicación de reposición. La cantidad de WIS queda
        vacía: la completa la fase 1.
        """
        self.ensure_one()
        config = self._cs_config()
        if not config.ubicacionReponerStock:
            raise UserError(_("Falta la ubicación de reposición en la configuración de WIS: "
                              "es la ubicación contra la que se compara y se ajusta."))
        if self.fase == 'consultando' and self.estado != 'error':
            raise UserError(_("Hay una consulta en curso. Cancelala antes de rearmar la tabla."))

        inicio = fields.Datetime.now()
        cr = self.env.cr
        t = self._cs_tabla()
        cr.execute("DROP TABLE IF EXISTS %s" % t)
        cr.execute("""
            CREATE UNLOGGED TABLE {t} (
                row_num       bigserial PRIMARY KEY,
                product_id    integer NOT NULL,
                codigo_unico  varchar,
                location_id   integer NOT NULL,
                cantidad_odoo numeric,
                cantidad_wis  numeric,
                diferencia    numeric,
                estado        varchar DEFAULT 'pendiente',
                error         text,
                consultado    boolean DEFAULT false
            )
        """.format(t=t))
        cr.execute("CREATE INDEX {t}_pend_idx ON {t} (row_num) WHERE consultado = false".format(t=t))
        cr.execute("CREATE INDEX {t}_cod_idx ON {t} (codigo_unico)".format(t=t))

        # El stock de Odoo se toma UNA vez, acá, y de la ubicación de
        # reposición: es contra ésa que se ajusta.
        cr.execute("""
            INSERT INTO {t} (product_id, codigo_unico, location_id, cantidad_odoo)
            SELECT p.id, p.codigo_unico, %(ubic)s,
                   coalesce((SELECT sum(q.quantity) FROM stock_quant q
                              WHERE q.product_id = p.id AND q.location_id = %(ubic)s), 0)
              FROM product_product p
              JOIN product_template pt ON pt.id = p.product_tmpl_id
             WHERE p.integracion_wms IS TRUE
               AND p.active IS TRUE
               AND p.codigo_unico IS NOT NULL
               AND pt.type = 'product'
             ORDER BY p.id
        """.format(t=t), {'ubic': config.ubicacionReponerStock.id})
        total = cr.rowcount

        vals = self._cs_vals_consulta_en_cero()
        vals.update({'fase': 'tabla', 'estado': 'borrador', 'cs_total': total,
                     'cs_tabla_started_at': inicio,
                     'cs_tabla_ended_at': fields.Datetime.now()})
        self.write(vals)
        self._cs_log("Tabla armada con %d variantes integradas. Ubicación: %s."
                     % (total, config.ubicacionReponerStock.display_name))
        if not total:
            raise UserError(_("No hay variantes integradas con código WIS para conciliar."))
        return True

    # ------------------------------------------------------------------
    # Fase 1: la consulta a WIS, por tandas
    # ------------------------------------------------------------------
    def action_consultar_wis(self):
        """Arranca la consulta DE CERO. El cron la va llevando tanda por tanda.

        Para seguir una consulta cortada está «Reanudar», que respeta el punto
        en el que iba.
        """
        self.ensure_one()
        if self.fase not in ('tabla', 'consultado'):
            raise UserError(_("Primero armá la tabla de trabajo."))
        if not self._cs_tabla_existe():
            raise UserError(_("Ya no existe la tabla de trabajo. Volvé a armarla."))
        if self.cs_batch_size < 1:
            raise UserError(_("El tamaño de tanda debe ser mayor a cero."))
        self._cs_config()

        self.env.cr.execute("""
            UPDATE {t} SET cantidad_wis = NULL, diferencia = NULL, estado = 'pendiente',
                           error = NULL, consultado = false
        """.format(t=self._cs_tabla()))
        vals = self._cs_vals_consulta_en_cero()
        vals.update({'fase': 'consultando', 'estado': 'en_proceso',
                     'cs_started_at': fields.Datetime.now(),
                     'cs_step': _("En cola: arranca en segundos.")})
        self.write(vals)
        self._cs_log("Consulta a WIS iniciada.")
        self.env.cr.commit()
        self._cs_encolar_cron()
        return True

    def action_cancelar_consulta(self):
        self.ensure_one()
        if self.fase != 'consultando':
            raise UserError(_("No hay una consulta en curso."))
        if self.estado == 'error':
            # Detenida: no hay tanda en curso que tenga que ver la marca.
            self.write({'fase': 'tabla', 'estado': 'borrador', 'cs_cancel_requested': True,
                        'cs_step': False, 'cs_ended_at': fields.Datetime.now()})
            self._cs_log("Consulta cancelada con %d de %d variantes clasificadas."
                         % (self.cs_done, self.cs_total))
            return True
        self.write({'cs_cancel_requested': True})
        self._cs_log("Cancelación pedida: la consulta se detiene al terminar la tanda en curso.")
        return True

    def action_reanudar_consulta(self):
        """Sigue desde donde iba: página, etapa y filas ya clasificadas."""
        self.ensure_one()
        detenida = self.fase == 'consultando' and self.estado == 'error'
        cancelada = self.fase == 'tabla' and self.cs_cancel_requested
        if not (detenida or cancelada):
            raise UserError(_("No hay una consulta a medio hacer."))
        if not self._cs_tabla_existe():
            raise UserError(_("Ya no existe la tabla de trabajo. Volvé a armarla."))
        self._cs_config()
        self.write({'fase': 'consultando', 'estado': 'en_proceso',
                    'cs_cancel_requested': False, 'cs_error_msg': False,
                    'cs_ended_at': False,
                    'cs_step': _("En cola: arranca en segundos.")})
        self._cs_log("Consulta reanudada desde %s."
                     % (_("la página %d") % self.cs_pagina
                        if self.cs_etapa_consulta == 'listado'
                        else _("donde había quedado")))
        self.env.cr.commit()
        self._cs_encolar_cron()
        return True

    def _cs_encolar_cron(self):
        cron = self.env.ref('integracion_wis.ir_cron_wis_conciliar_stock_tandas',
                            raise_if_not_found=False)
        if not cron:
            return
        try:
            cron.sudo()._trigger()
        except Exception as e:
            _logger.warning("[WIS] No se pudo disparar el cron de conciliación de stock: %s", e)

    @api.model
    def _cron_consultar_tandas(self):
        """Avanza SÓLO lo que el usuario arrancó a mano.

        El cron no sale a conciliar por su cuenta: toma las conciliaciones que
        ya están en fase de consulta. Es el mismo criterio de la importación —
        el proceso lo dispara una persona, el cron sólo lo lleva adelante sin
        dejarle la pantalla colgada.

        🔴 Una consulta DETENIDA por error no se retoma sola. Antes se retomaba
        cada 5 minutos, repitiendo la misma tanda sin fin. Ahora espera a que
        alguien mire el motivo y apriete «Reanudar».
        """
        pendientes = self.search([('fase', '=', 'consultando'),
                                  ('estado', '!=', 'error')], limit=1)
        if not pendientes:
            return True
        pendientes._cs_varias_tandas()
        return True

    def _cs_varias_tandas(self):
        """Una tanda por ejecución, con reintento y commit propio."""
        self.ensure_one()
        self.invalidate_recordset(['fase', 'estado', 'cs_cancel_requested'])
        if self.fase != 'consultando' or self.estado == 'error':
            return
        if self.cs_cancel_requested:
            self.write({'fase': 'tabla', 'estado': 'borrador', 'cs_step': False,
                        'cs_ended_at': fields.Datetime.now()})
            self._cs_log("Consulta cancelada con %d de %d variantes clasificadas."
                         % (self.cs_done, self.cs_total))
            self.env.cr.commit()
            return
        quedan = 0
        for intento in range(1, REINTENTOS_TANDA + 1):
            try:
                quedan = self._cs_consultar_tanda()
                self.env.cr.commit()
                break
            except Exception as e:
                self.env.cr.rollback()
                self.env.clear()
                if isinstance(e, TransactionRollbackError) and intento < REINTENTOS_TANDA:
                    _logger.warning("[WIS] tanda de consulta chocó (%s); reintento %d de %d",
                                    e, intento + 1, REINTENTOS_TANDA)
                    continue
                self._cs_detener(e)
                return
        if not quedan:
            self._cs_finalizar_consulta()
            return
        self._cs_encolar_cron()

    def _cs_detener(self, error):
        """Deja la consulta DETENIDA, con el motivo a la vista, y commitea.

        Corre después del rollback de la tanda: lo que la tanda había escrito
        —incluido el request que falló— se perdió. El request se vuelve a
        registrar acá, desde la traza que viaja en la excepción.
        """
        traza = getattr(error, 'traza', None)
        if traza:
            self._cs_registrar(traza, self.cs_etapa_consulta or 'listado')
        motivo = tools.ustr(error)[:2000]
        self.write({'estado': 'error', 'cs_error_msg': motivo,
                    'cs_step': _("Detenida por un error. Revisá el motivo y reanudá.")})
        self._cs_log("La consulta se detuvo: %s. Queda para «Reanudar» desde donde iba."
                     % motivo[:500], nivel='error')
        self.env.cr.commit()
        _logger.error("[WIS][conciliación %s] consulta detenida: %s", self.id, motivo)

    def _cs_consultar_tanda(self):
        """Avanza una tanda de la etapa en la que esté la consulta.

        Devuelve distinto de cero mientras quede trabajo; 0 cuando terminó.
        """
        self.ensure_one()
        etapa = self.cs_etapa_consulta
        if not etapa or etapa == 'sondeo':
            return self._cs_sondear()
        if etapa == 'listado':
            return self._cs_tanda_listado()
        return self._cs_tanda_por_codigo()

    def _cs_registrar(self, traza, etapa, nuestras=0):
        return self.env['wis.conciliacion.consulta']._desde_traza(self, etapa, traza, nuestras)

    def _cs_pendientes(self):
        self.env.cr.execute("SELECT count(*) FROM {t} WHERE consultado = false"
                            .format(t=self._cs_tabla()))
        return self.env.cr.fetchone()[0]

    def _cs_sondear(self):
        """Mide cuántas páginas tiene el listado y elige la estrategia.

        Búsqueda exponencial y después binaria: ~2·log2(N) requests —unos 25
        segundos con 2.000 páginas—. Sin esto la barra no tiene total: el
        listado trae TODO lo que WIS tiene con stock, no sólo lo nuestro.
        """
        config = self._cs_config()
        self.write({'cs_etapa_consulta': 'sondeo',
                    'cs_step': _("Midiendo el listado de stock de WIS…")})

        def hay(pagina):
            traza = {}
            filas = config.consultaStockPaginado(pagina, traza=traza)
            self._cs_registrar(traza, 'sondeo')
            return bool(filas)

        total = 0
        if hay(1):
            bajo, alto = 1, 2
            while hay(alto):
                bajo, alto = alto, alto * 2
                if alto > 10 ** 7:
                    raise UserError(_("El listado de stock de WIS no termina: se "
                                      "cortó la medición en la página %d.") % bajo)
            while alto - bajo > 1:
                medio = (bajo + alto) // 2
                if hay(medio):
                    bajo = medio
                else:
                    alto = medio
            total = bajo

        pendientes = self._cs_pendientes()
        faltan_paginas = max(total - self.cs_pagina, 0)
        # La estrategia que necesita menos requests. El listado además deja
        # para la pasada por código lo que no traiga, pero eso son pocas.
        estrategia = 'listado' if faltan_paginas and faltan_paginas <= pendientes \
            else 'por_codigo'
        vals = {'cs_paginas_total': total, 'cs_estrategia': estrategia,
                'cs_etapa_consulta': estrategia}
        if estrategia == 'por_codigo':
            vals.update({'cs_pc_total': pendientes, 'cs_pc_done': 0,
                         'cs_pc_started_at': fields.Datetime.now()})
        self.write(vals)
        self._cs_log(
            "Listado de stock de WIS: %d página(s) (~%d productos con stock). "
            "Variantes a consultar: %d. Estrategia: %s."
            % (total, total * 10, pendientes,
               _("recorrer el listado") if estrategia == 'listado'
               else _("una consulta por código")))
        return 1

    def _cs_tanda_listado(self):
        """Trae una tanda del LISTADO de stock y la vuelca en la tabla.

        Va por `/ConsultaDeStock/GetData`, que devuelve 10 productos por
        request. Cuando el listado se termina, lo que no trajo pasa a la
        pasada por código: no se lo marca, se lo pregunta.
        """
        self.ensure_one()
        t0 = time.time()
        cr = self.env.cr
        t = self._cs_tabla()
        config = self._cs_config()
        campo = config.campo_stock_wis or 'stockGeneral'

        pagina = self.cs_pagina
        leidas, paginas, fin_del_listado = 0, 0, False
        while leidas < self.cs_batch_size and time.time() - t0 < TIEMPO_MAX_TANDA:
            traza = {}
            filas = config.consultaStockPaginado(pagina + 1, traza=traza)
            pagina += 1
            paginas += 1
            if not filas:
                self._cs_registrar(traza, 'listado')
                fin_del_listado = True
                break
            datos = [(f.get('producto'), f.get(campo)) for f in filas
                     if f.get('producto') is not None and f.get(campo) is not None]
            nuestras = 0
            if datos:
                execute_values(cr, """
                    UPDATE {t} s SET cantidad_wis = v.cantidad,
                                     diferencia = v.cantidad - coalesce(s.cantidad_odoo, 0),
                                     estado = 'ok', error = NULL, consultado = true
                      FROM (VALUES %s) AS v(codigo, cantidad)
                     WHERE s.codigo_unico = v.codigo AND s.consultado = false
                """.format(t=t), datos, template="(%s, %s::numeric)", page_size=len(datos))
                nuestras = cr.rowcount
            self._cs_registrar(traza, 'listado', nuestras)
            leidas += len(filas)

        vals = {'cs_pagina': pagina if not fin_del_listado else pagina - 1}
        # Si WIS creció desde que se midió, el total acompaña.
        if vals['cs_pagina'] > self.cs_paginas_total:
            vals['cs_paginas_total'] = vals['cs_pagina']
        vals['cs_step'] = _("Recorriendo el listado: página %d de %d") % (
            vals['cs_pagina'], vals.get('cs_paginas_total', self.cs_paginas_total))
        quedan = max(leidas, 1)
        if fin_del_listado:
            vals['cs_paginas_total'] = vals['cs_pagina']
            pendientes = self._cs_pendientes()
            self._cs_log("Listado de stock recorrido entero: %d página(s). %d variante(s) "
                         "no aparecen: se consultan una por una para saber si WIS no "
                         "tiene stock o no las conoce." % (vals['cs_pagina'], pendientes))
            if pendientes:
                vals.update({'cs_etapa_consulta': 'por_codigo', 'cs_pc_total': pendientes,
                             'cs_pc_done': 0, 'cs_pc_started_at': fields.Datetime.now()})
            else:
                quedan = 0
        vals.update(self._cs_contadores())
        self.write(vals)
        _logger.info("[WIS][conciliación %s] tanda del listado: %d página(s), %d fila(s) "
                     "en %.1fs | clasificadas %d de %d", self.id, paginas, leidas,
                     time.time() - t0, vals['cs_done'], self.cs_total)
        return quedan

    def _cs_tanda_por_codigo(self):
        """Consulta una tanda de variantes de a una, filtrando por código.

        Con el filtro WIS distingue lo que el listado confunde:
            con stock               → 'ok'
            existe y no tiene stock → 'sin_stock', cantidad 0: ENTRA al ajuste
            no existe               → 'no_existe': queda fuera
            error                   → 'error': queda fuera
        """
        self.ensure_one()
        t0 = time.time()
        cr = self.env.cr
        t = self._cs_tabla()
        config = self._cs_config()
        cr.execute("SELECT row_num, codigo_unico FROM {t} WHERE consultado = false "
                   "ORDER BY row_num LIMIT %s".format(t=t), (self.cs_batch_size,))
        filas = cr.fetchall()
        if not filas:
            self.write(self._cs_contadores())
            return 0

        hechas, seguidos = 0, 0
        for row_num, codigo in filas:
            if time.time() - t0 >= TIEMPO_MAX_TANDA:
                break
            traza = {}
            try:
                clase, cantidad = config.consultaStockProducto(codigo, traza=traza)
            except ErrorConsultaStockWIS as e:
                seguidos += 1
                if seguidos >= ERRORES_SEGUIDOS_MAX:
                    raise ErrorConsultaStockWIS(dict(e.traza, mensaje=_(
                        "%(n)d errores seguidos consultando por código; el último: "
                        "%(msg)s") % {'n': seguidos, 'msg': e.traza.get('mensaje')}))
                self._cs_registrar(e.traza, 'por_codigo')
                cr.execute("UPDATE {t} SET estado = 'error', error = %s, consultado = true "
                           "WHERE row_num = %s".format(t=t),
                           ((e.traza.get('mensaje') or '')[:1000], row_num))
                hechas += 1
                continue
            seguidos = 0
            self._cs_registrar(traza, 'por_codigo', 0 if clase == 'no_existe' else 1)
            if clase == 'no_existe':
                cr.execute("UPDATE {t} SET estado = 'no_existe', consultado = true, "
                           "error = 'WIS no conoce el producto.' WHERE row_num = %s"
                           .format(t=t), (row_num,))
            else:
                cr.execute("""
                    UPDATE {t} SET cantidad_wis = %s,
                                   diferencia = %s - coalesce(cantidad_odoo, 0),
                                   estado = %s, error = NULL, consultado = true
                     WHERE row_num = %s
                """.format(t=t), (cantidad, cantidad, clase, row_num))
            hechas += 1

        vals = self._cs_contadores()
        pc_done = self.cs_pc_done + hechas
        vals.update({'cs_pc_done': pc_done,
                     'cs_step': _("Consultando por código: %d de %d")
                     % (pc_done, self.cs_pc_total)})
        self.write(vals)
        _logger.info("[WIS][conciliación %s] tanda por código: %d en %.1fs | %d de %d",
                     self.id, hechas, time.time() - t0, pc_done, self.cs_pc_total)
        return self._cs_pendientes()

    def _cs_contadores(self):
        """Los contadores de la pantalla, contados sobre la tabla."""
        self.env.cr.execute("""
            SELECT count(*) FILTER (WHERE consultado),
                   count(*) FILTER (WHERE estado = 'ok'),
                   count(*) FILTER (WHERE estado = 'sin_stock'),
                   count(*) FILTER (WHERE estado = 'no_existe'),
                   count(*) FILTER (WHERE estado IN ('error', 'sin_respuesta')),
                   count(*) FILTER (WHERE estado IN %s AND diferencia <> 0)
              FROM {t}
        """.format(t=self._cs_tabla()), (ESTADOS_AJUSTE,))
        hechas, ok, sin_stock, no_existe, errores, con_dif = self.env.cr.fetchone()
        return {'cs_done': hechas, 'cs_encontradas': ok, 'cs_sin_stock': sin_stock,
                'cs_no_existe': no_existe, 'cs_err_filas': errores,
                'cs_errors': no_existe + errores, 'cs_con_diferencia': con_dif}

    def _cs_finalizar_consulta(self):
        self.ensure_one()
        resumen = self._cs_resumen()
        vals = self._cs_contadores()
        vals.update({'fase': 'consultado', 'estado': 'completado',
                     'cs_ended_at': fields.Datetime.now(), 'cs_step': False})
        self.write(vals)
        self._cs_log(
            "Consulta terminada. Con stock en WIS: %d | Sin stock en WIS: %d | "
            "Con diferencia: %d | Fuera del ajuste (no existen o con error): %d | "
            "Quedarían en cero: %d."
            % (vals['cs_encontradas'], vals['cs_sin_stock'], resumen['con_diferencia'],
               resumen['sin_respuesta'], resumen['a_cero']),
            nivel='warning' if resumen['sin_respuesta'] else 'info')
        self.env.cr.commit()

    # ------------------------------------------------------------------
    # Fase 2: entregar el conteo al motor de ajuste de inventario
    # ------------------------------------------------------------------
    def action_generar_ajuste(self):
        """Arma el ajuste con las diferencias y lo deja listo para aplicar.

        No aplica ni publica nada: crea el batch de inventario con el conteo y
        lo deja en «listo», para que una persona mire las diferencias y decida.
        De ahí en adelante son las acciones del motor —aplicar, publicar,
        conciliar—, todas por tandas.

        🔴 El conteo que se entrega es `cantidad_wis`, **el stock que debe
        quedar**, no la diferencia. Y sólo van las filas con una respuesta de
        WIS sobre ese producto: stock informado, o «sin stock» por código.
        """
        self.ensure_one()
        if self.fase != 'consultado':
            raise UserError(_("Primero hay que terminar la consulta a WIS."))

        Batch = self.env.get('forum.import.batch')
        if Batch is None:
            raise UserError(_(
                "El motor de ajuste de inventario (forum_partner_import) no está "
                "instalado en esta base. La consulta a WIS quedó hecha y las "
                "diferencias se pueden revisar, pero el ajuste hay que armarlo a mano."))

        config = self._cs_config()
        resumen = self._cs_resumen()
        if not resumen['con_diferencia']:
            raise UserError(_("No hay diferencias que ajustar."))

        # Tope de seguridad: un WIS a medio responder se parece mucho a un
        # inventario vacío, y poner en cero no se deshace con un undo.
        if resumen['consultadas'] and self.cs_tope_a_cero_pct:
            pct = 100.0 * resumen['a_cero'] / resumen['consultadas']
            if pct > self.cs_tope_a_cero_pct:
                raise UserError(_(
                    "El ajuste dejaría en cero %(n)d de %(t)d variantes (%(pct).1f %%), "
                    "más que el tope de %(tope)d %%. Revisá la consulta antes de "
                    "seguir: si WIS contestó a medias, esto vacía stock real.",
                    n=resumen['a_cero'], t=resumen['consultadas'], pct=pct,
                    tope=self.cs_tope_a_cero_pct))

        inicio = fields.Datetime.now()
        minimo = config.diferenciaMinima or 0
        self.env.cr.execute("""
            SELECT product_id, location_id, cantidad_wis
              FROM {t}
             WHERE estado IN %(estados)s AND diferencia <> 0 AND abs(diferencia) >= %(min)s
             ORDER BY row_num
        """.format(t=self._cs_tabla()), {'min': minimo, 'estados': ESTADOS_AJUSTE})
        filas = [{'product_id': p, 'location_id': l, 'cantidad': float(c)}
                 for p, l, c in self.env.cr.fetchall()]

        motivo = "Conciliación de stock WIS %s (#%d)" % (
            fields.Date.to_string(fields.Date.context_today(self)), self.id)
        batch = Batch.create({
            'name': motivo,
            'import_type': 'inventario',
            'inventory_user_id': self.env.user.id,
            'inventory_reason': motivo,
        })
        batch.cargar_celdas_externas(filas, origen="WIS (conciliación #%d)" % self.id)

        self.write({'fase': 'ajuste', 'cs_batch_id': batch.id,
                    'cs_batch_nombre': batch.display_name,
                    'cs_ajuste_started_at': inicio,
                    'cs_ajuste_ended_at': fields.Datetime.now(),
                    'cs_ajuste_celdas': len(filas)})
        self._cs_log("Ajuste generado con %d celda(s) en el batch %s. "
                     "Revisá las diferencias y aplicá desde ahí."
                     % (len(filas), batch.display_name))
        return self.action_abrir_batch()

    # --- las fases del motor, disparadas desde acá -----------------------
    # Son delegaciones de una línea: la lógica vive en el motor y no se copia.
    # Están para no obligar a saltar de pantalla, que era lo pedido: todo el
    # proceso se maneja desde Conciliación de Stock.
    def _cs_batch(self):
        self.ensure_one()
        Batch = self.env.get('forum.import.batch')
        if Batch is None or not self.cs_batch_id:
            raise UserError(_("Todavía no se generó el ajuste."))
        batch = Batch.browse(self.cs_batch_id).exists()
        if not batch:
            raise UserError(_("El ajuste ya no existe."))
        return batch

    def action_aplicar_ajuste(self):
        """Fase 3: crea quants, capas de valuación y asientos EN BORRADOR."""
        return self._cs_batch().action_aplicar_ajuste()

    def action_publicar_asientos(self):
        """Fase 4: publica los asientos, por tandas."""
        return self._cs_batch().action_publicar_asientos()

    def action_conciliar_asientos(self):
        """Fase 5: concilia las líneas de los asientos."""
        return self._cs_batch().action_conciliar()

    def action_abrir_batch(self):
        """Abre el batch del ajuste. Por acción y no por Many2one: ver arriba."""
        self.ensure_one()
        if not self.cs_batch_id:
            raise UserError(_("Todavía no se generó el ajuste."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'forum.import.batch',
            'res_id': self.cs_batch_id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Detalle por variante
    # ------------------------------------------------------------------
    def action_descargar_detalle(self):
        """La tabla de trabajo en un Excel: una fila por variante.

        La tabla es SQL suelta —a propósito, por volumen—, así que no hay lista
        de Odoo que la muestre. Esto es lo que alguien revisa antes de aplicar.
        """
        self.ensure_one()
        if not self._cs_tabla_existe():
            raise UserError(_("Todavía no hay tabla de trabajo."))
        import xlsxwriter

        self.env.cr.execute("""
            SELECT s.codigo_unico, p.default_code, p.barcode, s.product_id,
                   s.cantidad_odoo, s.cantidad_wis, s.diferencia, s.estado, s.error
              FROM {t} s
              JOIN product_product p ON p.id = s.product_id
             ORDER BY s.row_num
        """.format(t=self._cs_tabla()))
        filas = self.env.cr.fetchall()
        nombres = dict((p.id, p.display_name) for p in self.env['product.product']
                       .browse({f[3] for f in filas}).with_context(active_test=False))
        etiquetas = {
            'pendiente': _("Sin consultar"), 'ok': _("Con stock en WIS"),
            'sin_stock': _("Sin stock en WIS (entra como 0)"),
            'no_existe': _("No existe en WIS (fuera del ajuste)"),
            'error': _("Error (fuera del ajuste)"),
            'sin_respuesta': _("Sin respuesta (fuera del ajuste)"),
        }

        salida = io.BytesIO()
        libro = xlsxwriter.Workbook(salida, {'in_memory': True})
        hoja = libro.add_worksheet(_("Detalle"))
        negrita = libro.add_format({'bold': True, 'bg_color': '#DDDDDD'})
        encabezado = [_("Código WIS"), _("Referencia"), _("Código de barras"), _("Variante"),
                      _("Stock Odoo"), _("Stock WIS"), _("Diferencia"), _("Estado"),
                      _("Detalle")]
        hoja.write_row(0, 0, encabezado, negrita)
        for i, (cod, ref, barra, pid, odoo, wis, dif, estado, error) in enumerate(filas, 1):
            hoja.write_row(i, 0, [
                cod or '', ref or '', barra or '', nombres.get(pid, pid),
                float(odoo) if odoo is not None else '',
                float(wis) if wis is not None else '',
                float(dif) if dif is not None else '',
                etiquetas.get(estado, estado), error or ''])
        hoja.autofilter(0, 0, len(filas), len(encabezado) - 1)
        hoja.freeze_panes(1, 0)
        hoja.set_column(0, 2, 16)
        hoja.set_column(3, 3, 45)
        hoja.set_column(4, 6, 11)
        hoja.set_column(7, 8, 34)
        libro.close()

        nombre = "conciliacion_stock_%d.xlsx" % self.id
        adjunto = self.env['ir.attachment'].create({
            'name': nombre,
            'datas': base64.b64encode(salida.getvalue()),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {'type': 'ir.actions.act_url', 'target': 'self',
                'url': '/web/content/%d?download=true' % adjunto.id}

    # ------------------------------------------------------------------
    # Resumen y log
    # ------------------------------------------------------------------
    def _cs_resumen(self):
        """Los números que decide mirar alguien antes de aplicar nada.

        `consultadas` son las que tienen respuesta de WIS sobre el producto
        (stock informado o «sin stock» por código). `sin_respuesta` son las
        que quedan fuera del ajuste: no existen en WIS, o dieron error.
        """
        self.ensure_one()
        if not self._cs_tabla_existe():
            return {'consultadas': 0, 'con_diferencia': 0, 'sin_respuesta': 0, 'a_cero': 0,
                    'sin_stock': 0}
        config = self._cs_config()
        minimo = config.diferenciaMinima or 0
        self.env.cr.execute("""
            SELECT count(*) FILTER (WHERE estado IN %(estados)s),
                   count(*) FILTER (WHERE estado IN %(estados)s AND diferencia <> 0
                                      AND abs(diferencia) >= %(min)s),
                   count(*) FILTER (WHERE estado IN ('sin_respuesta', 'no_existe', 'error')),
                   count(*) FILTER (WHERE estado IN %(estados)s AND cantidad_wis = 0
                                      AND cantidad_odoo > 0),
                   count(*) FILTER (WHERE estado = 'sin_stock')
              FROM {t}
        """.format(t=self._cs_tabla()), {'min': minimo, 'estados': ESTADOS_AJUSTE})
        consultadas, con_dif, sin_resp, a_cero, sin_stock = self.env.cr.fetchone()
        return {'consultadas': consultadas or 0, 'con_diferencia': con_dif or 0,
                'sin_respuesta': sin_resp or 0, 'a_cero': a_cero or 0,
                'sin_stock': sin_stock or 0}

    def _cs_log(self, texto, nivel='info'):
        self.ensure_one()
        self.env['logs.conciliacion.stock'].create({
            'texto': texto, 'nivel': nivel, 'conciliacion_id': self.id,
        })

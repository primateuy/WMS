# -*- coding: utf-8 -*-
"""Conciliación de stock contra WIS, por fases y por tandas.

Reemplaza la corrida de un solo tiro por el mismo esquema que ya usa la
importación masiva de inventario: una tabla de trabajo, fases que el usuario
dispara **a mano**, y tandas con puntero para no dejar una transacción abierta
ni un cron consultando WIS por su cuenta.

    Fase 0  Armar la tabla     una fila por variante integrada con código WIS
    Fase 1  Consultar WIS      por tandas: stock de WIS y diferencia contra Odoo
    Fase 2  Generar el ajuste  se entrega al motor de inventario (ver README)

🔴 **La fila que WIS no contesta NO entra al ajuste.** Sólo un 0 explícito de
WIS cuenta como cero. Tomar el silencio como «no hay stock» pondría en cero
productos que simplemente no se pudieron consultar, y eso no se deshace con un
undo: son movimientos de inventario con su valuación y sus asientos.
"""
import logging
import time

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from psycopg2.extensions import TransactionRollbackError

_logger = logging.getLogger(__name__)

# Variantes por tanda de consulta a WIS. Cada una es UN request
# (`/Producto/GetProducto` no acepta lista), así que la tanda es el tamaño del
# commit y del bloque que se reintenta, no un lote de red.
LOTE_CONSULTA = 200
REINTENTOS_TANDA = 3


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
        help="Cada variante es un request a WIS: /Producto/GetProducto no acepta "
             "lista. La tanda define cada cuánto se hace commit y cuánto se "
             "rehace si hay que reintentar, no un lote de red.",
    )
    cs_total = fields.Integer(string='Variantes a consultar', readonly=True, copy=False)
    cs_done = fields.Integer(string='Consultadas', readonly=True, copy=False)
    cs_errors = fields.Integer(string='Sin respuesta', readonly=True, copy=False)
    cs_con_diferencia = fields.Integer(string='Con diferencia', readonly=True, copy=False)
    cs_step = fields.Char(string='Etapa', readonly=True, copy=False)
    cs_started_at = fields.Datetime(string='Inicio de la consulta', readonly=True, copy=False)
    cs_ended_at = fields.Datetime(string='Fin de la consulta', readonly=True, copy=False)
    cs_cancel_requested = fields.Boolean(string='Cancelación pedida', readonly=True, copy=False)
    cs_tope_a_cero_pct = fields.Integer(
        string='Tope de variantes a cero (%)', default=10,
        help="Si el ajuste dejaría en cero más de este porcentaje de las "
             "variantes consultadas, no se genera y hay que revisar. Un WIS a "
             "medio responder se parece mucho a un inventario vacío.",
    )
    # 🔴 Referencia SUELTA al batch de inventario, a propósito: un Many2one
    # obligaría a este módulo —que es compartido entre clientes— a depender de
    # `forum_partner_import`, que es de Forum. Se guarda el id y se abre por
    # acción, que no necesita el comodel declarado.
    cs_batch_id = fields.Integer(string='Batch de ajuste', readonly=True, copy=False)
    cs_batch_nombre = fields.Char(string='Nombre del batch', readonly=True, copy=False)

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
        if self.fase in ('consultando',):
            raise UserError(_("Hay una consulta en curso. Cancelala antes de rearmar la tabla."))

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

        self.write({
            'fase': 'tabla', 'estado': 'borrador',
            'cs_total': total, 'cs_done': 0, 'cs_errors': 0, 'cs_con_diferencia': 0,
            'cs_started_at': False, 'cs_ended_at': False, 'cs_step': False,
            'cs_cancel_requested': False,
        })
        self._cs_log("Tabla armada con %d variantes integradas. Ubicación: %s."
                     % (total, config.ubicacionReponerStock.display_name))
        if not total:
            raise UserError(_("No hay variantes integradas con código WIS para conciliar."))
        return True

    # ------------------------------------------------------------------
    # Fase 1: la consulta a WIS, por tandas
    # ------------------------------------------------------------------
    def action_consultar_wis(self):
        """Arranca la consulta. El cron la va llevando tanda por tanda."""
        self.ensure_one()
        if self.fase not in ('tabla', 'consultado'):
            raise UserError(_("Primero armá la tabla de trabajo."))
        if not self._cs_tabla_existe():
            raise UserError(_("Ya no existe la tabla de trabajo. Volvé a armarla."))
        if self.cs_batch_size < 1:
            raise UserError(_("El tamaño de tanda debe ser mayor a cero."))
        self._cs_config()

        vals = {'fase': 'consultando', 'estado': 'en_proceso',
                'cs_cancel_requested': False, 'cs_ended_at': False}
        if not self.cs_started_at:
            vals['cs_started_at'] = fields.Datetime.now()
        self.write(vals)
        self._cs_log("Consulta a WIS iniciada. Tandas de %d." % self.cs_batch_size)
        self.env.cr.commit()
        self._cs_encolar_cron()
        return True

    def action_cancelar_consulta(self):
        self.ensure_one()
        self.write({'cs_cancel_requested': True})
        self._cs_log("Cancelación pedida: la consulta se detiene al terminar la tanda en curso.")
        return True

    def action_reanudar_consulta(self):
        self.ensure_one()
        if self.fase != 'consultando':
            raise UserError(_("No hay una consulta a medio hacer."))
        self.write({'cs_cancel_requested': False})
        return self.action_consultar_wis()

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
        """
        pendientes = self.search([('fase', '=', 'consultando')], limit=1)
        if not pendientes:
            return True
        pendientes._cs_varias_tandas()
        return True

    def _cs_varias_tandas(self):
        """Una tanda por ejecución, con reintento y commit propio."""
        self.ensure_one()
        self.invalidate_recordset(['fase', 'cs_cancel_requested'])
        if self.fase != 'consultando':
            return
        if self.cs_cancel_requested:
            self.write({'fase': 'tabla', 'estado': 'borrador', 'cs_step': False,
                        'cs_ended_at': fields.Datetime.now()})
            self._cs_log("Consulta cancelada con %d de %d variantes."
                         % (self.cs_done, self.cs_total))
            self.env.cr.commit()
            return
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
                self.write({'fase': 'consultando', 'estado': 'error'})
                self._cs_log("ERROR en la tanda de consulta: %s" % e, nivel='error')
                self.env.cr.commit()
                _logger.exception("[WIS] tanda de consulta fallida")
                return
        if not quedan:
            self._cs_finalizar_consulta()
            return
        self._cs_encolar_cron()

    def _cs_consultar_tanda(self):
        """Consulta una tanda. Devuelve cuántas filas quedaron pendientes."""
        self.ensure_one()
        t0 = time.time()
        cr = self.env.cr
        t = self._cs_tabla()
        config = self._cs_config()

        cr.execute("""SELECT row_num, product_id, codigo_unico, cantidad_odoo
                        FROM {t} WHERE consultado = false
                       ORDER BY row_num LIMIT %s""".format(t=t), (self.cs_batch_size,))
        filas = cr.fetchall()
        if not filas:
            return 0

        hechas = errores = con_dif = 0
        for row_num, product_id, codigo, cantidad_odoo in filas:
            try:
                cantidad_wis = config.consultaStockCodigo(codigo)
            except Exception as e:
                # 🔴 Sin respuesta NO es cero: la fila queda marcada y fuera
                # del ajuste.
                cr.execute("""UPDATE {t} SET consultado = true, estado = 'sin_respuesta',
                                     error = %s WHERE row_num = %s""".format(t=t),
                           (str(e)[:500], row_num))
                errores += 1
                continue
            if cantidad_wis is None:
                cr.execute("""UPDATE {t} SET consultado = true, estado = 'sin_respuesta',
                                     error = 'WIS no devolvió cantidad para el producto.'
                               WHERE row_num = %s""".format(t=t), (row_num,))
                errores += 1
                continue
            diferencia = float(cantidad_wis) - float(cantidad_odoo or 0)
            cr.execute("""UPDATE {t} SET consultado = true, estado = 'ok',
                                 cantidad_wis = %s, diferencia = %s, error = NULL
                           WHERE row_num = %s""".format(t=t),
                       (cantidad_wis, diferencia, row_num))
            hechas += 1
            if abs(diferencia) >= (config.diferenciaMinima or 0):
                if diferencia:
                    con_dif += 1

        self.write({
            'cs_done': self.cs_done + hechas + errores,
            'cs_errors': self.cs_errors + errores,
            'cs_con_diferencia': self.cs_con_diferencia + con_dif,
            'cs_step': _("Consultando WIS: %d de %d") % (
                self.cs_done + hechas + errores, self.cs_total),
        })
        _logger.info("[WIS][conciliación %s] tanda de %d variantes en %.1fs "
                     "(%.0f ms/variante) | ok=%d sin respuesta=%d",
                     self.id, len(filas), time.time() - t0,
                     1000.0 * (time.time() - t0) / len(filas), hechas, errores)
        return len(filas) if len(filas) == self.cs_batch_size else 0

    def _cs_finalizar_consulta(self):
        self.ensure_one()
        resumen = self._cs_resumen()
        self.write({'fase': 'consultado', 'estado': 'completado',
                    'cs_ended_at': fields.Datetime.now(), 'cs_step': False})
        self._cs_log(
            "Consulta terminada. Consultadas: %d | Con diferencia: %d | "
            "Sin respuesta: %d | Quedarían en cero: %d."
            % (resumen['consultadas'], resumen['con_diferencia'],
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
        quedar**, no la diferencia. Y sólo van las filas que WIS contestó: una
        fila sin respuesta no es un cero.
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

        minimo = config.diferenciaMinima or 0
        self.env.cr.execute("""
            SELECT product_id, location_id, cantidad_wis
              FROM {t}
             WHERE estado = 'ok' AND diferencia <> 0 AND abs(diferencia) >= %(min)s
             ORDER BY row_num
        """.format(t=self._cs_tabla()), {'min': minimo})
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
                    'cs_batch_nombre': batch.display_name})
        self._cs_log("Ajuste generado con %d celda(s) en el batch %s. "
                     "Revisá las diferencias y aplicá desde ahí."
                     % (len(filas), batch.display_name))
        return self.action_abrir_batch()

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
    # Resumen y log
    # ------------------------------------------------------------------
    def _cs_resumen(self):
        """Los números que decide mirar alguien antes de aplicar nada."""
        self.ensure_one()
        if not self._cs_tabla_existe():
            return {'consultadas': 0, 'con_diferencia': 0, 'sin_respuesta': 0, 'a_cero': 0}
        config = self._cs_config()
        minimo = config.diferenciaMinima or 0
        self.env.cr.execute("""
            SELECT count(*) FILTER (WHERE estado = 'ok'),
                   count(*) FILTER (WHERE estado = 'ok' AND diferencia <> 0
                                      AND abs(diferencia) >= %(min)s),
                   count(*) FILTER (WHERE estado = 'sin_respuesta'),
                   count(*) FILTER (WHERE estado = 'ok' AND cantidad_wis = 0
                                      AND cantidad_odoo > 0)
              FROM {t}
        """.format(t=self._cs_tabla()), {'min': minimo})
        consultadas, con_dif, sin_resp, a_cero = self.env.cr.fetchone()
        return {'consultadas': consultadas or 0, 'con_diferencia': con_dif or 0,
                'sin_respuesta': sin_resp or 0, 'a_cero': a_cero or 0}

    def _cs_log(self, texto, nivel='info'):
        self.ensure_one()
        self.env['logs.conciliacion.stock'].create({
            'texto': texto, 'nivel': nivel, 'conciliacion_id': self.id,
        })

# -*- coding: utf-8 -*-
"""Cola de integración de productos con WIS.

La integración de un producto con muchas variantes puede demorar varios
minutos. Este modelo permite encolar variantes y procesarlas por lote desde un
cron, para que el usuario no quede bloqueado esperando a WIS.

**Nada llama a WIS dentro de la transacción del usuario**: el alta de un
producto, tildar la casilla en la ficha, agregar un atributo, la acción masiva y
el botón «Integrar con prioridad» de la ficha encolan todos acá. El botón entra
primero en la fila (`PRIORIDAD_BOTON`): antes mandaba a WIS en el momento y la
pantalla quedaba esperando minutos, con las filas tomadas. Al encolar se dispara
el cron (`_disparar_cron`), así que el trabajo arranca en segundos y no en el
próximo ciclo de 5 minutos.

Una variante sólo queda «Integrado» si WIS la aceptó, con su código de barras.
Si WIS la rechaza queda Pendiente con error (en rojo). Por defecto no se reenvía
sola —reenviar el mismo dato no lo corrige y WIS pidió evitarlo (12-oct-2026)—:
sale con «Reintentar», y entonces antes que lo que no se mandó todavía. Con
«Reintentar solos los rechazos de WIS» se reintenta cada 15 min hasta el tope.
Si WIS no atiende —credenciales, conexión, timeout— no es culpa de ninguna
variante: no se marca nada y los envíos a WIS quedan en pausa (5, 15, 60 min),
compartida con la cola de operaciones.

No se usa `queue_job` de la OCA porque no está disponible en este addons-path:
esta cola cubre el caso concreto (integración de productos) sin sumar
infraestructura de servidor. Si más adelante entra `queue_job`, se reemplaza el
cron por un job sin tocar los call-sites, que solo conocen `_encolar()`.
"""
import logging
import threading
import time
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from .models import WisErrorGeneral

_logger = logging.getLogger(__name__)

# Prioridades: menor número = se procesa antes.
PRIORIDAD_BOTON = 0     # «Integrar con prioridad» en la ficha: el usuario lo pidió ya
PRIORIDAD_URGENTE = 1   # variantes que traban una Orden de Compra
PRIORIDAD_NORMAL = 10   # integración masiva pedida por el usuario
PRIORIDAD_BAJA = 20     # el resto de las variantes de un template

# Espera entre reintentos automáticos de una variante que WIS rechazó: sin ella,
# un producto con datos malos encabezaría todas las corridas.
ESPERA_REINTENTO = timedelta(minutes=15)
# Una entrada «procesando» más vieja que esto quedó de una corrida que se cortó
# (worker reiniciado, límite de tiempo) y vuelve a pendiente.
PROCESANDO_COLGADA = timedelta(minutes=15)
# Llamadas a WIS por lote (productos + códigos de barras): el presupuesto de la corrida
# deja lugar para que el último lote termine aunque las dos lleguen al timeout.
LLAMADAS_POR_LOTE = 2
MAX_INTENTOS_POR_DEFECTO = 5


def _errores_por_codigo(resultado):
    """{código de producto: motivo} de un resultado de `insertarProductosMasivo`.

    Si sólo trae el detalle en texto, cada línea es «<código>: <motivo>».
    """
    resultado = resultado or {}
    return resultado.get('errores_por_codigo') or {
        detalle.split(': ', 1)[0]: detalle
        for detalle in resultado.get('errores_detalle') or []}


def _en_test():
    return getattr(threading.current_thread(), 'testing', False)


class WisSyncQueue(models.Model):
    _name = 'wis.sync.queue'
    _description = 'Cola de integración de productos con WIS'
    _order = 'prioridad asc, create_date asc, id asc'

    product_id = fields.Many2one(
        'product.product',
        string='Variante',
        required=True,
        index=True,
        ondelete='cascade',
    )

    estado = fields.Selection([
        ('pendiente', 'Pendiente'),
        ('procesando', 'Procesando'),
        ('hecho', 'Integrado'),
        ('cancelado', 'Cancelado'),
    ], string='Estado', default='pendiente', required=True, index=True,
        help='Integrado sólo cuando WIS aceptó la variante y su código de barras.')

    subestado = fields.Selection([
        ('sin_enviar', 'Sin enviar'),
        ('error', 'Con error'),
    ], string='Subestado', default='sin_enviar', required=True, index=True,
        help='Sin enviar: todavía no se mandó. Con error: WIS la rechazó; sale con '
             '«Reintentar» (o sola cada 15 minutos, si está habilitado en la '
             'configuración), antes que las sin enviar.')

    prioridad = fields.Integer(
        string='Prioridad',
        default=PRIORIDAD_NORMAL,
        index=True,
        help='Menor número, se procesa antes. 0 = pedido con el botón de la ficha, '
             '1 = urgente (traba una Orden de Compra), 10 = normal, 20 = baja.',
    )

    origen = fields.Selection([
        ('boton', 'Botón de la ficha'),
        ('oc', 'Orden de Compra'),
        ('masiva', 'Integración masiva'),
        ('template', 'Alta de variantes'),
        ('manual', 'Manual'),
    ], string='Origen', default='manual', required=True)

    origen_ref = fields.Char(
        string='Referencia de origen',
        help='Documento que originó el encolado (ej. el número de la Orden de Compra).',
    )

    intentos = fields.Integer(string='Intentos', default=0, readonly=True)
    ultimo_error = fields.Text(string='Último error', readonly=True)
    fecha_procesado = fields.Datetime(string='Fecha de procesamiento', readonly=True)
    proximo_intento = fields.Datetime(
        string='Próximo reintento', readonly=True,
        help='Antes de esta hora la cola no vuelve a intentar sola esta variante.')

    codigo_unico = fields.Char(
        related='product_id.codigo_unico',
        string='Código WIS',
        readonly=True,
    )

    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        required=True,
        ondelete='cascade',
        default=lambda self: self.env.company,
    )

    @api.depends('product_id', 'estado')
    def _compute_display_name(self):
        for record in self:
            record.display_name = f"{record.product_id.display_name or ''} [{record.estado}]"

    # ------------------------------------------------------------------
    # Encolado
    # ------------------------------------------------------------------
    @api.model
    def _encolar(self, variantes, origen='manual', origen_ref='', prioridad=PRIORIDAD_NORMAL):
        """Encola variantes para integrar con WIS. Devuelve las entradas creadas.

        🔴 **Sólo inserta: nunca modifica filas que ya están en la cola.** Antes
        subía la prioridad de las pendientes con un `write`, y esas mismas filas
        las podía estar escribiendo el cron: una de las dos transacciones caía
        por «could not serialize» y Odoo repetía la operación del usuario entera.
        Ahora, si una variante ya está en cola más abajo, se agrega otra entrada
        más arriba y el cron descarta la vieja (`_depurar_pendientes`).

        No se agrega nada si la variante ya tiene una entrada que va a salir
        igual o antes (sin enviar con igual o mejor prioridad, o en proceso). Una
        con error sólo se adelanta con el botón, que es un pedido explícito.
        """
        variantes = variantes.filtered(lambda v: v.type == 'product')
        if not variantes:
            return self.browse()

        existentes = self.search([
            ('product_id', 'in', variantes.ids),
            ('estado', 'in', ('pendiente', 'procesando')),
        ])
        cubiertas = existentes.filtered(
            lambda e: e.estado == 'procesando' and prioridad != PRIORIDAD_BOTON
            or e.estado == 'pendiente' and e.subestado == 'sin_enviar' and e.prioridad <= prioridad
            or e.estado == 'pendiente' and e.subestado == 'error' and prioridad != PRIORIDAD_BOTON
        ).mapped('product_id')

        nuevas = variantes - cubiertas
        if not nuevas:
            return self.browse()

        entradas = self.create([{
            'product_id': variante.id,
            'origen': origen,
            'origen_ref': origen_ref,
            'prioridad': prioridad,
            'company_id': self.env.company.id,
        } for variante in nuevas])

        _logger.info("[WIS] Encoladas %d variantes (origen=%s ref=%s prioridad=%d)",
                     len(entradas), origen, origen_ref or '-', prioridad)
        self._disparar_cron()
        return entradas

    @api.model
    def _disparar_cron(self):
        """Pide que el cron corra apenas termine esta transacción.

        Sin esto la cola espera hasta el próximo ciclo —5 minutos—, y para un
        producto de tres variantes ese retraso se ve como si no hubiera pasado
        nada. Con el trigger el trabajo arranca a los segundos y encolar deja
        de ser un compromiso entre no bloquear al usuario y que el producto
        quede integrado ya.

        Nunca puede tumbar al que encola: si el cron no está o falla el
        disparo, la cola igual se procesa en el ciclo normal.
        """
        cron = self.env.ref('integracion_wis.ir_cron_wis_procesar_cola_productos',
                            raise_if_not_found=False)
        if not cron:
            return
        try:
            cron.sudo()._trigger()
        except Exception as e:
            _logger.warning("[WIS] No se pudo disparar el cron de la cola: %s", e)

    @api.model
    def _depurar_pendientes(self):
        """Deja una sola entrada pendiente por variante. Lo llama el cron, que es el
        único que modifica filas pendientes. De paso devuelve a pendiente lo que
        quedó «procesando» de una corrida cortada.

        Queda la que sale primero según el orden de la cola; las demás se cancelan
        con el motivo a la vista. Se cancelan y no se dan por integradas: darlas por
        hechas sin haberlas mandado era justamente cómo aparecían «integradas»
        variantes que WIS nunca recibió.
        """
        colgadas = self.search([
            ('estado', '=', 'procesando'),
            ('write_date', '<', fields.Datetime.now() - PROCESANDO_COLGADA),
        ])
        if colgadas:
            _logger.warning("[WIS] Cola: %d entradas quedaron en proceso de una corrida "
                            "cortada; vuelven a pendiente.", len(colgadas))
            colgadas.write({'estado': 'pendiente'})

        pendientes = self.search([('estado', '=', 'pendiente')])
        if len(pendientes) < 2:
            return
        ahora = fields.Datetime.now()
        maximo = self._max_intentos()
        mejor = {}
        for entrada in pendientes:
            clave = entrada._clave_orden(ahora, maximo)
            actual = mejor.get(entrada.product_id.id)
            if actual is None or clave < actual[0]:
                mejor[entrada.product_id.id] = (clave, entrada)
        conservar = {e.id for _clave, e in mejor.values()}
        for entrada in pendientes.filtered(lambda e: e.id not in conservar):
            entrada.write({
                'estado': 'cancelado',
                'ultimo_error': _("Reemplazada por la entrada %s, que sale antes.")
                % mejor[entrada.product_id.id][1].id,
            })

    def _clave_orden(self, ahora, maximo):
        """Posición en la cola: botón, después con error, después sin enviar."""
        self.ensure_one()
        if self.subestado == 'sin_enviar' and self.prioridad <= PRIORIDAD_BOTON:
            tramo = 0
        elif self.subestado == 'error':
            # Una con error que todavía no toca reintentar va detrás de todo.
            listo = self.intentos < maximo and (
                not self.proximo_intento or self.proximo_intento <= ahora)
            tramo = 1 if listo else 3
        else:
            tramo = 2
        return (tramo, self.prioridad, self.create_date or ahora, self.id)

    @api.model
    def _max_intentos(self):
        """Envíos que puede tener una variante rechazada antes de dejar de salir sola.

        Sin reintentos automáticos es 1: el primer rechazo la deja quieta hasta que
        alguien aprieta «Reintentar», que pone los intentos en cero.
        """
        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not (config and config.reintentar_errores_datos):
            return 1
        return config.cola_max_intentos or MAX_INTENTOS_POR_DEFECTO

    @api.model
    def _siguiente_lote(self, limite):
        """El próximo lote a mandar, de un solo tramo y en orden:

        1. lo pedido con el botón de la ficha, en el orden en que se apretó;
        2. lo que WIS rechazó y ya toca reintentar, de lo más viejo a lo más nuevo;
        3. lo que todavía no se mandó, por prioridad (OC, normal, baja).

        Un lote nunca mezcla tramos: lo del botón sale solo, sin esperar a que
        se reintenten de a uno las que fallan.
        """
        ahora = fields.Datetime.now()
        boton = self.search([
            ('estado', '=', 'pendiente'), ('subestado', '=', 'sin_enviar'),
            ('prioridad', '<=', PRIORIDAD_BOTON),
        ], order='create_date asc, id asc', limit=limite)
        if boton:
            return boton
        con_error = self.search([
            ('estado', '=', 'pendiente'), ('subestado', '=', 'error'),
            ('intentos', '<', self._max_intentos()),
            '|', ('proximo_intento', '=', False), ('proximo_intento', '<=', ahora),
        ], order='create_date asc, id asc', limit=limite)
        if con_error:
            return con_error
        return self.search([
            ('estado', '=', 'pendiente'), ('subestado', '=', 'sin_enviar'),
            ('prioridad', '>', PRIORIDAD_BOTON),
        ], order='prioridad asc, create_date asc, id asc', limit=limite)

    # ------------------------------------------------------------------
    # Procesamiento
    # ------------------------------------------------------------------
    def procesar(self):
        """Procesa estas entradas contra WIS, en un solo lote.

        No lanza. Devuelve 'general' si WIS no atendió el pedido por algo ajeno
        a los productos (credenciales, conexión): las entradas vuelven a quedar
        como estaban, sin sumar intento, y la cola lo prueba en la próxima corrida.
        Los rechazos de una variante quedan Pendientes con error, en rojo.
        """
        entradas = self.filtered(lambda e: e.estado == 'pendiente')
        if not entradas:
            return True

        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not config or not config.comunicacion_activa:
            _logger.info("[WIS] Cola: comunicación deshabilitada, no se procesa nada.")
            return True

        entradas.write({'estado': 'procesando'})
        if not _en_test():
            # Que «procesando» se vea: así el botón no vuelve a mandar lo que ya va
            # en camino, y nadie espera a esta transacción mientras dura el envío.
            self.env.cr.commit()
        variantes = entradas.mapped('product_id')

        try:
            resultado = variantes._enviar_wms_en_lote(motivo='cola en segundo plano')
        except WisErrorGeneral as e:
            _logger.warning("[WIS] Cola: WIS no atendió el lote (%s); se reintenta "
                            "cuando termine la pausa.", e)
            entradas.write({'estado': 'pendiente', 'ultimo_error': str(e),
                            'fecha_procesado': fields.Datetime.now()})
            config._wis_pausar(str(e))
            entradas._avisar_resultado()
            return 'general'
        except Exception as e:
            _logger.exception("[WIS] Cola: fallo el lote completo")
            entradas._registrar_fallo(str(e), config)
            entradas._avisar_resultado()
            return False

        errores = _errores_por_codigo(resultado)
        ahora = fields.Datetime.now()

        for entrada in entradas:
            variante = entrada.product_id
            # 🔴 El `codigo_unico` se persiste RECIÉN cuando WIS acepta la
            # variante, así que la rechazada no lo tiene: el código es
            # determinístico y se recalcula igual que al armar el envío.
            codigo = variante.codigo_unico or config._wis_codigo_producto(variante)
            detalle = errores.get(codigo)
            if detalle or not variante.codigo_unico:
                entrada._registrar_fallo(
                    detalle or 'La variante no obtuvo código WIS.', config)
            else:
                entrada.write({
                    'estado': 'hecho',
                    'fecha_procesado': ahora,
                    'ultimo_error': False,
                    'proximo_intento': False,
                })

        # WIS contestó: si estaba en pausa, se levanta.
        config._wis_levantar_pausa()
        entradas._avisar_resultado()
        return True

    def _registrar_fallo(self, error, config=None):
        """WIS rechazó la variante: queda Pendiente con error y suma un intento.

        Nunca se da por integrada. Si los reintentos automáticos están habilitados,
        la cola la reintenta antes que lo que no se mandó todavía, cada
        `ESPERA_REINTENTO`, hasta agotar los intentos. Si no (por defecto), o agotados,
        sigue Pendiente con error y sale sólo con «Reintentar» o con el botón.
        """
        ahora = fields.Datetime.now()
        config = config or self.env['integracion_wis.integracion_wis']._get_config()
        reintenta = bool(config and config.reintentar_errores_datos)
        for entrada in self:
            entrada.write({
                'estado': 'pendiente',
                'subestado': 'error',
                'intentos': entrada.intentos + 1,
                'ultimo_error': error,
                'fecha_procesado': ahora,
                'proximo_intento': ahora + ESPERA_REINTENTO if reintenta else False,
            })

    def _avisar_resultado(self):
        """Deja en el chatter del producto cómo terminó lo pedido con el botón.

        El botón ya no espera a WIS, así que es ahí donde el usuario se entera.
        """
        boton = self.filtered(lambda e: e.origen == 'boton')
        for plantilla in boton.mapped('product_id.product_tmpl_id'):
            propias = boton.filtered(lambda e: e.product_id.product_tmpl_id == plantilla)
            ok = propias.filtered(lambda e: e.estado == 'hecho')
            con_error = propias.filtered(lambda e: e.subestado == 'error' and e.estado == 'pendiente')
            if ok and not (propias - ok):
                cuerpo = _("Integración con WIS: %s variante(s) integradas.") % len(ok)
            elif con_error:
                cuerpo = _("Integración con WIS: %s integradas, %s rechazadas por WIS "
                           "(quedan Pendientes con error en la Cola de productos). %s") % (
                    len(ok), len(con_error), con_error[0].ultimo_error or '')
            else:
                cuerpo = _("Integración con WIS: WIS no respondió (%s). Los envíos "
                           "quedan en pausa unos minutos y se reintenta solo.") % (propias[0].ultimo_error or '')
            plantilla.message_post(body=cuerpo)

    @api.model
    def _cron_procesar_cola(self):
        """Cron: procesa la cola por tandas, en el orden de `_siguiente_lote`.

        Después de cada tanda vuelve a mirar la cola: lo pedido con el botón a
        mitad de una corrida sale en la tanda siguiente, no al final. Corta por
        tiempo y se vuelve a disparar si queda trabajo.
        """
        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not config or not config.comunicacion_activa:
            _logger.info("[WIS] Cron cola de productos omitido: comunicación deshabilitada.")
            return True

        inicio = time.time()
        tanda = config.chunk_size_productos or 200
        limite = config.cola_productos_por_corrida or 500

        self._depurar_pendientes()
        if not _en_test():
            self.env.cr.commit()
        if config._wis_en_pausa():
            # WIS no respondió hace poco: el próximo ciclo del cron vuelve a mirar.
            _logger.info("[WIS] Cola de productos en espera: envíos a WIS en pausa hasta %s.",
                         config.pausa_hasta)
            return True

        presupuesto = config._presupuesto_corrida(llamadas_por_envio=LLAMADAS_POR_LOTE)
        procesadas, cortada = 0, False
        while procesadas < limite and time.time() - inicio < presupuesto:
            lote = self._siguiente_lote(min(tanda, limite - procesadas))
            if not lote:
                break
            _logger.info("[WIS] Cron cola de productos: lote de %d (prioridad %d, %s)",
                         len(lote), lote[0].prioridad, lote[0].subestado)
            resultado = lote.procesar()
            if not _en_test():
                self.env.cr.commit()
            procesadas += len(lote)
            if resultado == 'general':
                # WIS no atiende: seguir sólo sumaría llamadas que fallan igual.
                cortada = True
                break

        if not cortada and self._siguiente_lote(1):
            self._disparar_cron()

        # Operaciones que esperaban a que estos productos se integraran: que salgan ya.
        if procesadas and 'wis.picking.cola' in self.env and self.env['wis.picking.cola'].search_count(
                [('estado', '=', 'pendiente')], limit=1):
            self.env['wis.picking.cola']._disparar_cron()
        return True

    # ------------------------------------------------------------------
    # Acciones de la vista
    # ------------------------------------------------------------------
    def action_reintentar(self):
        """Vuelve a poner en cola lo que falló o se canceló, con los intentos en cero.

        Las que tenían error siguen marcadas así —en rojo— hasta que WIS las acepte,
        y salen antes que lo que todavía no se mandó.
        """
        entradas = self.filtered(lambda e: e.estado == 'cancelado'
                                 or e.estado == 'pendiente' and e.subestado == 'error')
        entradas.write({
            'estado': 'pendiente',
            'intentos': 0,
            'proximo_intento': False,
        })
        if entradas:
            self._disparar_cron()
        return True

    def action_procesar_ahora(self):
        """Procesa las entradas seleccionadas en el acto (sin esperar al cron)."""
        if not self:
            raise UserError(_("No hay entradas seleccionadas."))
        self.procesar()
        return True

    def action_cancelar(self):
        self.filtered(lambda e: e.estado == 'pendiente').write({
            'estado': 'cancelado',
        })
        return True

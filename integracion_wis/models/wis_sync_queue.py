# -*- coding: utf-8 -*-
"""Cola de integración de productos con WIS.

La integración de un producto con muchas variantes puede demorar varios
minutos. Este modelo permite encolar variantes y procesarlas por lote desde un
cron, para que el usuario no quede bloqueado esperando a WIS.

**Ningún camino automático llama a WIS dentro de su transacción**: el alta de un
producto, tildar la casilla en la ficha, agregar un atributo y la acción masiva
encolan todos acá. El único envío inmediato es el botón «Integrar ahora con
WIS», que es una decisión explícita del usuario. Al encolar se dispara el cron
(`_disparar_cron`), así que el trabajo arranca en segundos y no en el próximo
ciclo de 5 minutos.

No se usa `queue_job` de la OCA porque no está disponible en este addons-path:
esta cola cubre el caso concreto (integración de productos) sin sumar
infraestructura de servidor. Si más adelante entra `queue_job`, se reemplaza el
cron por un job sin tocar los call-sites, que solo conocen `_encolar()`.
"""
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Prioridades: menor número = se procesa antes.
PRIORIDAD_URGENTE = 1   # variantes que traban una Orden de Compra
PRIORIDAD_NORMAL = 10   # integración masiva pedida por el usuario
PRIORIDAD_BAJA = 20     # el resto de las variantes de un template


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
        ('error', 'Error'),
        ('cancelado', 'Cancelado'),
    ], string='Estado', default='pendiente', required=True, index=True)

    prioridad = fields.Integer(
        string='Prioridad',
        default=PRIORIDAD_NORMAL,
        index=True,
        help='Menor número, se procesa antes. 1 = urgente (traba una Orden de '
             'Compra), 10 = normal, 20 = baja (resto de variantes).',
    )

    origen = fields.Selection([
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

        Solo se encolan variantes almacenables. Si una variante ya tiene una
        entrada pendiente se actualiza su prioridad —si la nueva es más
        urgente— en lugar de duplicarla.
        """
        variantes = variantes.filtered(lambda v: v.type == 'product')
        if not variantes:
            return self.browse()

        pendientes = self.search([
            ('product_id', 'in', variantes.ids),
            ('estado', 'in', ('pendiente', 'procesando')),
        ])
        ya_encoladas = pendientes.mapped('product_id')

        # Subir la prioridad de lo que ya estaba encolado más abajo.
        a_priorizar = pendientes.filtered(
            lambda e: e.estado == 'pendiente' and e.prioridad > prioridad
        )
        if a_priorizar:
            a_priorizar.write({'prioridad': prioridad, 'origen': origen,
                               'origen_ref': origen_ref})

        nuevas = variantes - ya_encoladas
        if not nuevas:
            return a_priorizar

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
        return entradas | a_priorizar

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

    # ------------------------------------------------------------------
    # Procesamiento
    # ------------------------------------------------------------------
    def procesar(self):
        """Procesa estas entradas contra WIS, en un solo lote.

        No lanza: los errores quedan registrados en la propia entrada para que
        el cron pueda seguir con el resto.
        """
        entradas = self.filtered(lambda e: e.estado in ('pendiente', 'error'))
        if not entradas:
            return True

        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not config or not config.comunicacion_activa:
            _logger.info("[WIS] Cola: comunicación deshabilitada, no se procesa nada.")
            return True

        entradas.write({'estado': 'procesando'})
        variantes = entradas.mapped('product_id')

        try:
            resultado = variantes._enviar_wms_en_lote(motivo='cola en segundo plano')
        except Exception as e:
            _logger.exception("[WIS] Cola: fallo el lote completo")
            entradas._registrar_fallo(str(e), config)
            return False

        errores_detalle = resultado.get('errores_detalle', [])
        ahora = fields.Datetime.now()

        for entrada in entradas:
            variante = entrada.product_id
            # 🔴 El `codigo_unico` se persiste RECIÉN cuando WIS acepta la
            # variante, así que la rechazada no lo tiene y buscar su error por
            # ese campo no encontraba nada: la entrada quedaba con «La variante
            # no obtuvo código WIS» y tapaba el motivo que WIS había dado, que
            # sí estaba en el detalle. El código es determinístico: se
            # recalcula igual que al armar el envío.
            codigo = variante.codigo_unico or config._wis_codigo_producto(variante)
            detalle = next((e for e in errores_detalle if codigo and codigo in e), None)
            if detalle or not variante.codigo_unico:
                entrada._registrar_fallo(
                    detalle or 'La variante no obtuvo código WIS.', config)
            else:
                entrada.write({
                    'estado': 'hecho',
                    'fecha_procesado': ahora,
                    'ultimo_error': False,
                })

        return True

    def _registrar_fallo(self, error, config=None):
        """Marca las entradas como error y cuenta el intento.

        Mientras no se agoten los reintentos la entrada vuelve a 'pendiente'
        para que el cron la tome de nuevo.
        """
        config = config or self.env['integracion_wis.integracion_wis']._get_config()
        max_intentos = (config.cola_max_intentos if config else 0) or 3
        ahora = fields.Datetime.now()

        for entrada in self:
            intentos = entrada.intentos + 1
            entrada.write({
                'intentos': intentos,
                'ultimo_error': error,
                'fecha_procesado': ahora,
                'estado': 'error' if intentos >= max_intentos else 'pendiente',
            })

    @api.model
    def _cron_procesar_cola(self):
        """Cron: procesa las variantes pendientes por orden de prioridad."""
        config = self.env['integracion_wis.integracion_wis']._get_config()
        if not config or not config.comunicacion_activa:
            _logger.info("[WIS] Cron cola de productos omitido: comunicación deshabilitada.")
            return True

        limite = config.cola_productos_por_corrida or 500
        pendientes = self.search([('estado', '=', 'pendiente')], limit=limite)

        if not pendientes:
            return True

        _logger.info("[WIS] Cron cola de productos: procesando %d entradas (límite %d)",
                     len(pendientes), limite)

        # Se procesa por prioridad para que lo urgente (una OC esperando) salga
        # primero incluso si el lote total es grande.
        for prioridad in sorted(set(pendientes.mapped('prioridad'))):
            grupo = pendientes.filtered(lambda e: e.prioridad == prioridad)
            grupo.procesar()
            self.env.cr.commit()

        return True

    # ------------------------------------------------------------------
    # Acciones de la vista
    # ------------------------------------------------------------------
    def action_reintentar(self):
        """Vuelve a poner en cola entradas en error, reseteando los intentos."""
        self.filtered(lambda e: e.estado in ('error', 'cancelado')).write({
            'estado': 'pendiente',
            'intentos': 0,
            'ultimo_error': False,
        })
        return True

    def action_procesar_ahora(self):
        """Procesa las entradas seleccionadas en el acto (sin esperar al cron)."""
        if not self:
            raise UserError(_("No hay entradas seleccionadas."))
        self.procesar()
        return True

    def action_cancelar(self):
        self.filtered(lambda e: e.estado in ('pendiente', 'error')).write({
            'estado': 'cancelado',
        })
        return True

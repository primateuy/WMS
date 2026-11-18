from odoo import _, fields, api, models
from odoo.exceptions import UserError, ValidationError
import json
import logging
import re
import pytz
import time
from datetime import datetime, timedelta

from .models import WisErrorGeneral
from .wis_notificacion import _en_test, _sin_tiempo

_logger = logging.getLogger(__name__)

def get_uruguay_datetime():
    utc_now = datetime.utcnow()
    uruguay_tz = pytz.timezone('America/Montevideo')
    utc_now = pytz.utc.localize(utc_now)
    uruguay_time = utc_now.astimezone(uruguay_tz)
    return fields.Datetime.to_string(uruguay_time.replace(tzinfo=None))

class ConciliacionLogs(models.Model):
    _name = 'logs.conciliacion'
    _description = 'Logs de Conciliación entre Sistemas'
    _order = 'fecha asc'

    modelo = fields.Char(string='Modelo', required=True)
    fecha = fields.Datetime(string='Fecha', required=True, default=lambda self: get_uruguay_datetime())
    texto = fields.Text(string='Detalle')
    nivel = fields.Selection([
        ('info', 'Información'),
        ('warning', 'Advertencia'),
        ('error', 'Error')
    ], string='Nivel', default='info')
    conciliacion_id = fields.Many2one('conciliacion.maestros', string='Conciliación', ondelete='cascade')

class ConciliacionMaestros(models.Model):
    _name = 'conciliacion.maestros'
    _description = 'Conciliación de Maestros entre Sistemas'

    productos = fields.Boolean(string='Conciliar Productos', default=True)
    clientes = fields.Boolean(string='Conciliar Clientes', default=True)
    codigoBarras = fields.Boolean(string='Conciliar Códigos de Barras', default=True)
    
    logs = fields.One2many('logs.conciliacion', 'conciliacion_id', string='Logs de Conciliación')

    name = fields.Char(string='Nombre', default='Nueva Conciliación', required=True)
    fecha_creacion = fields.Datetime(string='Fecha de Creación', default=fields.Datetime.now, readonly=True)
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('en_proceso', 'En Proceso'),
        ('completado', 'Completado'),
        ('completado_con_errores', 'Completado con Errores'),
        ('error', 'Error Fatal')
    ], string='Estado', default='borrador')

    # Conciliación de productos por tandas (ver `_procesar_productos`).
    prod_pendientes = fields.Text(readonly=True, help="Productos que faltan mandar (JSON con los ids).")
    prod_total = fields.Integer(string='Productos a mandar', readonly=True)
    prod_enviados = fields.Integer(string='Productos enviados', readonly=True)
    prod_errores = fields.Integer(string='Productos con error', readonly=True)
    prod_omitidos = fields.Integer(string='Productos sin cambios', readonly=True)
    prod_fallidos = fields.Text(readonly=True, help="Productos que WIS rechazó en esta conciliación "
                                                    "(JSON: id → motivo).")
    prod_rechazados_previos = fields.Text(
        readonly=True, help="Rechazados de antes que no se modificaron: no se mandan y siguen "
                            "rechazados (JSON: id → motivo).")
    prod_inicio_sync = fields.Datetime(readonly=True)

    # Campo calculado para el nombre de visualización
    display_name = fields.Char(string='Nombre', compute='_compute_display_name', store=True)


    @api.model
    def _cron_conciliar_clientes(self):
        """Método para el cron que ejecuta la conciliación de clientes"""
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            _logger.info("[WIS] Cron conciliación clientes omitido: comunicación deshabilitada.")
            return True
        uruguay_time = datetime.fromisoformat(get_uruguay_datetime())
        conciliacion = self.create({
            'name': f'Conciliación Automática de Clientes - {uruguay_time.strftime("%Y-%m-%d %H:%M")}',
            'clientes': True,
            'productos': False,
            'codigoBarras': False
        })
        conciliacion.conciliarClientes()
        return True

    @api.model
    def _cron_conciliar_productos(self):
        """Conciliación de productos con WIS, por tandas.

        Si quedó una a medias (la corrida anterior no alcanzó), sigue esa; si no, arma una
        nueva. Cada lote se confirma apenas WIS contesta y, si no alcanza el tiempo, el cron
        se vuelve a disparar para seguir donde quedó.

        🔴 Antes mandaba todo en una sola corrida: 163 lotes de 200, 8-9 minutos, y el 18-11-2026
        pasó los 10 minutos del cron. Odoo la cortaba, se perdía todo y volvía a empezar: 14
        arranques sin terminar, reenviando productos a WIS cada vez.
        """
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            _logger.info("[WIS] Cron conciliación productos omitido: comunicación deshabilitada.")
            return True
        Config = self.env['integracion_wis.integracion_wis']
        inicio = time.time()
        presupuesto = Config._presupuesto_corrida(operaciones=True)
        # Un lote que WIS rechace entero se reintenta producto por producto y puede tardar
        # minutos: no se empieza uno nuevo sin margen para eso (con los límites de producción,
        # 440 s de presupuesto, quedan 260 s de envío por corrida).
        limite = inicio + max(presupuesto - self.RESERVA_LOTE, presupuesto / 2)
        conciliacion = self._conciliacion_productos_en_curso()
        if not conciliacion:
            uruguay_time = datetime.fromisoformat(get_uruguay_datetime())
            conciliacion = self.create({
                'name': f'Conciliación Automática de Productos - {uruguay_time.strftime("%Y-%m-%d %H:%M")}',
                'clientes': False,
                'productos': True,
                'codigoBarras': False
            })
            if not conciliacion._preparar_productos():
                return True
            if not _en_test():
                self.env.cr.commit()
        resultado = conciliacion._procesar_productos(limite)
        if resultado == 'general':
            # WIS no atiende: se sigue más tarde, sin cargarlo.
            self._disparar_cron_productos(en=timedelta(minutes=15))
        elif not resultado:
            self._disparar_cron_productos()
        return True

    @api.model
    def _conciliacion_productos_en_curso(self):
        return self.search([('productos', '=', True), ('estado', '=', 'en_proceso'),
                            ('prod_pendientes', '!=', False), ('prod_pendientes', '!=', '[]')],
                           order='id', limit=1)

    @api.model
    def _disparar_cron_productos(self, en=None):
        cron = self.env.ref('integracion_wis.ir_cron_wis_conciliar_productos', raise_if_not_found=False)
        if not cron or not cron.active:
            return False
        try:
            cron.sudo()._trigger(at=fields.Datetime.now() + en if en else None)
        except Exception as e:
            _logger.warning("[WIS] No se pudo disparar el cron de conciliación de productos: %s", e)
        return True

    @api.model
    def _proximo_viernes_0030(self, desde=None):
        """Próximo viernes a las 00:30 de Uruguay, en UTC sin zona (como lo guarda Odoo)."""
        tz = pytz.timezone('America/Montevideo')
        ahora = pytz.utc.localize(desde or fields.Datetime.now()).astimezone(tz)
        dias = (4 - ahora.weekday()) % 7
        candidato = ahora.replace(hour=0, minute=30, second=0, microsecond=0) + timedelta(days=dias)
        if candidato <= ahora:
            candidato += timedelta(days=7)
        return tz.normalize(candidato).astimezone(pytz.utc).replace(tzinfo=None)

    @api.model
    def _cron_conciliar_codigos_barras(self):
        """Método para el cron que ejecuta la conciliación de códigos de barras"""
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            _logger.info("[WIS] Cron conciliación códigos de barras omitido: comunicación deshabilitada.")
            return True
        uruguay_time = datetime.fromisoformat(get_uruguay_datetime())
        conciliacion = self.create({
            'name': f'Conciliación Automática de Códigos de Barras - {uruguay_time.strftime("%Y-%m-%d %H:%M")}',
            'clientes': False,
            'productos': False,
            'codigoBarras': True
        })
        conciliacion.conciliarCodigosBarra()
        return True


    @api.depends('name', 'fecha_creacion', 'estado')
    def _compute_display_name(self):
        uruguay_tz = pytz.timezone('America/Montevideo')
        for record in self:
            if record.fecha_creacion:
                fecha_utc = pytz.utc.localize(record.fecha_creacion)
                fecha_uy = fecha_utc.astimezone(uruguay_tz)
                fecha_str = fecha_uy.strftime('%Y-%m-%d %H:%M')
                record.display_name = f"{record.name} - {fecha_str}"
            else:
                record.display_name = record.name or 'Nueva Conciliación'

    def _agregar_log(self, texto, modelo='', nivel='info'):
        """Helper para agregar logs correctamente"""
        self.env['logs.conciliacion'].create({
            'conciliacion_id': self.id,
            'texto': texto,
            'modelo': modelo,
            'nivel': nivel,
            'fecha': get_uruguay_datetime()
        })

    def _actualizar_estado(self, nuevo_estado):
        """Actualiza el estado de la conciliación"""
        self.estado = nuevo_estado
        self._agregar_log(f"Estado cambiado a: {nuevo_estado}", 'conciliacion.maestros', 'info')

    def _iniciar_conciliacion(self, tipo):
        self._actualizar_estado('en_proceso')
        self._agregar_log(f"Iniciando conciliación de {tipo}...", f'{tipo}', 'info')

    def _completar_conciliacion(self, tipo):
        errores = self.logs.filtered(lambda l: l.nivel == 'error')
        if errores:
            self._actualizar_estado('completado_con_errores')
            self._agregar_log(f"Conciliación de {tipo} completada CON ERRORES ({len(errores)} errores encontrados).", f'{tipo}', 'warning')
        else:
            self._actualizar_estado('completado')
            self._agregar_log(f"Conciliación de {tipo} completada exitosamente.", f'{tipo}', 'info')

    def _error_conciliacion(self, tipo, error_msg):
        self._actualizar_estado('error')
        self._agregar_log(f"Error fatal en conciliación de {tipo}: {error_msg}", f'{tipo}', 'error')

    def _agregar_error_parcial(self, texto, modelo='', excepcion=None):
        """Agrega un error parcial que no detiene la conciliación"""
        error_detalle = f"{texto}"
        if excepcion:
            error_detalle += f" - Detalle: {str(excepcion)}"
        self._agregar_log(error_detalle, modelo, 'error')
        return error_detalle

    def conciliarCodigosBarra(self):
        # Dirección: Odoo → WIS.
        # WIS no tiene endpoint para obtener todos los barcodes de un producto,
        # por lo que no es posible revertir la dirección de esta conciliación.
        try:
            with self.env.cr.savepoint():

                self._iniciar_conciliacion('códigos de barras')

                datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
                if not datosAPI or not datosAPI.apiLink:
                    raise ValidationError("No se encuentran todos los datos para una consulta a la API")

                variantes = self.env['product.product'].search([
                    ('active', '=', True),
                    ('integracion_wms', '=', True),
                    ('codigo_unico', '!=', False),
                ])

                if not variantes:
                    self._agregar_log('No se encontraron productos con integración WMS activa', 'product.product', 'warning')
                    self._actualizar_estado('completado')
                    return True

                procesados = 0
                errores    = 0
                for variante in variantes:
                    try:
                        datosAPI.consultarCodigosBarras(variante, self.id)
                        procesados += 1
                    except Exception as e:
                        errores += 1
                        self._agregar_error_parcial(
                            f'Error procesando códigos de barras de {variante.name}',
                            'product.product', e
                        )

                self._agregar_log(
                    f'Códigos de barras completado. Procesados: {procesados} | Errores: {errores}',
                    'product.barcode',
                    'warning' if errores > 0 else 'info'
                )
                self._completar_conciliacion('códigos de barras')
                return True

        except Exception as e:
            self._error_conciliacion('códigos de barras', str(e))
            raise

    def conciliarMarcados(self):
        if not self.clientes and not self.productos and not self.codigoBarras:
            raise ValidationError("Debe seleccionar al menos una opción para conciliar.")

        try:
            self._agregar_log('Iniciando conciliación múltiple...', 'conciliacion.maestros', 'info')
            
            conciliaciones_exitosas = []
            conciliaciones_fallidas = []

            if self.clientes:
                try:
                    self.conciliarClientes()
                    conciliaciones_exitosas.append('Clientes')
                except Exception as e:
                    conciliaciones_fallidas.append(f'Clientes: {str(e)}')

            if self.productos:
                try:
                    self.conciliarProductos()
                    conciliaciones_exitosas.append('Productos')
                except Exception as e:
                    conciliaciones_fallidas.append(f'Productos: {str(e)}')

            if self.codigoBarras:
                try:
                    self.conciliarCodigosBarra()
                    conciliaciones_exitosas.append('Códigos de Barras')
                except Exception as e:
                    conciliaciones_fallidas.append(f'Códigos de Barras: {str(e)}')

            # Resumen final
            if conciliaciones_exitosas:
                self._agregar_log(f'Conciliaciones exitosas: {", ".join(conciliaciones_exitosas)}', 'conciliacion.maestros', 'info')

            if conciliaciones_fallidas:
                self._agregar_log(f'Conciliaciones fallidas: {"; ".join(conciliaciones_fallidas)}', 'conciliacion.maestros', 'error')

            errores_totales = self.logs.filtered(lambda l: l.nivel == 'error')
            if conciliaciones_fallidas:
                self._actualizar_estado('error')
            elif errores_totales:
                self._actualizar_estado('completado_con_errores')
            else:
                self._actualizar_estado('completado')
                
            return True

        except Exception as e:
            self._error_conciliacion('múltiple', str(e))
            raise

    def conciliarClientes(self):
        try:
            with self.env.cr.savepoint():

                self._iniciar_conciliacion('clientes')

                datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
                if not datosAPI or not datosAPI.apiLink:
                    raise ValidationError("No se encuentran todos los datos para una consulta a la API")

                clientes = self.env['res.partner'].search([
                    ('active', '=', True),
                    ('integracion_wms', '=', True),
                    ('codigo_unico', '!=', False),
                    ('customer_rank', '>', 0),
                ])

                if not clientes:
                    self._agregar_log('No se encontraron clientes con integración WMS activa y código asignado', 'res.partner', 'warning')
                    self._actualizar_estado('completado')
                    return True

                self._agregar_log(f'Enviando {len(clientes)} clientes/proveedores a WIS...', 'res.partner', 'info')

                procesados = 0
                errores = 0
                for partner in clientes:
                    try:
                        partner.enviarWS()
                        procesados += 1
                    except Exception as e:
                        errores += 1
                        self._agregar_error_parcial(
                            f'Error enviando agente {partner.name} a WIS', 'res.partner', e
                        )

                self.env['logs.conciliacion'].create([{
                    'conciliacion_id': self.id,
                    'texto': (
                        f"Conciliación Odoo→WIS (clientes) completada. "
                        f"Enviados a WIS: {procesados} | "
                        f"Errores: {errores}"
                    ),
                    'modelo': 'res.partner',
                    'nivel': 'warning' if errores > 0 else 'info',
                    'fecha': get_uruguay_datetime(),
                }])

                self._completar_conciliacion('clientes')
                return True

        except Exception as e:
            self._error_conciliacion('clientes', str(e))
            raise

    def conciliarProductos(self):
        """Botón «Conciliar Productos»: arma la conciliación y la deja al cron, en segundo plano.

        Antes la hacía entera dentro de la petición: con el maestro completo tardaba más que
        el corte de Odoo y la pantalla se cortaba.
        """
        self.ensure_one()
        en_curso = self._conciliacion_productos_en_curso()
        if en_curso and en_curso != self:
            raise UserError(_("Ya hay una conciliación de productos en curso: %s. Se sigue "
                              "procesando en segundo plano.") % en_curso.display_name)
        if not en_curso:
            if not self._preparar_productos():
                return self._aviso_productos(_("No hay productos para mandar a WIS."), 'info')
        if self._disparar_cron_productos():
            return self._aviso_productos(
                _("%s producto(s) para mandar a WIS: se envían en segundo plano, por lotes.")
                % len(json.loads(self.prod_pendientes or '[]')), 'info')
        return self._aviso_productos(
            _("La conciliación quedó armada, pero el proceso automático «WIS: Conciliación "
              "Productos» está apagado: se envía cuando se prenda."), 'warning')

    def _aviso_productos(self, mensaje, tipo):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _("Conciliación de productos"), 'message': mensaje,
                           'type': tipo, 'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    def _preparar_productos(self):
        """Arma la lista de productos a mandar: los modificados desde la última conciliación
        terminada. False si no hay nada que mandar.

        Un producto que WIS rechazó no se vuelve a mandar hasta que se modifique: el rechazo es
        por el dato (por ejemplo «ProductoEmpresa: El valor está asignado a otro producto») y
        mandar lo mismo no lo arregla. Cuando alguien lo corrige, su fecha de modificación lo
        trae solo. Una caída o un timeout de WIS no cuentan como rechazo: cortan la corrida y se
        sigue después (`_procesar_productos`).
        """
        self.ensure_one()
        datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")
        self._iniciar_conciliacion('productos')
        Producto = self.env['product.product']
        ultima_sync = datosAPI.ultima_sync_productos
        inicio_sync = fields.Datetime.now()
        dominio_base = [('active', '=', True), ('integracion_wms', '=', True),
                        ('codigo_unico', '!=', False)]
        if ultima_sync:
            modificados = Producto.search(dominio_base + [('write_date', '>', ultima_sync)])
        else:
            modificados = Producto.search(dominio_base)
        rechazados = {int(k): v for k, v in json.loads(datosAPI.productos_rechazados_wis or '{}').items()}
        vigentes = Producto.browse(list(rechazados)).exists().filtered_domain(dominio_base)
        sin_corregir = {pid: rechazados[pid] for pid in (vigentes - modificados).ids}
        a_mandar = modificados.sorted('id')
        omitidos = Producto.search_count(dominio_base) - len(a_mandar)
        if not a_mandar:
            datosAPI.write({'ultima_sync_productos': inicio_sync,
                            'productos_rechazados_wis': json.dumps(sin_corregir) if sin_corregir else False})
            self._agregar_log(f'Todos los productos están sincronizados ({omitidos} sin cambios desde '
                              f'{ultima_sync}). Nada que procesar.', 'product.product', 'info')
            self._actualizar_estado('completado')
            return False
        self.write({'prod_pendientes': json.dumps(a_mandar.ids), 'prod_total': len(a_mandar),
                    'prod_enviados': 0, 'prod_errores': 0, 'prod_omitidos': omitidos,
                    'prod_fallidos': '{}', 'prod_inicio_sync': inicio_sync,
                    'prod_rechazados_previos': json.dumps(sin_corregir)})
        self._agregar_log(
            f'Sincronización incremental: {len(modificados)} modificados '
            f'| {omitidos} sin cambios (omitidos) '
            f'| {len(sin_corregir)} rechazados antes y sin corregir (no se mandan) '
            f'| referencia: {ultima_sync or "primera ejecución"}. Se envían por lotes.',
            'product.product', 'info')
        return True

    def _procesar_productos(self, limite):
        """Manda los productos pendientes de a un lote, confirmando cada uno.

        True si terminó, False si se acabó el tiempo (sigue la corrida siguiente) y 'general'
        si WIS no atiende. `limite` ya descuenta el margen para un lote lento
        (`RESERVA_LOTE`, ver `_cron_conciliar_productos`).
        """
        self.ensure_one()
        datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
        tamano = datosAPI.chunk_size_productos or 200
        Producto = self.env['product.product']
        pendientes = json.loads(self.prod_pendientes or '[]')
        fallidos = json.loads(self.prod_fallidos or '{}')
        primero = True
        while pendientes:
            # Al menos un lote por corrida: con un presupuesto chico la reserva no puede dejar
            # la conciliación sin avanzar nunca.
            if not primero and _sin_tiempo(limite):
                return False
            primero = False
            lote_ids = pendientes[:tamano]
            variantes = Producto.browse(lote_ids).exists()
            try:
                resultado = datosAPI.insertarProductosMasivo(
                    variantes, chunk_size=tamano, enviar_barcodes=False) if variantes else {
                        'enviados': 0, 'errores': 0, 'errores_por_codigo': {}}
            except WisErrorGeneral as e:
                if not _en_test():
                    self.env.cr.rollback()
                    self.env.invalidate_all()
                _logger.warning("[WIS] Conciliación de productos en espera: WIS no atiende (%s).", e)
                self._agregar_log(f'WIS no atiende ({e}). Se sigue más tarde con los '
                                  f'{len(pendientes)} productos que faltan.', 'product.product', 'warning')
                if not _en_test():
                    self.env.cr.commit()
                return 'general'
            motivos = resultado.get('errores_por_codigo', {})
            con_error = variantes.filtered(lambda v: v.codigo_unico in motivos)
            for v in con_error:
                fallidos[str(v.id)] = self._motivo_corto(motivos[v.codigo_unico])
            pendientes = pendientes[len(lote_ids):]
            self.write({'prod_pendientes': json.dumps(pendientes),
                        'prod_enviados': self.prod_enviados + resultado['enviados'],
                        'prod_errores': self.prod_errores + len(con_error),
                        'prod_fallidos': json.dumps(fallidos)})
            if not _en_test():
                self.env.cr.commit()
        self._terminar_productos(datosAPI, fallidos)
        return True

    #: Segundos que se dejan para un lote que WIS rechace entero y haya que reintentar de a uno.
    RESERVA_LOTE = 180

    @staticmethod
    def _motivo_corto(detalle):
        """El mensaje de WIS sin el envoltorio («<código>: Error en la API: 400 - {...}»)."""
        texto = str(detalle or '')
        encontrado = re.search(r'Messages\\?"\s*:\s*\[\\?"([^"\\]+)', texto)
        return (encontrado.group(1) if encontrado else texto)[:200]

    def _terminar_productos(self, datosAPI, fallidos):
        # La fecha se guarda aunque haya rechazos: antes un solo producto rechazado la dejaba
        # sin guardar y cada semana se reenviaba el maestro entero. Los rechazados quedan en la
        # configuración con su motivo: los de antes que no se tocaron, más los de ahora.
        rechazados = json.loads(self.prod_rechazados_previos or '{}')
        rechazados.update(fallidos)
        datosAPI.write({'ultima_sync_productos': self.prod_inicio_sync,
                        'productos_rechazados_wis': json.dumps(rechazados, ensure_ascii=False)
                        if rechazados else False})
        Producto = self.env['product.product']
        detalle = ''
        if fallidos:
            ids = [int(k) for k in list(fallidos)[:20]]
            codigos = Producto.browse(ids).mapped('codigo_unico')
            motivos = sorted(set(fallidos.values()))
            detalle = (f" Rechazados: {', '.join(codigos)}{' y otros' if len(fallidos) > 20 else ''}. "
                       f"Motivo: {' / '.join(motivos[:3])}. No se vuelven a mandar hasta que se "
                       f"modifiquen.")
            # Nivel error: así la conciliación queda «completada con errores» y no «completada»,
            # que es como figuraban las de octubre y noviembre con 12 rechazos cada una.
            self._agregar_log(f"WIS rechazó {len(fallidos)} producto(s).{detalle}",
                              'product.product', 'error')
        self._agregar_log(
            f"Conciliación Odoo→WIS completada. Enviados a WIS: {self.prod_enviados} | "
            f"Omitidos (sin modificar): {self.prod_omitidos} | Rechazados: {self.prod_errores} | "
            f"Rechazados de antes sin corregir: {len(rechazados) - len(fallidos)}.",
            'product.product', 'warning' if fallidos else 'info')
        self._completar_conciliacion('productos')
        if not _en_test():
            self.env.cr.commit()
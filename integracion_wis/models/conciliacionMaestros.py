from odoo import fields, api, models
from odoo.exceptions import ValidationError
import logging
import pytz
from datetime import datetime

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
        """Método para el cron que ejecuta la conciliación de productos"""
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            _logger.info("[WIS] Cron conciliación productos omitido: comunicación deshabilitada.")
            return True
        uruguay_time = datetime.fromisoformat(get_uruguay_datetime())
        conciliacion = self.create({
            'name': f'Conciliación Automática de Productos - {uruguay_time.strftime("%Y-%m-%d %H:%M")}',
            'clientes': False,
            'productos': True,
            'codigoBarras': False
        })
        conciliacion.conciliarProductos()
        return True

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
        try:
            with self.env.cr.savepoint():

                self._iniciar_conciliacion('productos')

                datosAPI = self.env['integracion_wis.integracion_wis']._get_config()
                if not datosAPI or not datosAPI.apiLink:
                    raise ValidationError("No se encuentran todos los datos para una consulta a la API")

                ultima_sync  = datosAPI.ultima_sync_productos
                inicio_sync  = fields.Datetime.now()

                dominio_base = [
                    ('active', '=', True),
                    ('integracion_wms', '=', True),
                    ('codigo_unico', '!=', False),
                ]

                if ultima_sync:
                    variantes_a_sincronizar = self.env['product.product'].search(
                        dominio_base + [('write_date', '>', ultima_sync)]
                    )
                    todas   = self.env['product.product'].search_count(dominio_base)
                    omitidos = todas - len(variantes_a_sincronizar)
                else:
                    variantes_a_sincronizar = self.env['product.product'].search(dominio_base)
                    omitidos = 0

                if not variantes_a_sincronizar:
                    self._agregar_log(
                        f'Todos los productos están sincronizados ({omitidos} sin cambios desde '
                        f'{ultima_sync}). Nada que procesar.',
                        'product.product', 'info'
                    )
                    self._actualizar_estado('completado')
                    return True

                self._agregar_log(
                    f'Sincronización incremental: {len(variantes_a_sincronizar)} modificados '
                    f'| {omitidos} sin cambios (omitidos) '
                    f'| referencia: {ultima_sync or "primera ejecución"}.',
                    'product.product', 'info'
                )

                # Los códigos de barras tienen su propia conciliación
                # (`conciliarCodigosBarra`): no se reenvían acá.
                resultado = datosAPI.insertarProductosMasivo(
                    variantes_a_sincronizar, enviar_barcodes=False)

                if resultado['errores'] == 0:
                    datosAPI.write({'ultima_sync_productos': inicio_sync})
                else:
                    self._agregar_log(
                        f'ultima_sync_productos NO actualizada porque hubo {resultado["errores"]} errores. '
                        f'Los productos fallidos serán reintentados en la próxima ejecución.',
                        'product.product', 'warning'
                    )

                self.env['logs.conciliacion'].create([{
                    'conciliacion_id': self.id,
                    'texto': (
                        f"Conciliación Odoo→WIS completada. "
                        f"Enviados a WIS: {resultado['enviados']} | "
                        f"Omitidos (sin modificar): {omitidos} | "
                        f"Errores: {resultado['errores']}"
                    ),
                    'modelo': 'product.product',
                    'nivel': 'warning' if resultado['errores'] > 0 else 'info',
                    'fecha': get_uruguay_datetime(),
                }])

                self._completar_conciliacion('productos')
                return True

        except Exception as e:
            # El savepoint fue revertido, la transacción principal sigue válida.
            self._error_conciliacion('productos', str(e))
            raise
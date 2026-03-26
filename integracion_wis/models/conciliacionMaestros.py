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
        try:
            self._iniciar_conciliacion('códigos de barras')

            productos = self.env['product.product'].search([
                ('active', '=', True),
                ('integracion_wms', '=', True),
                ('codigo_unico', '!=', False)
            ])

            if not productos:
                self._agregar_log('No se encontraron productos con integración WMS activa', 'product.product', 'warning')
                self._actualizar_estado('completado')
                return True

            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
            if not datosAPI or not datosAPI.apiLink:
                error_msg = "No se encuentran todos los datos para una consulta a la API"
                self._error_conciliacion('códigos de barras', error_msg)
                raise ValidationError(error_msg)

            productos_procesados = 0
            productos_con_error = 0
            for producto in productos:
                variantes = producto.product_variant_ids.filtered(lambda v: v.active and v.codigo_unico)

                if variantes:
                    try:
                        datosAPI.consultarCodigosBarras(variantes, self.id)
                        productos_procesados += len(variantes)
                        self._agregar_log(f'Procesadas {len(variantes)} variantes del producto {producto.name}', 'product.product', 'info')
                    except Exception as e:
                        productos_con_error += len(variantes)
                        self._agregar_error_parcial(f'Error procesando variantes del producto {producto.name}', 'product.product', e)

            if productos_con_error > 0:
                self._agregar_log(f'📊 Resumen: {productos_procesados} variantes procesadas exitosamente, {productos_con_error} con errores', 'product.barcode', 'warning')
            else:
                self._agregar_log(f'Total de variantes procesadas exitosamente: {productos_procesados}', 'product.barcode', 'info')
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
            self._iniciar_conciliacion('clientes')

            clientes = self.env['res.partner'].search([
                ('active', '=', True),
                ('integracion_wms', '=', True),
                ('customer_rank', '>', 0)
            ])

            if not clientes:
                self._agregar_log('No se encontraron clientes con integración WMS activa', 'res.partner', 'warning')
                self._actualizar_estado('completado')
                return True

            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
            if not datosAPI or not datosAPI.apiLink:
                error_msg = "No se encuentran todos los datos para una consulta a la API"
                self._error_conciliacion('clientes', error_msg)
                raise ValidationError(error_msg)

            self._agregar_log(f'Procesando {len(clientes)} clientes...', 'res.partner', 'info')
            try:
                datosAPI.conciliarClientes(clientes, self.id)
                self._agregar_log(f'Procesados {len(clientes)} clientes exitosamente', 'res.partner', 'info')
            except Exception as e:
                self._agregar_error_parcial(f'Error procesando clientes', 'res.partner', e)

            self._completar_conciliacion('clientes')
            return True

        except Exception as e:
            self._error_conciliacion('clientes', str(e))
            raise

    def conciliarProductos(self):
        try:
            self._iniciar_conciliacion('productos')

            productos = self.env['product.product'].search([
                ('active', '=', True),
                ('integracion_wms', '=', True),
                ('codigo_unico', '!=', False)
            ])

            if not productos:
                self._agregar_log('No se encontraron productos con integración WMS activa', 'product.product', 'warning')
                self._actualizar_estado('completado')
                return True

            datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
            if not datosAPI or not datosAPI.apiLink:
                error_msg = "No se encuentran todos los datos para una consulta a la API"
                self._error_conciliacion('productos', error_msg)
                raise ValidationError(error_msg)

            productos_procesados = 0
            productos_con_error = 0
            for producto in productos:
                variantes = producto.product_variant_ids.filtered(lambda v: v.active and v.codigo_unico)

                if variantes:
                    try:
                        datosAPI.consultarProductos(variantes, self.id)
                        productos_procesados += len(variantes)
                        self._agregar_log(f'Procesadas {len(variantes)} variantes del producto {producto.name}', 'product.product', 'info')
                    except Exception as e:
                        productos_con_error += len(variantes)
                        self._agregar_error_parcial(f'Error procesando variantes del producto {producto.name}', 'product.product', e)

            if productos_con_error > 0:
                self._agregar_log(f'Resumen: {productos_procesados} variantes procesadas exitosamente, {productos_con_error} con errores', 'product.product', 'warning')
            else:
                self._agregar_log(f'Total de variantes procesadas exitosamente: {productos_procesados}', 'product.product', 'info')
            self._completar_conciliacion('productos')
            return True

        except Exception as e:
            self._error_conciliacion('productos', str(e))
            raise
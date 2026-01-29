# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class WizardEjecutarConciliacion(models.TransientModel):
    _name = 'wizard.ejecutar.conciliacion'
    _description = 'Wizard para Ejecutar Conciliación Manual'

    tipo_conciliacion = fields.Selection([
        ('completa', 'Conciliación Completa'),
        ('productos', 'Solo Productos'),
        ('clientes', 'Solo Clientes'),
        ('codigos_barras', 'Solo Códigos de Barras'),
    ], string='Tipo de Conciliación', default='completa', required=True)

    # Configuraciones de Productos
    sincronizar_productos = fields.Boolean('Sincronizar Productos', default=True)
    incluir_productos_inactivos = fields.Boolean('Incluir Productos Inactivos', default=False)
    incluir_variantes = fields.Boolean('Incluir Variantes de Producto', default=True)

    # Configuraciones de Clientes  
    sincronizar_clientes = fields.Boolean('Sincronizar Clientes', default=True)
    incluir_clientes_inactivos = fields.Boolean('Incluir Clientes Inactivos', default=False)

    # Configuraciones de Códigos de Barras
    sincronizar_codigos_barras = fields.Boolean('Sincronizar Códigos de Barras', default=True)
    crear_codigos_faltantes = fields.Boolean('Crear Códigos Faltantes', default=False)

    # Configuraciones de Notificación
    notificar_completado = fields.Boolean('Notificar al Completar', default=True)
    notificar_discrepancias = fields.Boolean('Notificar Discrepancias', default=True)

    # Configuración Avanzada
    ignorar_errores_menores = fields.Boolean('Ignorar Errores Menores', default=False)
    crear_log_detallado = fields.Boolean('Crear Log Detallado', default=True)

    @api.onchange('tipo_conciliacion')
    def _onchange_tipo_conciliacion(self):
        """Actualiza las configuraciones según el tipo de conciliación."""
        if self.tipo_conciliacion == 'productos':
            self.sincronizar_productos = True
            self.sincronizar_clientes = False
            self.sincronizar_codigos_barras = False
        elif self.tipo_conciliacion == 'clientes':
            self.sincronizar_productos = False
            self.sincronizar_clientes = True
            self.sincronizar_codigos_barras = False
        elif self.tipo_conciliacion == 'codigos_barras':
            self.sincronizar_productos = False
            self.sincronizar_clientes = False
            self.sincronizar_codigos_barras = True
        else:  # completa
            self.sincronizar_productos = True
            self.sincronizar_clientes = True
            self.sincronizar_codigos_barras = True

    def ejecutar_conciliacion(self):
        """Ejecuta la conciliación con las configuraciones especificadas."""
        self.ensure_one()

        # Validaciones
        if not any([self.sincronizar_productos, self.sincronizar_clientes, self.sincronizar_codigos_barras]):
            raise ValidationError(_("Debe seleccionar al menos un tipo de sincronización."))

        try:
            # Crear el registro de conciliación
            conciliacion = self.env['conciliacion.maestros'].create({
                'tipo_conciliacion': self.tipo_conciliacion,
                'proceso_automatico': False,
                'ejecutado_por': self.env.user.id,
            })

            # Crear configuración temporal para esta ejecución
            config_temp = self.env['configuracion.conciliacion'].create({
                'name': f'Conciliación Manual - {fields.Datetime.now()}',
                'activo': False,
                'tipo_conciliacion': self.tipo_conciliacion,
                'frecuencia': 'manual',
                'sincronizar_productos': self.sincronizar_productos,
                'incluir_productos_inactivos': self.incluir_productos_inactivos,
                'incluir_variantes': self.incluir_variantes,
                'sincronizar_clientes': self.sincronizar_clientes,
                'incluir_clientes_inactivos': self.incluir_clientes_inactivos,
                'sincronizar_codigos_barras': self.sincronizar_codigos_barras,
                'crear_codigos_faltantes': self.crear_codigos_faltantes,
                'notificar_completado': self.notificar_completado,
                'notificar_discrepancias': self.notificar_discrepancias,
                'ignorar_errores_menores': self.ignorar_errores_menores,
                'crear_log_detallado': self.crear_log_detallado,
            })

            # Asociar la configuración con la conciliación
            conciliacion.configuracion_id = config_temp.id

            # Ejecutar la conciliación
            resultado = conciliacion.ejecutar_conciliacion()

            # Eliminar la configuración temporal
            config_temp.unlink()

            # Mostrar resultado
            if resultado:
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'conciliacion.maestros',
                    'res_id': conciliacion.id,
                    'view_mode': 'form',
                    'target': 'current',
                    'context': {'form_view_initial_mode': 'readonly'},
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Error en Conciliación'),
                        'message': _('La conciliación no se pudo completar. Revise los logs para más detalles.'),
                        'type': 'danger',
                        'sticky': False,
                    }
                }

        except Exception as e:
            _logger.error(f"Error al ejecutar conciliación manual: {str(e)}")
            raise ValidationError(_("Error al ejecutar la conciliación: %s") % str(e))

    def cancelar(self):
        """Cancela el wizard."""
        return {'type': 'ir.actions.act_window_close'}
# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Configuración de activación del módulo
    wis_integration_enabled = fields.Boolean(
        string="Activar Integración WIS",
        config_parameter='integracion_wis.integration_enabled',
        help="Activar o desactivar la integración automática con WIS"
    )

    # Configuración de API
    wis_client_id = fields.Char(
        string="Cliente ID",
        config_parameter='integracion_wis.client_id',
        help="ID del cliente para la autenticación con la API de WIS"
    )

    wis_client_secret = fields.Char(
        string="Client Secret",
        config_parameter='integracion_wis.client_secret',
        help="Secreto del cliente para la autenticación con la API de WIS"
    )

    wis_url_access_token = fields.Char(
        string="URL Access Token",
        config_parameter='integracion_wis.url_access_token',
        help="URL para obtener el token de acceso a la API de WIS"
    )

    wis_api_link = fields.Char(
        string="Enlace de la API",
        config_parameter='integracion_wis.api_link',
        help="URL base de la API de WIS para realizar las solicitudes"
    )

    # Configuración de sincronización automática
    wis_auto_sync_products = fields.Boolean(
        string="Sincronizar Productos Automáticamente",
        config_parameter='integracion_wis.auto_sync_products',
        default=True,
        help="Sincronizar automáticamente los productos con WIS al crearlos o modificarlos"
    )

    wis_auto_sync_partners = fields.Boolean(
        string="Sincronizar Clientes/Proveedores Automáticamente",
        config_parameter='integracion_wis.auto_sync_partners',
        default=True,
        help="Sincronizar automáticamente los clientes y proveedores con WIS al crearlos o modificarlos"
    )

    wis_auto_sync_orders = fields.Boolean(
        string="Sincronizar Pedidos Automáticamente",
        config_parameter='integracion_wis.auto_sync_orders',
        default=True,
        help="Sincronizar automáticamente los pedidos con WIS al confirmarlos"
    )

    wis_auto_sync_pickings = fields.Boolean(
        string="Sincronizar Albaranes Automáticamente",
        config_parameter='integracion_wis.auto_sync_pickings',
        default=True,
        help="Sincronizar automáticamente los albaranes con WIS"
    )

    # Configuración de WebHook
    wis_webhook_enabled = fields.Boolean(
        string="Activar WebHooks",
        config_parameter='integracion_wis.webhook_enabled',
        help="Activar o desactivar la recepción de webhooks desde WIS"
    )

    wis_webhook_secret_key = fields.Char(
        string="Clave Secreta WebHook",
        config_parameter='integracion_wis.webhook_secret_key',
        help="Clave secreta para autenticar las solicitudes del webhook"
    )

    # Configuración de empresa por defecto
    wis_empresa_id = fields.Char(
        string="ID de Empresa en WIS",
        config_parameter='integracion_wis.empresa_id',
        default="6005",
        help="ID de la empresa en el sistema WIS"
    )

    # Configuración de logs
    wis_enable_debug_logs = fields.Boolean(
        string="Activar Logs de Debug",
        config_parameter='integracion_wis.enable_debug_logs',
        help="Activar logs detallados para debug de la integración"
    )

    # Configuración de timeouts
    wis_api_timeout = fields.Integer(
        string="Timeout de API (segundos)",
        config_parameter='integracion_wis.api_timeout',
        default=30,
        help="Tiempo límite para las llamadas a la API de WIS"
    )

    # Configuración de reintentos
    wis_max_retries = fields.Integer(
        string="Número Máximo de Reintentos",
        config_parameter='integracion_wis.max_retries',
        default=3,
        help="Número máximo de reintentos para las llamadas a la API en caso de fallo"
    )

    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        # Obtener configuración de la base de datos actual si existe
        wis_config = self.env['integracion_wis.integracion_wis'].search([
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        
        if wis_config:
            res.update({
                'wis_client_id': wis_config.client_id,
                'wis_client_secret': wis_config.client_secret,
                'wis_url_access_token': wis_config.url_access_token,
                'wis_api_link': wis_config.apiLink,
            })
        
        # Obtener configuración de webhook si existe
        webhook_config = self.env['integracion_wis.integracion_wis_webhook'].search([
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        
        if webhook_config:
            res.update({
                'wis_webhook_secret_key': webhook_config.claveSecreta,
                'wis_webhook_enabled': True,
            })
        
        return res

    def set_values(self):
        super(ResConfigSettings, self).set_values()
        # Sincronizar con el modelo de configuración existente
        if self.wis_integration_enabled:
            wis_config = self.env['integracion_wis.integracion_wis'].search([
                ('company_id', '=', self.env.company.id)
            ], limit=1)
            
            if not wis_config:
                wis_config = self.env['integracion_wis.integracion_wis'].create({
                    'company_id': self.env.company.id,
                    'client_id': self.wis_client_id or '',
                    'client_secret': self.wis_client_secret or '',
                    'url_access_token': self.wis_url_access_token or '',
                    'apiLink': self.wis_api_link or '',
                })
            else:
                wis_config.write({
                    'client_id': self.wis_client_id or '',
                    'client_secret': self.wis_client_secret or '',
                    'url_access_token': self.wis_url_access_token or '',
                    'apiLink': self.wis_api_link or '',
                })

        # Manejar configuración de webhooks
        if self.wis_webhook_enabled and self.wis_webhook_secret_key:
            webhook_config = self.env['integracion_wis.integracion_wis_webhook'].search([
                ('company_id', '=', self.env.company.id)
            ], limit=1)
            
            if not webhook_config:
                self.env['integracion_wis.integracion_wis_webhook'].create({
                    'company_id': self.env.company.id,
                    'claveSecreta': self.wis_webhook_secret_key,
                })
            else:
                webhook_config.write({
                    'claveSecreta': self.wis_webhook_secret_key,
                })

    def action_test_wis_connection(self):
        """Método para probar la conexión con WIS"""
        try:
            wis_config = self.env['integracion_wis.integracion_wis'].search([
                ('company_id', '=', self.env.company.id)
            ], limit=1)
            
            if not wis_config:
                raise UserError(_("No se encontró configuración de WIS para esta compañía"))
            
            # Intentar renovar token para probar la conexión
            wis_config.renovarToken()
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Conexión Exitosa'),
                    'message': _('La conexión con WIS se ha establecido correctamente'),
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error de Conexión'),
                    'message': _('Error al conectar con WIS: %s') % str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def action_open_wis_config(self):
        """Abrir la configuración avanzada de WIS"""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Configuración Avanzada WIS'),
            'res_model': 'integracion_wis.integracion_wis',
            'view_mode': 'tree,form',
            'domain': [('company_id', '=', self.env.company.id)],
            'context': {'default_company_id': self.env.company.id},
        }

    def action_open_wis_webhooks(self):
        """Abrir la configuración de webhooks"""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Configuración de WebHooks WIS'),
            'res_model': 'integracion_wis.integracion_wis_webhook',
            'view_mode': 'tree,form',
            'domain': [('company_id', '=', self.env.company.id)],
            'context': {'default_company_id': self.env.company.id},
        }
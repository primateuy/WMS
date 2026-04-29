from odoo import models, fields, api
import logging
import json
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

class ResPartnerLogs(models.Model):
    _name = 'logs.res.partner'
    _description = 'Logs de Integración de Agentes con WIS'
    _order = 'fecha desc'

    partner_id = fields.Many2one('res.partner', string='Agente', ondelete='cascade', required=True)
    fecha = fields.Datetime(string='Fecha', required=True, default=fields.Datetime.now)
    texto = fields.Text(string='Detalle')

    operacion = fields.Selection([
        ('envio_cli', 'Envío CLI'),
        ('envio_pro', 'Envío PRO'),
        ('sincronizacion', 'Sincronización'),
        ('otro', 'Otro'),
    ], string='Operación', default='otro')

    resultado = fields.Selection([
        ('exito', 'Éxito'),
        ('error', 'Error'),
    ], string='Resultado', default='exito')

    payload_enviado = fields.Text(string='Payload enviado a WIS')
    response_data = fields.Text(string='Respuesta de WIS')

class ResPartner(models.Model):
    _inherit = 'res.partner'

    integracion_wms = fields.Boolean(
        string='Integración con WIS',
        default=False,
    )

    codigo_wms = fields.Char(
        string='Código WMS',
        default='',
    )

    codigo_unico = fields.Char(
        string="Código unico identificatorio"
    )

    codigo_unico_cliente = fields.Char(
        string="Identificación WIS Cliente",
        readonly=True,
        help="Código de agente CLI en WIS (se usa en operaciones de venta/despacho)",
    )

    codigo_unico_proveedor = fields.Char(
        string="Identificación WIS Proveedor",
        readonly=True,
        help="Código de agente PRO en WIS (se usa en operaciones de compra/recepción/devolución de sucursal)",
    )


    logs = fields.One2many('logs.res.partner', 'partner_id', string='Logs de Integración')


    def unlink(self):
        for partner in self:
            if partner.codigo_wms and partner.codigo_unico:
                raise ValidationError("No se puede eliminar un agente que ya está sincronizado con WMS (tiene código WMS y código único).")
        return super(ResPartner, self).unlink()


    CAMPOS_WIS = {'name', 'street', 'street2', 'city', 'zip', 'state_id', 'country_id', 'phone', 'integracion_wms'}

    @api.model_create_multi
    def create(self, vals_list):
        records = super(ResPartner, self).create(vals_list)
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            return records
        for partner in records:
            if partner.integracion_wms:
                partner.enviarWS()
        return records

    def write(self, vals):
        res = super(ResPartner, self).write(vals)

        if (not self.env.context.get('skip_wis_sync') and
                self.env['integracion_wis.integracion_wis']._comunicacion_habilitada()):
            hay_cambios_relevantes = bool(self.CAMPOS_WIS & set(vals.keys()))
            if hay_cambios_relevantes:
                for partner in self:
                    if partner.integracion_wms:
                        partner.enviarWS()

        return res

    def _aplicar_sync_desde_wis(self, wis, tipo):
        """Compara datos de WIS con Odoo y aplica los cambios en Odoo si difieren.
        Retorna texto descriptivo de los cambios aplicados (o None si no hubo cambios).
        """
        cambios = {}
        detalle = []

        desc_wis = (wis.get('descripcion') or '').strip()
        if desc_wis and desc_wis != (self.name or '').strip():
            cambios['name'] = desc_wis
            detalle.append(f"name: '{self.name}' → '{desc_wis}'")

        tel_wis = (wis.get('telefonoPrincipal') or '').strip()
        if tel_wis and tel_wis != (self.phone or '').strip():
            cambios['phone'] = tel_wis
            detalle.append(f"phone: '{self.phone}' → '{tel_wis}'")

        dir_wis = (wis.get('direccion') or '').strip()
        if dir_wis and dir_wis != (self.street or '').strip():
            cambios['street'] = dir_wis
            detalle.append(f"street: '{self.street}' → '{dir_wis}'")

        if cambios:
            self.with_context(skip_wis_sync=True).write(cambios)
            resumen = f"[{tipo}] Sincronizado desde WIS: {', '.join(detalle)}"
            self.message_post(body=resumen)
            return resumen
        return None

    def enviarWS(self):
        if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
            raise ValidationError("La comunicación con WIS está deshabilitada. Actívela en la configuración de WIS antes de sincronizar.")

        if self.customer_rank == 0 and self.supplier_rank == 0:
            raise ValidationError("El agente no es ni cliente ni proveedor, no se puede integrar con WIS")

        if self.integracion_wms == False:
            raise ValidationError("El agente no está marcado para integración con WIS")

        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        update_vals = {}

        phone_clean = (self.phone or '').replace(' ', '').replace('-', '')
        country_code = self.country_id.code.upper() if self.country_id and self.country_id.code else 'UY'

        punto_entrega_parts = []
        if self.street:
            punto_entrega_parts.append(f"Calle: {self.street}")
        if self.street2:
            punto_entrega_parts.append(f"Esquina: {self.street2}")
        if self.city:
            ciudad = self.city + (f" (CP: {self.zip})" if self.zip else '')
            punto_entrega_parts.append(ciudad)
        elif self.zip:
            punto_entrega_parts.append(f"CP: {self.zip}")
        if self.state_id and self.state_id.name:
            punto_entrega_parts.append(f"Provincia: {self.state_id.name}")
        punto_entrega = ' - '.join(punto_entrega_parts) if punto_entrega_parts else 'Sin dirección'

        # --- Rol CLI ---
        if self.customer_rank > 0:
            codigo_cli_enviado = self.codigo_unico_cliente or f"CLI-pendiente"
            payload_cli = json.dumps({
                'codigoAgente': codigo_cli_enviado,
                'tipo': 'CLI',
                'descripcion': (self.name or '').upper(),
                'telefonoPrincipal': phone_clean or '00000000',
                'direccion': punto_entrega[:120],
                'pais': country_code,
                'localidad': (self.city or '').upper(),
            }, ensure_ascii=False, indent=2)
            try:
                response_cli = datosAPI.insertarClienteOrSupplier(self, 'CLI')
                _logger.info("RESPONSE CLI => %s", response_cli)
                if response_cli and isinstance(response_cli, dict):
                    codigo_cli = response_cli.get('codigoUnico', '')
                    if codigo_cli and not self.codigo_unico_cliente:
                        update_vals['codigo_unico_cliente'] = codigo_cli
                    self.env['logs.res.partner'].create({
                        'partner_id': self.id,
                        'fecha': fields.Datetime.now(),
                        'operacion': 'envio_cli',
                        'resultado': 'exito',
                        'texto': f"[CLI] Enviado a WIS. Código: '{self.codigo_unico_cliente or codigo_cli}'",
                        'payload_enviado': payload_cli,
                        'response_data': json.dumps(response_cli, ensure_ascii=False, indent=2),
                    })
            except Exception as e:
                self.env['logs.res.partner'].create({
                    'partner_id': self.id,
                    'fecha': fields.Datetime.now(),
                    'operacion': 'envio_cli',
                    'resultado': 'error',
                    'texto': f"[CLI] Error al enviar a WIS: {str(e)}",
                    'payload_enviado': payload_cli,
                })
                raise

        # --- Rol PRO ---
        if self.supplier_rank > 0:
            codigo_pro_enviado = self.codigo_unico_proveedor or f"PRO-pendiente"
            payload_pro = json.dumps({
                'codigoAgente': codigo_pro_enviado,
                'tipo': 'PRO',
                'descripcion': (self.name or '').upper(),
                'telefonoPrincipal': phone_clean or '00000000',
                'direccion': punto_entrega[:120],
                'pais': country_code,
                'localidad': (self.city or '').upper(),
            }, ensure_ascii=False, indent=2)
            try:
                response_pro = datosAPI.insertarClienteOrSupplier(self, 'PRO')
                _logger.info("RESPONSE PRO => %s", response_pro)
                if response_pro and isinstance(response_pro, dict):
                    codigo_pro = response_pro.get('codigoUnico', '')
                    if codigo_pro and not self.codigo_unico_proveedor:
                        update_vals['codigo_unico_proveedor'] = codigo_pro
                    self.env['logs.res.partner'].create({
                        'partner_id': self.id,
                        'fecha': fields.Datetime.now(),
                        'operacion': 'envio_pro',
                        'resultado': 'exito',
                        'texto': f"[PRO] Enviado a WIS. Código: '{self.codigo_unico_proveedor or codigo_pro}'",
                        'payload_enviado': payload_pro,
                        'response_data': json.dumps(response_pro, ensure_ascii=False, indent=2),
                    })
            except Exception as e:
                self.env['logs.res.partner'].create({
                    'partner_id': self.id,
                    'fecha': fields.Datetime.now(),
                    'operacion': 'envio_pro',
                    'resultado': 'error',
                    'texto': f"[PRO] Error al enviar a WIS: {str(e)}",
                    'payload_enviado': payload_pro,
                })
                raise

        if update_vals:
            self.with_context(skip_wis_sync=True).write(update_vals)

    




from odoo import models, fields, api
import logging
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

class ResPartnerLogs(models.Model):
    _name = 'logs.res.partner'
    _description = 'Logs de Integración de Agentes con WIS'
    _order = 'fecha desc'


    partner_id = fields.Many2one('res.partner', string='Agente', ondelete='cascade', required=True)
    fecha = fields.Datetime(string='Fecha', required=True, default=fields.Datetime.now
    )
    texto = fields.Text(string='Detalle')

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


    logs = fields.One2many('logs.res.partner', 'partner_id', string='Logs de Integración')


    def unlink(self):
        for partner in self:
            if partner.codigo_wms and partner.codigo_unico:
                raise ValidationError("No se puede eliminar un agente que ya está sincronizado con WMS (tiene código WMS y código único).")
        return super(ResPartner, self).unlink()

    def enviarWS(self):

        if self.integracion_wms == False:
            raise ValidationError("El agente no está marcado para integración con WIS")

        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")

        self.env['logs.res.partner'].create({
            'partner_id': self.id,
            'fecha': fields.Datetime.now(),
            'texto': 'Se envió la información a WIS',
        })  


        return datosAPI.insertarClienteOrSupplier(self);

    @api.model
    def create(self, vals):
        _logger.info("CREANDO AGENTE");
        res = super(ResPartner, self).create(vals)

        _logger.info(f"customer_rank: {res.customer_rank}, supplier_rank: {res.supplier_rank}");

        if (
            (res.customer_rank or 0) > 0 or
            (res.supplier_rank or 0) > 0 or
            (vals.get('customer_rank') or 0) > 0 or
            (vals.get('supplier_rank') or 0) > 0
        ):
            _logger.info("ES CLIENTE O SUPPLIER");
            if res.integracion_wms == True or vals.get('integracion_wms') == True:
                response = res.enviarWS()
                _logger.info(response)
                res.write({
                    'codigo_wms': response.get('numeroInterfaz', ''),
                    'codigo_unico': response.get('codigoUnico', ''),
                })

        return res


    def write(self, vals):
        _logger.info("EDITANDO AGENTE");

        res = super(ResPartner, self).write(vals);
        _logger.info(f"customer_rank: {self.customer_rank}, supplier_rank: {self.supplier_rank}");
        if self.customer_rank > 0 or self.supplier_rank > 0 or (vals.get('customer_rank', 0) > 0 or vals.get('supplier_rank', 0) > 0):
            
            if self.integracion_wms == True or vals.get('integracion_wms') == True and (not self.codigo_wms or self.codigo_wms == '' and not vals.get('codigo_wms', '') or vals.get('codigo_wms', '') == ''):
                _logger.info("ES CLIENTE O SUPPLIER");
                response = self.enviarWS();
                _logger.info(response);
                super(ResPartner, self).write({
                        'codigo_wms': response.get('numeroInterfaz', ''),
                        'codigo_unico': response.get('codigoUnico', '')
                    })
        return res;




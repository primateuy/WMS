from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging
_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    numeroInterfazWMS = fields.Char(
        string="Numero interfaz Wms",
        help="Identificación WMS"
    )

    referencia = fields.Char(string="Referencia", default="");

    enviadoWMS = fields.Boolean(string="Enviado de WMS", default=False)


    def buttonWMS(self):
        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")
        
        return datosAPI.editarReferencia(self);


    def enviarDatosWMS(self):
        datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)
        if not datosAPI or not datosAPI.apiLink:
            raise ValidationError("No se encuentran todos los datos para una consulta a la API")
        
        response = datosAPI.insertarOrdenDeCompra(self)

        # Verificar si la respuesta contiene los datos esperados
        if not response or 'numeroInterfaz' not in response or 'referencia' not in response:
            _logger.error("Error en la API: Respuesta incompleta o inválida")
            raise ValidationError("Error al procesar la solicitud: Respuesta incompleta o inválida")

        _logger.info(f"Datos enviados correctamente a WMS: {response}")
        return response


    def button_confirm(self):
        res = super(PurchaseOrder, self).button_confirm()

        # Enviar solo si no fue enviado y no estamos en skip_wms_integration
        for order in self:
                response = order.enviarDatosWMS()

                _logger.info(f"RESPONSE DESDE ORDEN: {response}");
                order.write({'enviadoWMS': True, 'referencia': response.get('referencia', ''), 'numeroInterfazWMS': response.get('numeroInterfaz', '')})
        return res

    def write(self, vals):

        record = super(PurchaseOrder, self).write(vals)

        

        return record;

    @api.model
    def create(self, vals):

        record = super(PurchaseOrder, self).create(vals)

        

            

        return record;


    
    








    

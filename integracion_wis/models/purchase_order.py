from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging
_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    integracion_wms = fields.Boolean(
        string='Integración con WIS',
        default=False,
    )

    numeroInterfazWMS = fields.Char(
        string="Numero interfaz Wms",
        help="Identificación WMS"
    )

    referencia = fields.Char(string="Referencia", default="");

    enviadoWMS = fields.Boolean(string="Enviado de WMS", default=False)

    def _create_picking(self):
        res = super(PurchaseOrder, self)._create_picking()

        pickings = res if not isinstance(res, bool) else self.picking_ids

        if pickings:
            for picking in pickings:
                if picking.move_ids:
                    
                    datosAPI = self.env['integracion_wis.integracion_wis'].search([], limit=1)

                    if datosAPI and datosAPI.apiLink and picking.state == picking.picking_type_id.estado_disparo_wms:
                        response = datosAPI.insertarReferenciaRecepcion(picking)
                        
                        if response and isinstance(response, dict):
                            picking.with_context(skip_wms_integration=True).write({
                                'idPedidoWMS': response.get('numeroInterfaz', ''),
                                'codigo_unico': response.get('codigoUnico', '')
                            })
                            self.env['wms.integracion.log'].create({
                                'fecha': fields.Datetime.now(),
                                'nivel': 'info',
                                'modelo': 'stock.picking',
                                'texto': f"Integración exitosa con WIS",
                                'picking_id': picking.id,
                                'resultado': 'exito',
                                'detalle': f"ID Pedido WMS: '{response.get('numeroInterfaz', '')}' | Código Único: '{response.get('codigoUnico', '')}'"
                            })
        
        return res

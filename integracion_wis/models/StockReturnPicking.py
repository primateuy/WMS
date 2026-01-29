from odoo import api, models, fields;

import logging;

_logger = logging.getLogger(__name__);


class ReturnPicking(models.TransientModel):
    _inherit = 'stock.return.picking'
    
    # def _create_returns(self):
        
    #     new_picking_id, picking_type_id = super()._create_returns()
        
    #     new_picking = self.env['stock.picking'].browse(new_picking_id)
        
    #     if hasattr(new_picking, 'es_devolucion_cliente'):
    #         new_picking.es_devolucion_cliente = True
        
    #     response = new_picking.insertarDevolucion()
    #     if response:
    #             new_picking.write({
    #                 'idPedidoWMS': response.get('numeroInterfaz', ''),
    #                 'codigo_unico': response.get('codigoUnico', '')
    #             })

    #     return new_picking_id, picking_type_id
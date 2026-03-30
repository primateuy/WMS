from odoo import models, api, fields

import logging;
_logger = logging.getLogger(__name__)

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    @api.model
    def updatePickingQuantity(self, picking_id, product_id, new_quantity):
        try:
            picking = self.env['stock.picking'].browse(picking_id)
            if not picking.exists():
                return {'error': 'Picking not found'}
            
            # Validar que el picking esté en estado editable
            if picking.state not in ('confirmed', 'waiting'):
                return {
                    'error': f'No se puede editar este picking. Estado actual: {picking.state}. Solo se permiten ediciones en pickings con estado "Confirmado" o "En espera".'
                }
            
            original_state = picking.state
            
            move_line = picking.move_ids_without_package.filtered(
                lambda m: m.product_id.id == int(product_id)
            )
            
            if not move_line:
                return {'error': 'Product not found in picking'}
            
            if len(move_line) > 1:
                move_line = move_line[0]
            
            old_quantity = move_line.product_uom_qty
            quantity_difference = float(new_quantity) - old_quantity
            
            move_line.with_context(do_not_propagate=True, no_recompute=True).write({
                'product_uom_qty': float(new_quantity),
                'quantity': float(new_quantity),
            })
            
            purchase_line = move_line.purchase_line_id
            surplus_updated = False
            
            if purchase_line and purchase_line.order_id.crossdock_enabled:
                purchase_order = purchase_line.order_id
                surplus_updated = self._update_surplus_picking_quantity(
                    purchase_order, purchase_line, -quantity_difference  # Signo contrario
                )

                self.update_transfer_picking(purchase_order, purchase_line, quantity_difference, picking)
            
            picking.write({'state': original_state})

            message = f'Cantidad actualizada a {new_quantity} unidades'
            if surplus_updated:
                if quantity_difference > 0:
                    message += f'. Se redujeron {quantity_difference} unidades del sobrante.'
                elif quantity_difference < 0:
                    message += f'. Se agregaron {abs(quantity_difference)} unidades al sobrante.'

            return {
                'success': True,
                'message': message,
                'surplus_updated': surplus_updated,
                'move_id': move_line.id,
                'quantity_difference': quantity_difference
            }
            
        except Exception as e:
            return {'error': str(e)}

    def update_transfer_picking(self, purchase_order, purchase_line, quantity_difference, current_picking):
        
        try:
            if not current_picking.location_id:
                _logger.warning("El picking actual no tiene location_id")
                return False

            crossdocking_location = current_picking.location_id
            
          
            transfer_pickings = purchase_order.picking_ids.filtered(
                lambda p: (
                    p.location_dest_id.id == crossdocking_location.id
                )
            )
            
            if transfer_pickings:
                _logger.info(f"Encontrados {len(transfer_pickings)} pickings candidatos: {transfer_pickings.mapped('name')}")
            else:
                _logger.warning(f"No se encontraron pickings de transferencia que salgan desde {crossdocking_location.name}")
                return False
            
            updated = False
            
            for transfer_picking in transfer_pickings:
                transfer_move = transfer_picking.move_ids_without_package.filtered(
                    lambda m: m.purchase_line_id.id == purchase_line.id
                )
                
                if not transfer_move:
                    _logger.info(f"Picking {transfer_picking.name} no contiene el producto {purchase_line.product_id.name}")
                    continue
                
                if len(transfer_move) > 1:
                    transfer_move = transfer_move[0]
                
                old_transfer_quantity = transfer_move.product_uom_qty
                new_transfer_quantity = old_transfer_quantity + quantity_difference
                
                if new_transfer_quantity < 0:
                    _logger.warning(f"Cantidad negativa detectada ({new_transfer_quantity}), ajustando a 0")
                    new_transfer_quantity = 0
                
                original_state = transfer_picking.state
                
                _logger.info(f"Actualizando {transfer_picking.name}: {old_transfer_quantity} → {new_transfer_quantity}")
                
                transfer_move.with_context(do_not_propagate=True, no_recompute=True).write({
                    'product_uom_qty': new_transfer_quantity,
                    'quantity': new_transfer_quantity,
                })
                
                if transfer_picking.state != original_state:
                    transfer_picking.write({'state': original_state})
                
                updated = True
                _logger.info(f"Picking {transfer_picking.name} actualizado correctamente")
            
            return updated
            
        except Exception as e:
            _logger.error(f"Error al actualizar picking de transferencia: {str(e)}")
            import traceback
            _logger.error(traceback.format_exc())
            return False
    
    def _update_surplus_picking_quantity(self, purchase_order, purchase_line, quantity_difference):
        
        try:
            entrance_location = purchase_order._get_or_create_entrance_location()
            
            main_warehouse = purchase_order.picking_type_id.warehouse_id or self.env['stock.warehouse'].search([
                ('company_id', '=', purchase_order.company_id.id)
            ], limit=1)
            
            if not main_warehouse:
                _logger.warning("No se encontró almacén principal")
                return False
            
            stock_location = main_warehouse.lot_stock_id
            
            surplus_pickings = purchase_order.picking_ids.filtered(
                lambda p: p.location_id.id == entrance_location.id and 
                         p.location_dest_id.id == stock_location.id)
            
            if not surplus_pickings:
                _logger.warning(f"No se encontraron pickings de sobrante para la orden {purchase_order.name}")
                return False
            
            for surplus_picking in surplus_pickings:
                surplus_move = surplus_picking.move_ids_without_package.filtered(
                    lambda m: m.purchase_line_id.id == purchase_line.id
                )
                
                if surplus_move:
                    if len(surplus_move) > 1:
                        surplus_move = surplus_move[0]
                    
                    old_surplus_quantity = surplus_move.product_uom_qty
                    new_surplus_quantity = old_surplus_quantity + quantity_difference
                    
                    if new_surplus_quantity < 0:
                        new_surplus_quantity = 0
                    
                    original_state = surplus_picking.state
                    surplus_move.with_context(do_not_propagate=True, no_recompute=True).write({
                        'product_uom_qty': new_surplus_quantity,
                        'quantity': new_surplus_quantity,
                    })
                    
                    if surplus_picking.state != original_state:
                        surplus_picking.write({'state': original_state})
                    
                    
                    return True
            
            return False
            
        except Exception as e:
            _logger.error(f"Error al actualizar picking de sobrante: {str(e)}")
            return False

    def button_validate(self):

        result = super(StockPicking, self).button_validate()

        for picking in self:
            if picking._is_crossdock_reception_picking():
                picking._activate_dependent_crossdock_pickings()
            elif picking._is_crossdock_picking():
                picking._activate_dependent_transfer_pickings()
            elif picking._is_transfer_picking():
                picking._reset_transfer_backorder()

        return result

    def _is_crossdock_reception_picking(self):
        """Determina si es un picking de Recepción Crossdock"""
        return 'Recepción Crossdock' in (self.origin or '')
    
    def _is_crossdock_picking(self):
        almacenes = self.env['stock.warehouse'].search([])
        crossdocking_locations_ids = [alm.crossdocking_location_id.id for alm in almacenes if alm.crossdocking_location_id]
        
        return (
            self.location_dest_id.id in crossdocking_locations_ids and
            'Crossdock' in (self.origin or '')
        )
    
    def _is_transfer_picking(self):
        almacenes = self.env['stock.warehouse'].search([])
        crossdocking_locations_ids = [alm.crossdocking_location_id.id for alm in almacenes if alm.crossdocking_location_id]
        
        return (
            self.location_id.id in crossdocking_locations_ids and
            'Crossdock' in (self.origin or '')
        )

    def _reset_transfer_backorder(self):
        
        backorder = self.env['stock.picking'].search([
            ('backorder_id', '=', self.id),
            ('state', 'not in', ('done', 'cancel')),
        ], limit=1)

        if not backorder:
            return

        try:
            for move in backorder.move_ids:
                move.with_context(do_not_propagate=True, no_recompute=True).write({
                    'quantity': 0,
                })
            _logger.info(f"Backorder {backorder.name}: quantity=0, esperando siguiente crossdock")
        except Exception as e:
            _logger.error(f"Error al resetear backorder {backorder.name}: {str(e)}")

    def action_assign(self):
        
        crossdock_picks = self.filtered(lambda p: p._is_crossdock_picking())
        transfer_picks = self.filtered(lambda p: p._is_transfer_picking())
        regular_picks = self - crossdock_picks - transfer_picks

        if regular_picks:
            super(StockPicking, regular_picks).action_assign()

        for picking in crossdock_picks:
            picking._assign_from_reception()

        for picking in transfer_picks:
            picking._assign_transfer_from_crossdock()

        return True

    def _get_crossdock_purchase_order(self):
        """Helper: obtiene la purchase order del crossdock picking."""
        po = self.purchase_id
        if not po:
            po_prefix = (self.origin or '').split(' - ')[0].strip()
            if po_prefix:
                po = self.env['purchase.order'].search([('name', '=', po_prefix)], limit=1)
        return po if po and po.crossdock_enabled else None

    def _assign_from_reception(self):
        
        try:
            purchase_order = self._get_crossdock_purchase_order()
            if not purchase_order:
                return

            received_qty = {}
            for p in purchase_order.picking_ids:
                if (p.state == 'done' and
                        p.location_dest_id.id == self.location_id.id and
                        'Recepción Crossdock' in (p.origin or '')):
                    for m in p.move_ids:
                        pid = m.product_id.id
                        received_qty[pid] = received_qty.get(pid, 0) + m.quantity

            if not received_qty:
                return

            taken_qty = {}
            for p in purchase_order.picking_ids:
                if (p.id != self.id and
                        p.location_id.id == self.location_id.id and
                        'Crossdock' in (p.origin or '') and
                        p.state in ('done', 'assigned', 'partially_available')):
                    for m in p.move_ids:
                        pid = m.product_id.id
                        taken_qty[pid] = taken_qty.get(pid, 0) + m.quantity

            any_assigned = False
            for move in self.move_ids:
                if move.state in ('done', 'cancel'):
                    continue
                pid = move.product_id.id
                available = max(0, received_qty.get(pid, 0) - taken_qty.get(pid, 0))
                qty_to_set = min(available, move.product_uom_qty)
                move.with_context(do_not_propagate=True, no_recompute=True).write({'quantity': qty_to_set})
                if qty_to_set > 0:
                    any_assigned = True

            if any_assigned:
                self.write({'state': 'assigned'})
            _logger.info(f"{self.name} _assign_from_reception: recibido={received_qty}, tomado={taken_qty}")
        except Exception as e:
            _logger.error(f"Error en _assign_from_reception {self.name}: {str(e)}")

    def _assign_transfer_from_crossdock(self):
        
        try:
            purchase_order = self._get_crossdock_purchase_order()
            if not purchase_order:
                return

            crossdock_location_id = self.location_id.id

            # Total procesado en crossdock pickings que van a esta ubicación crossdock
            total_done_by_product = {}
            for p in purchase_order.picking_ids:
                if (p.state == 'done' and
                        p.location_dest_id.id == crossdock_location_id and
                        'Crossdock' in (p.origin or '')):
                    for m in p.move_ids:
                        pid = m.product_id.id
                        total_done_by_product[pid] = total_done_by_product.get(pid, 0) + m.quantity

            if not total_done_by_product:
                return

            any_assigned = False
            for move in self.move_ids:
                if move.state in ('done', 'cancel'):
                    continue
                pid = move.product_id.id
                qty_to_set = min(total_done_by_product.get(pid, 0), move.product_uom_qty)
                move.with_context(do_not_propagate=True, no_recompute=True).write({'quantity': qty_to_set})
                if qty_to_set > 0:
                    any_assigned = True

            if any_assigned:
                self.write({'state': 'assigned'})
            _logger.info(f"{self.name} _assign_transfer_from_crossdock: done={total_done_by_product}")
        except Exception as e:
            _logger.error(f"Error en _assign_transfer_from_crossdock {self.name}: {str(e)}")

    def _create_backorder(self):
        
        backorders = super(StockPicking, self)._create_backorder()

        for backorder in backorders:
            original = backorder.backorder_id
            if original and original._is_transfer_picking():
                try:
                    for move in backorder.move_ids:
                        move.with_context(do_not_propagate=True, no_recompute=True).write({
                            'quantity': 0
                        })
                    _logger.info(f"Backorder transfer {backorder.name}: quantity reseteada a 0")
                except Exception as e:
                    _logger.error(f"Error al resetear backorder transfer {backorder.name}: {str(e)}")
            elif original and original._is_crossdock_picking():
                try:
                    backorder._assign_from_reception()
                    _logger.info(f"Backorder crossdock {backorder.name}: asignado desde recepción")
                except Exception as e:
                    _logger.error(f"Error al asignar backorder crossdock {backorder.name}: {str(e)}")

        return backorders

    def _activate_dependent_crossdock_pickings(self):
        
        try:
            purchase_order = self.purchase_id
            if not purchase_order or not purchase_order.crossdock_enabled:
                return

            crossdocking_pickings = purchase_order.picking_ids.filtered(
                lambda p: (
                    p.state == 'waiting' and
                    p.id != self.id and
                    'Crossdock' in (p.origin or '') and
                    p.location_id.id == self.location_dest_id.id
                )
            )

            if not crossdocking_pickings:
                return

            done_qty_by_product = {}
            for src_move in self.move_ids:
                pid = src_move.product_id.id
                done_qty_by_product[pid] = done_qty_by_product.get(pid, 0) + src_move.quantity

            for picking in crossdocking_pickings:
                try:
                    for move in picking.move_ids:
                        done_qty = done_qty_by_product.get(move.product_id.id, 0)
                        qty_to_set = min(done_qty, move.product_uom_qty)
                        move.with_context(do_not_propagate=True, no_recompute=True).write({
                            'quantity': qty_to_set
                        })
                    picking.write({'state': 'assigned'})
                    _logger.info(f"Picking crossdock {picking.name} activado (Listo): {done_qty_by_product}")
                except Exception as e:
                    _logger.warning(f"No se pudo activar el picking {picking.name}: {str(e)}")

        except Exception as e:
            _logger.error(f"Error al activar pickings de crossdocking: {str(e)}")
    
    def _activate_dependent_transfer_pickings(self):
        
        try:
            purchase_order = self._get_crossdock_purchase_order()
            if not purchase_order:
                return

            transfer_pickings = purchase_order.picking_ids.filtered(
                lambda p: p.state not in ('done', 'cancel') and
                          p.id != self.id and
                          'Crossdock' in (p.origin or '') and
                          p.location_id.id == self.location_dest_id.id
            )

            for picking in transfer_pickings:
                try:
                    picking._assign_transfer_from_crossdock()
                except Exception as e:
                    _logger.warning(f"No se pudo activar {picking.name}: {str(e)}")

        except Exception as e:
            _logger.error(f"Error al activar pickings de transferencia: {str(e)}")
    
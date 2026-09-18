# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..tools.excel_parser import ExcelParser
from .common import PrimateStockImportCommon


@tagged('post_install', '-at_install')
class TestPickingImport(PrimateStockImportCommon):

    def test_wizard_full_flow(self):
        """Validar -> importar crea pickings en borrador vinculados a la sesión."""
        wizard = self._wizard(self._make_xlsx([
            self._int_row('IMP-1', 'IMP-A', 10, origin='OC-77', scheduled_date='2026-09-20'),
            self._int_row('IMP-1', 'IMP-B', 2, origin='OC-77', scheduled_date='2026-09-20'),
            self._int_row('IMP-2', 'IMP-LOT', 1, lot='L-1'),
        ]))
        wizard.action_validate()
        self.assertEqual(wizard.state, 'validated')
        session = wizard.session_id
        self.assertEqual(session.state, 'validated')
        self.assertEqual(session.total_rows, 3)
        self.assertFalse(session.has_errors)
        self.assertEqual(session.warning_count, 1)
        self.assertTrue(session.name.startswith('IMP/'))
        self.assertTrue(session.file)

        wizard.action_import()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(session.state, 'imported')
        pickings = session.picking_ids
        self.assertEqual(len(pickings), 2)
        self.assertEqual(session.move_count, 3)
        self.assertTrue(all(picking.state == 'draft' for picking in pickings))
        self.assertTrue(all(picking.is_imported for picking in pickings))

        first = pickings.filtered(lambda picking: picking.origin == 'OC-77')
        self.assertEqual(len(first.move_ids), 2)
        self.assertEqual(first.picking_type_id, self.picking_type_int)
        self.assertEqual(first.location_id, self.stock_location)
        self.assertEqual(first.location_dest_id, self.shelf_location)
        move_a = first.move_ids.filtered(lambda move: move.product_id == self.product_a)
        self.assertEqual(move_a.product_uom_qty, 10)

        second = pickings - first
        self.assertEqual(second.origin, 'IMP-2')
        self.assertIn('L-1', second.move_ids.description_picking)

    def test_import_blocked_with_errors(self):
        wizard = self._wizard(self._make_xlsx([
            self._int_row('IMP-1', 'NO-EXISTE', 10),
        ]))
        wizard.action_validate()
        self.assertTrue(wizard.has_errors)
        self.assertEqual(wizard.session_id.state, 'failed')
        with self.assertRaisesRegex(UserError, 'errores bloqueantes'):
            wizard.action_import()
        self.assertFalse(wizard.session_id.picking_ids)

    def test_revalidate_reuses_session(self):
        wizard = self._wizard(self._make_xlsx([self._int_row('IMP-1', 'NO-EXISTE', 1)]))
        wizard.action_validate()
        session = wizard.session_id
        wizard.write({'file': self._wizard(self._make_xlsx(
            [self._int_row('IMP-1', 'IMP-A', 1)]
        )).file})
        wizard.action_validate()
        self.assertEqual(wizard.session_id, session)
        self.assertEqual(session.state, 'validated')

    def test_check_availability_batch(self):
        """La acción masiva confirma y reserva; un picking sin stock no bloquea al resto."""
        wizard = self._wizard(self._make_xlsx([
            self._int_row('IMP-1', 'IMP-A', 10),   # hay 100 en stock
            self._int_row('IMP-2', 'IMP-B', 5),    # sin stock: queda confirmado, no reservado
        ]))
        wizard.action_validate()
        wizard.action_import()
        pickings = wizard.session_id.picking_ids
        done_picking = self.env['stock.picking'].create({
            'picking_type_id': self.picking_type_int.id,
            'location_id': self.stock_location.id,
            'location_dest_id': self.shelf_location.id,
        })
        done_picking.action_cancel()

        action = (pickings | done_picking).action_check_availability_batch()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn('2 operaciones procesadas', action['params']['message'])
        self.assertIn('1 omitidas', action['params']['message'])
        picking_a = pickings.filtered(lambda picking: picking.product_id == self.product_a)
        picking_b = pickings - picking_a
        self.assertEqual(picking_a.state, 'assigned')
        self.assertEqual(picking_b.state, 'confirmed')

    def test_check_availability_batch_logs_failure(self):
        """Un picking sin movimientos falla en action_assign y se anota en el chatter."""
        empty_picking = self.env['stock.picking'].create({
            'picking_type_id': self.picking_type_int.id,
            'location_id': self.stock_location.id,
            'location_dest_id': self.shelf_location.id,
        })
        action = empty_picking.action_check_availability_batch()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn(empty_picking.name, action['params']['message'])
        self.assertTrue(empty_picking.message_ids.filtered(
            lambda message: 'Comprobar disponibilidad' in (message.body or '')
        ))

    def test_download_template(self):
        wizard = self.env['primate_stock_import.wizard'].create({})
        action = wizard.action_download_template()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertTrue(wizard.template_file)
        # La plantilla generada se puede volver a leer con el parser (filas de ejemplo).
        rows = ExcelParser(self.env).parse(base64.b64decode(wizard.template_file))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['quantity'], '5,5')

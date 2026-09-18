# -*- coding: utf-8 -*-
from odoo.tests import tagged

from ..tools.excel_parser import ExcelParser
from ..tools.row_validator import RowValidator
from .common import PrimateStockImportCommon


@tagged('post_install', '-at_install')
class TestRowValidator(PrimateStockImportCommon):

    def _validate(self, rows):
        parsed = ExcelParser(self.env).parse(self._make_xlsx(rows))
        return RowValidator(self.env).validate(parsed)

    def test_valid_file_groups_by_operation_ref(self):
        result = self._validate([
            self._int_row('IMP-1', 'IMP-A', 10, scheduled_date='2026-09-20'),
            self._int_row('IMP-1', '7790001234567', '2,5', scheduled_date='2026-09-20',
                          uom=self.product_b.uom_id.name),
            self._int_row('IMP-2', 'IMP-A', 1),
        ])
        self.assertFalse(result['has_blocking_errors'], result['errors'])
        self.assertEqual(result['picking_count'], 2)
        self.assertEqual(result['move_count'], 3)
        group = result['groups'][0]
        self.assertEqual(group['picking_type_id'], self.picking_type_int.id)
        self.assertEqual(group['location_id'], self.stock_location.id)
        self.assertEqual(group['location_dest_id'], self.shelf_location.id)
        self.assertTrue(group['scheduled_date'].startswith('2026-09-'))
        lines = group['lines']
        self.assertEqual(lines[1]['product_id'], self.product_b.id)
        self.assertEqual(lines[1]['quantity'], 2.5)
        self.assertEqual(lines[1]['product_uom_id'], self.product_b.uom_id.id)

    def test_picking_type_by_display_name_and_code(self):
        display_name = f"{self.warehouse.name}: {self.picking_type_int.name}"
        result = self._validate([
            self._int_row('IMP-1', 'IMP-A', 1, picking_type=display_name, warehouse=''),
            self._int_row('IMP-2', 'IMP-A', 1, picking_type='INT'),
        ])
        self.assertFalse(result['has_blocking_errors'], result['errors'])
        self.assertEqual(
            {group['picking_type_id'] for group in result['groups']}, {self.picking_type_int.id}
        )

    def test_default_locations_from_picking_type(self):
        result = self._validate([{
            'operation_ref': 'REC-1', 'picking_type': self.picking_type_in.name,
            'warehouse': self.warehouse.name, 'partner': self.partner.name,
            'product': 'IMP-A', 'quantity': 3,
        }])
        self.assertFalse(result['has_blocking_errors'], result['errors'])
        group = result['groups'][0]
        self.assertEqual(group['partner_id'], self.partner.id)
        # Recepción: origen = ubicación de proveedor del contacto, destino = stock del almacén.
        self.assertEqual(group['location_id'], self.partner.property_stock_supplier.id)
        self.assertEqual(
            group['location_dest_id'], self.picking_type_in.default_location_dest_id.id
        )

    def test_blocking_errors(self):
        result = self._validate([
            self._int_row('IMP-1', 'NO-EXISTE', 1),
            self._int_row('IMP-1', 'IMP-A', 0),
            self._int_row('IMP-1', 'IMP-A', 'abc'),
            self._int_row('IMP-2', 'IMP-A', 1, picking_type='Tipo Inexistente'),
            self._int_row('IMP-3', 'IMP-A', 1, location_dest='Ubicación Inexistente'),
            self._int_row('IMP-4', 'IMP-A', 1, scheduled_date='ayer'),
            self._int_row('IMP-5', 'IMP-A', 1, uom='kg'),
        ])
        self.assertTrue(result['has_blocking_errors'])
        errors = result['errors']
        self.assertIn('2', errors)  # producto inexistente
        self.assertIn('3', errors)  # cantidad cero
        self.assertIn('4', errors)  # cantidad no numérica
        self.assertIn('5', errors)  # tipo de operación
        self.assertIn('6', errors)  # ubicación
        self.assertIn('7', errors)  # fecha
        self.assertIn('8', errors)  # UdM de otra categoría
        # los grupos con errores de cabezal no se construyen
        self.assertNotIn('IMP-2', [group['operation_ref'] for group in result['groups']])

    def test_header_inconsistency_within_group(self):
        result = self._validate([
            self._int_row('IMP-1', 'IMP-A', 1, origin='OC-1'),
            self._int_row('IMP-1', 'IMP-B', 1, origin='OC-2'),
        ])
        self.assertTrue(result['has_blocking_errors'])
        self.assertIn('3', result['errors'])
        self.assertIn('origin', result['errors']['3'][0])

    def test_lot_warnings(self):
        result = self._validate([
            self._int_row('IMP-1', 'IMP-LOT', 5),                      # sin lote
            self._int_row('IMP-1', 'IMP-LOT', 5, lot='L-NUEVO'),       # lote inexistente
            self._int_row('IMP-1', 'IMP-A', 5, lot='L-IGNORADO'),      # producto sin seguimiento
        ])
        self.assertFalse(result['has_blocking_errors'], result['errors'])
        self.assertEqual(set(result['warnings']), {'2', '3', '4'})
        self.assertEqual(result['groups'][0]['lines'][1]['lot_name'], 'L-NUEVO')
        self.assertFalse(result['groups'][0]['lines'][2]['lot_name'])

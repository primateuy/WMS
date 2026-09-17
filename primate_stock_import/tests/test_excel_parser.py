# -*- coding: utf-8 -*-
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..tools.excel_parser import ExcelParser
from .common import PrimateStockImportCommon


@tagged('post_install', '-at_install')
class TestExcelParser(PrimateStockImportCommon):

    def test_parse_rows_as_text(self):
        """Números enteros sin '.0', decimales con punto, fechas ISO, texto recortado."""
        file_bytes = self._make_xlsx([
            {'operation_ref': ' IMP-1 ', 'picking_type': 'INT', 'product': 'IMP-A',
             'quantity': 10, 'scheduled_date': '2026-09-20'},
            {'operation_ref': 'IMP-1', 'picking_type': 'INT', 'product': 'IMP-B',
             'quantity': 2.5},
        ])
        rows = ExcelParser(self.env).parse(file_bytes)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['row'], 2)
        self.assertEqual(rows[0]['operation_ref'], 'IMP-1')
        self.assertEqual(rows[0]['quantity'], '10')
        self.assertEqual(rows[1]['quantity'], '2.5')
        # columnas opcionales ausentes en el archivo quedan como ''
        self.assertEqual(rows[0]['lot'], '')

    def test_header_case_insensitive_and_optional_columns(self):
        file_bytes = self._make_xlsx(
            [{'OPERATION_REF': 'X', 'Picking_Type': 'INT', 'Product': 'IMP-A', 'QUANTITY': 1}],
            columns=('OPERATION_REF', 'Picking_Type', 'Product', 'QUANTITY'),
        )
        rows = ExcelParser(self.env).parse(file_bytes)
        self.assertEqual(rows[0]['operation_ref'], 'X')
        self.assertEqual(rows[0]['warehouse'], '')

    def test_missing_required_column(self):
        file_bytes = self._make_xlsx(
            [{'operation_ref': 'X', 'picking_type': 'INT', 'product': 'IMP-A'}],
            columns=('operation_ref', 'picking_type', 'product'),
        )
        with self.assertRaisesRegex(UserError, 'quantity'):
            ExcelParser(self.env).parse(file_bytes)

    def test_empty_rows_are_skipped_and_no_data_raises(self):
        with self.assertRaisesRegex(UserError, 'no tiene filas'):
            ExcelParser(self.env).parse(self._make_xlsx([]))

    def test_invalid_file(self):
        with self.assertRaises(UserError):
            ExcelParser(self.env).parse(b'esto no es un excel')

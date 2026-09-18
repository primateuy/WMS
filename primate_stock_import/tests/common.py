# -*- coding: utf-8 -*-
import base64
import io

import xlsxwriter

from odoo.tests import TransactionCase

from ..tools.excel_parser import COLUMNS


class PrimateStockImportCommon(TransactionCase):
    """Datos base y utilidades para generar Excel en memoria."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1
        )
        cls.picking_type_int = cls.warehouse.int_type_id
        # En una BD sin rutas multi-paso el tipo de transferencia interna está archivado.
        cls.picking_type_int.active = True
        cls.picking_type_in = cls.warehouse.in_type_id
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.shelf_location = cls.env['stock.location'].create({
            'name': 'Estantería Import',
            'location_id': cls.stock_location.id,
            'usage': 'internal',
        })
        cls.product_a = cls.env['product.product'].create({
            'name': 'Producto Import A',
            'default_code': 'IMP-A',
            'detailed_type': 'product',
        })
        cls.product_b = cls.env['product.product'].create({
            'name': 'Producto Import B',
            'default_code': 'IMP-B',
            'barcode': '7790001234567',
            'detailed_type': 'product',
        })
        cls.product_lot = cls.env['product.product'].create({
            'name': 'Producto Import Lote',
            'default_code': 'IMP-LOT',
            'detailed_type': 'product',
            'tracking': 'lot',
        })
        cls.partner = cls.env['res.partner'].create({'name': 'Proveedor Import SA'})
        cls.env['stock.quant']._update_available_quantity(
            cls.product_a, cls.stock_location, 100
        )

    @classmethod
    def _make_xlsx(cls, rows, columns=COLUMNS):
        """Genera un .xlsx con ``columns`` como encabezado y ``rows`` (dicts).

        Returns:
            bytes: contenido del archivo.
        """
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet("Operaciones")
        for col, column in enumerate(columns):
            sheet.write(0, col, column)
        for row_index, row in enumerate(rows, start=1):
            for col, column in enumerate(columns):
                value = row.get(column, '')
                if value != '':
                    sheet.write(row_index, col, value)
        workbook.close()
        return output.getvalue()

    def _int_row(self, ref, product, quantity, **extra):
        """Fila de traslado interno Stock -> Estantería para el producto dado."""
        row = {
            'operation_ref': ref,
            'picking_type': self.picking_type_int.name,
            'warehouse': self.warehouse.name,
            'location_origin': self.stock_location.complete_name,
            'location_dest': self.shelf_location.complete_name,
            'product': product,
            'quantity': quantity,
        }
        row.update(extra)
        return row

    def _wizard(self, file_bytes, file_name='import.xlsx'):
        return self.env['primate_stock_import.wizard'].create({
            'file': base64.b64encode(file_bytes),
            'file_name': file_name,
        })

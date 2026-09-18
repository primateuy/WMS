# -*- coding: utf-8 -*-
"""Lectura del Excel de operaciones a una lista de filas normalizadas.

Se usa ``xlrd`` (requisito estándar de Odoo 17, que en su versión 1.2 lee
tanto .xls como .xlsx) para no sumar dependencias Python al servidor. La
conversión de celdas replica la de ``base_import``: todo se entrega como texto
ya recortado, las fechas en formato ISO y los enteros sin ".0".
"""
import datetime
import logging

from odoo import _
from odoo.exceptions import UserError
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT

try:
    import xlrd
except ImportError:
    xlrd = None

_logger = logging.getLogger(__name__)

# Columnas de la plantilla, en el orden en que se generan.
COLUMNS = (
    'operation_ref',
    'picking_type',
    'warehouse',
    'partner',
    'location_origin',
    'location_dest',
    'scheduled_date',
    'origin',
    'product',
    'lot',
    'quantity',
    'uom',
)
REQUIRED_COLUMNS = ('operation_ref', 'picking_type', 'product', 'quantity')

# Columnas que definen el cabezal de la operación: deben coincidir en todas las
# filas con la misma operation_ref.
HEADER_COLUMNS = (
    'picking_type',
    'warehouse',
    'partner',
    'location_origin',
    'location_dest',
    'scheduled_date',
    'origin',
)


class ExcelParser:
    """Convierte el archivo Excel en filas ``dict`` listas para validar."""

    def __init__(self, env):
        self.env = env

    def parse(self, file_bytes):
        """Lee la primera hoja del archivo y devuelve sus filas.

        Args:
            file_bytes (bytes): contenido binario del .xlsx/.xls.

        Returns:
            list[dict]: una entrada por fila con datos. Cada dict tiene la
                clave ``row`` (número de fila en Excel, base 1) y una clave por
                columna de ``COLUMNS`` con el valor como texto ('' si vacío).

        Raises:
            UserError: si falta la librería, el archivo no se puede abrir, no
                tiene encabezado o faltan columnas obligatorias.
        """
        if xlrd is None:
            raise UserError(_(
                "Falta la librería Python 'xlrd' en el servidor. Es un requisito estándar "
                "de Odoo; instalala con: pip install xlrd==1.2.0"
            ))
        try:
            book = xlrd.open_workbook(file_contents=file_bytes or b'')
        except Exception as error:  # xlrd lanza varios tipos según el formato
            _logger.info("No se pudo abrir el Excel de importación: %s", error)
            raise UserError(_(
                "No se pudo leer el archivo. Verificá que sea un Excel válido (.xlsx o .xls). "
                "Detalle: %s", error
            )) from error
        sheet = book.sheet_by_index(0)
        if sheet.nrows < 1:
            raise UserError(_("El archivo está vacío: no tiene fila de encabezados."))

        column_index = self._read_header(book, sheet)
        rows = []
        for row_number in range(1, sheet.nrows):
            values = {
                column: self._cell_to_text(book, sheet.cell(row_number, index))
                if index is not None else ''
                for column, index in column_index.items()
            }
            if not any(values.values()):
                continue
            values['row'] = row_number + 1
            rows.append(values)
        if not rows:
            raise UserError(_("El archivo no tiene filas de datos debajo del encabezado."))
        return rows

    def _read_header(self, book, sheet):
        """Mapea cada columna conocida a su índice en la fila 1.

        Los encabezados se comparan en minúsculas y sin espacios en los bordes.
        Columnas opcionales ausentes quedan mapeadas a ``None``.

        Returns:
            dict: {nombre_columna: índice | None}.

        Raises:
            UserError: si falta alguna columna de ``REQUIRED_COLUMNS``.
        """
        headers = [
            self._cell_to_text(book, sheet.cell(0, index)).lower()
            for index in range(sheet.ncols)
        ]
        column_index = {
            column: headers.index(column) if column in headers else None
            for column in COLUMNS
        }
        missing = [column for column in REQUIRED_COLUMNS if column_index[column] is None]
        if missing:
            raise UserError(_(
                "Faltan columnas obligatorias en el encabezado: %s. "
                "Descargá la plantilla desde el asistente para ver el formato esperado.",
                ", ".join(missing),
            ))
        return column_index

    @staticmethod
    def _cell_to_text(book, cell):
        """Devuelve el valor de la celda como texto recortado.

        Números enteros sin decimales, fechas en formato ISO (con hora si la
        celda la tiene) y booleanos como 'True'/'False'.
        """
        if cell.ctype == xlrd.XL_CELL_NUMBER:
            value = cell.value
            return str(int(value)) if value % 1 == 0.0 else repr(value)
        if cell.ctype == xlrd.XL_CELL_DATE:
            date_value = datetime.datetime(*xlrd.xldate.xldate_as_tuple(cell.value, book.datemode))
            has_time = cell.value % 1 != 0.0
            return date_value.strftime(
                DEFAULT_SERVER_DATETIME_FORMAT if has_time else DEFAULT_SERVER_DATE_FORMAT
            )
        if cell.ctype == xlrd.XL_CELL_BOOLEAN:
            return 'True' if cell.value else 'False'
        if cell.ctype == xlrd.XL_CELL_ERROR:
            return ''
        return str(cell.value or '').strip()

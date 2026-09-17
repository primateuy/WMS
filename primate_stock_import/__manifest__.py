# -*- coding: utf-8 -*-
{
    'name': 'Importación de Operaciones de Inventario desde Excel',
    'version': '17.0.1.0.0',
    'author': 'PrimateUY',
    'website': 'https://primate.uy',
    'category': 'Inventory/Inventory',
    'license': 'AGPL-3',
    'summary': """Importa operaciones de inventario (stock.picking) desde un archivo Excel,
con validación previa, log por sesión y comprobación de disponibilidad masiva""",
    'description': """
Importación de Operaciones de Inventario desde Excel
====================================================

Permite cargar operaciones de inventario (recepciones, entregas, traslados
internos) desde un archivo Excel y generarlas en estado **borrador**.

Flujo:

1. El usuario descarga la plantilla desde el asistente.
2. Completa una fila por línea de movimiento; las filas con la misma
   ``operation_ref`` se agrupan en una misma operación.
3. Sube el archivo y **valida**: el asistente resuelve tipos de operación,
   ubicaciones, productos, UdM y fechas, y muestra un resumen de errores
   (bloqueantes) y advertencias por fila. No se crea nada en este paso.
4. Si no hay errores, **importa**: se crean los ``stock.picking`` con sus
   ``stock.move`` en borrador, vinculados a una sesión de importación que
   guarda el archivo original y el log completo para auditoría.
5. Desde la lista de operaciones, la acción masiva *Comprobar disponibilidad
   (importadas)* confirma y reserva las operaciones seleccionadas, dejando en
   el chatter de cada una el error si lo hubo, sin bloquear al resto.

Sin dependencias Python adicionales: la lectura usa ``xlrd`` y la plantilla se
genera con ``xlsxwriter``, ambos requisitos estándar de Odoo 17.
    """,
    'depends': ['stock'],
    'data': [
        # GRUPOS
        'security/primate_stock_import_groups.xml',
        # IR.MODEL.ACCESS.CSV
        'security/ir.model.access.csv',
        # DATOS
        'data/primate_stock_import_session_data.xml',
        # VISTAS
        'views/primate_stock_import_session_views.xml',
        'views/stock_picking_views.xml',
        'wizard/primate_stock_import_wizard_views.xml',
        # MENU
        'views/primate_stock_import_menus.xml',
    ],
    'auto_install': False,
    'installable': True,
    'application': False,
}

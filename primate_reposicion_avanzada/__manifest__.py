# -*- coding: utf-8 -*-
{
    'name': 'Reposición Avanzada — PrimateUY',
    'version': '17.0.1.1.0',
    'author': 'PrimateUY',
    'website': 'https://primate.uy',
    'category': 'Inventory',
    'license': 'AGPL-3',
    'summary': """Extensión de setu_advance_reordering para carga por categoría/grupo, múltiplos
        de distribución, clasificación de factibilidad y estrategias de distribución parcial.""",
    'description': """
        Extiende setu_advance_reordering por herencia, sin modificar código del vendor.

        - Carga asistida de productos por categoría Y grupo de almacenes del producto (AND).
        - Selección de almacenes destino por grupo de almacenes, aditiva a la carga manual.
        - Validación del grupo de almacenes del producto: ningún almacén fuera del grupo
          recibe mercadería, aunque haya ventas registradas ahí.
        - Ajuste de la cantidad al múltiplo de distribución del producto, con dirección de
          redondeo elegible (arriba/abajo).
        - Clasificación de líneas por factibilidad, evaluando la escasez de forma agregada
          por producto contra el stock del almacén origen.
        - Cinco estrategias de distribución del stock disponible cuando el origen no alcanza.
        - Tipo de operación de inventario configurable por canal ICT/IWT.
        - Aviso por correo cuando el planificador genera la reposición sin validarla.
    """,
    'depends': [
        'setu_advance_reordering',
        'setu_intercompany_transaction',
        'automatizacion_reglas_abastecimiento',
        'automatic_crossdocking',
        'stock_multiplos_validation',
    ],
    # No se agregan modelos nuevos (todo por herencia), así que no hay ACLs ni grupos propios.
    'data': [
        'views/advance_procurement_process_views.xml',
        'views/advance_reorder_orderprocess_views.xml',
        'views/setu_interwarehouse_channel_views.xml',
        'views/setu_intercompany_channel_views.xml',
        'views/sale_order_views.xml',
        'wizard/create_reordering_views.xml',
    ],
    'auto_install': False,
    'installable': True,
    'application': False,
}

# -*- coding: utf-8 -*-
{
    'name': 'Impresión masiva de remitos',
    'version': '17.0.1.0.0',
    'category': 'Inventario',
    'summary': 'Impresión y descarga masiva de remitos por lotes, con PDF de UCFE o reporte estándar',
    'description': """
Impresión masiva de remitos
===========================

Agrega una acción de impresión/descarga masiva sobre cualquier lista de
operaciones de inventario, procesando los registros en lotes para no
sobrecargar el servidor.

Para cada operación elige el PDF del CFE emitido por UCFE cuando existe, y el
reporte estándar de Odoo cuando la operación no emite CFE.

El módulo NO depende de la facturación electrónica: si no hay ningún módulo
``l10n_uy_einvoice_*`` instalado funciona igual, imprimiendo siempre el
reporte estándar.

Incluye además:

* Control de operaciones impresas (automático y manual).
* Restricción por usuario de los tipos de operación visibles.
* Perfil "Impresión de remitos (solo)" para operadores logísticos.
    """,
    'author': 'PrimateUY',
    'website': 'https://primate.uy',
    'depends': ['stock', 'web'],
    'data': [
        'security/remito_security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'data/server_actions.xml',
        'wizard/remito_print_wizard_views.xml',
        'views/stock_picking_type_views.xml',
        'views/stock_picking_views.xml',
        'views/res_users_views.xml',
        'views/remito_menu_views.xml',
        # Al final: necesita que el menú Remitos ya exista.
        'data/menu_restriction.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'stock_remito_print/static/src/js/remito_print_action.js',
            'stock_remito_print/static/src/xml/remito_print_action.xml',
            'stock_remito_print/static/src/css/remito_print.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}

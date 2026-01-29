# -*- coding: utf-8 -*-
{
    'name': "Integración con WIS",

    'summary': "Integración con WIS para una gestión eficiente",

    'description': """
 Módulo para configurar e integrar con la API de WIS
    """,

    'author': "Avance Software",
    'website': "https://avancesoftware.us/",
    'category': 'Uncategorized',
    'version': '0.1',
    'application': True,
    'installable': True,
    'icon': '/integracion_wis/static/description/icon.png',

    'depends': ['base', 'contacts', 'stock', 'account', 'sale_management', 'purchase', 'product', 'stock_barcode'],

    'data': [
        'security/ir.model.access.csv',
        'views/views.xml',
        'views/templates.xml',
        'views/ProductView.xml',
        'views/PartnerView.xml',
        'views/StockPickingTypeView.xml',
        'views/StockPickingView.xml',
        'views/PickingWMSlog.xml',
        'views/PurchaseOrderView.xml',
        'views/WizardStock.xml',
        'data/cron.xml',        
    ],
    'demo': [
        'demo/demo.xml',
    ],
}


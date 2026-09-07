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
    'version': '0.4',
    'application': True,
    'installable': True,
    'icon': '/integracion_wis/static/description/icon.png',

    'depends': ['base', 'contacts', 'stock', 'account', 'sale_management', 'purchase', 'purchase_stock', 'product', 'stock_barcode', 'l10n_uy_einvoice_base', 'l10n_uy_einvoice_uruware'],

    'data': [
        'security/ir.model.access.csv',
        'views/views.xml',
        'views/wis_webhook_log_views.xml',
        'views/wis_sync_queue_views.xml',
        'views/wizard_integrar_variantes_views.xml',
        'views/templates.xml',
        'views/ProductView.xml',
        'views/PartnerView.xml',
        'views/StockPickingTypeView.xml',
        'views/StockPickingView.xml',
        'views/StockLocationView.xml',
        'views/PickingWMSlog.xml',
        'views/PurchaseOrderView.xml',
        'views/WizardStock.xml',
        'data/cron.xml',
        'data/default_params.xml',
        'views/UomView.xml',
    ],
    'demo': [
        'demo/demo.xml',
    ],
}


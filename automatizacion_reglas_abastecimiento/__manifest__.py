# -*- coding: utf-8 -*-
{
    'name': "Automatización Reglas de Abastecimiento",

    'summary': "Módulo para la automatización de las reglas de abastecimiento",

    'description': """
    """,

    'author': "Avance Software",
    'website': "https://avancesoftware.us/",

    'category': 'Uncategorized',
    'version': '0.4',

    'depends': ['base', 'product', 'stock', 'base_automation', 'setu_intercompany_transaction', 'setu_advance_reordering'],

    'data': [
        'security/ir.model.access.csv',
        'views/views.xml',
        'views/templates.xml',
        'views/ProductView.xml',
        'views/StockView.xml',
        'views/WarehouseView.xml',
        'views/NivelesJerarquiaView.xml',
        'views/ProductTemplate.xml',
        'views/WizardView.xml',
        'data/cluster_proceso_data.xml',
        'views/cluster_proceso_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'automatizacion_reglas_abastecimiento/static/src/cluster_progress/cluster_progress.js',
            'automatizacion_reglas_abastecimiento/static/src/cluster_progress/cluster_progress.xml',
        ],
    },
    'demo': [
        'demo/demo.xml',
    ],
}


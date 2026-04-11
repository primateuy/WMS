# -*- coding: utf-8 -*-
{
    'name': "Validación de Múltiplos en Operaciones de Inventario",
    'summary': "Advertencia y bloqueo al validar operaciones que no respetan el múltiplo de distribución.",
    'description': (
        "Extensión de automatic_crossdocking. "
        "Cuando el tipo de operación tiene 'Respeta Múltiplos' activo:\n"
        "- Marca visualmente las líneas con cantidades que no respetan el múltiplo.\n"
        "- Muestra un banner de advertencia en el formulario del picking.\n"
        "- Avisa al hacer 'Marcar como por realizar'.\n"
        "- Bloquea la validación final, salvo usuarios con permiso especial."
    ),
    'author': "Avance Software",
    'website': "https://avancesoftware.us/",
    'category': 'Inventory',
    'version': '0.1',
    'depends': ['automatic_crossdocking'],
    'data': [
        'security/security.xml',
        'views/stock_picking_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

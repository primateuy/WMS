# -*- coding: utf-8 -*-
{
    'name': "Validación de Múltiplos en Operaciones de Inventario",
    'summary': "Impide validar una operación de inventario si las cantidades no respetan el múltiplo de distribución del producto.",
    'description': (
        "Extensión de automatic_crossdocking. "
        "Cuando el tipo de operación tiene 'Respeta Múltiplos' activo, "
        "bloquea la validación del picking si algún producto tiene una cantidad "
        "que no es múltiplo exacto de su 'Múltiplos de Distribución', "
        "mostrando un mensaje de error con el detalle de cada producto problemático."
    ),
    'author': "Avance Software",
    'website': "https://avancesoftware.us/",
    'category': 'Inventory',
    'version': '0.1',
    'depends': ['automatic_crossdocking'],
    'data': [
        # sin vistas adicionales: la vista del tipo de operación y el campo
        # respeta_multiplos ya los agrega automatic_crossdocking
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

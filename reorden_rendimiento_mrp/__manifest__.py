# -*- coding: utf-8 -*-
{
    'name': "Reabastecimiento: rendimiento con muchas reglas - MRP",
    'summary': "En procesos masivos de stock no busca kits movimiento por movimiento si no existe ninguno",
    'description': """
MRP busca por cada movimiento confirmado si el producto es un kit (lista de materiales
«phantom»). Sin ningún kit definido esa búsqueda nunca encuentra nada y en procesos masivos
(armado de crossdock, reposición de muchas reglas) suma decenas de miles de consultas. Con
`stock_proceso_masivo` en el contexto, una consulta previa decide: si mañana se crea un kit,
vuelve el comportamiento estándar.
    """,
    'author': "Primate",
    'category': 'Inventory',
    'version': '17.0.1.0.0',
    'depends': ['reorden_rendimiento', 'mrp'],
    'data': [],
    'auto_install': True,
    'license': 'LGPL-3',
}

# -*- coding: utf-8 -*-
{
    'name': "Crossdocking Automático - MRP",
    'summary': "Puente entre el crossdock automático y MRP: armado sin búsqueda de kits innecesaria",
    'description': """
Durante el armado de una OC de crossdock, MRP busca por cada movimiento si el producto es un
kit (lista de materiales tipo «phantom»). Sin ningún kit definido esa búsqueda nunca encuentra
nada y en una OC de 250 líneas suma decenas de miles de consultas. Este puente la saltea sólo
dentro del armado del crossdock y sólo si no existe ningún kit.
    """,
    'author': "Avance Software",
    'website': "https://avancesoftware.us/",
    'category': 'Uncategorized',
    'version': '0.1',
    'depends': ['automatic_crossdocking', 'mrp'],
    'data': [],
    'auto_install': True,
    'license': 'LGPL-3',
}

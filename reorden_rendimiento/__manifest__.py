# -*- coding: utf-8 -*-
{
    'name': "Reabastecimiento: rendimiento con muchas reglas",
    'summary': "Recálculo diferido y en lote de la cantidad a pedir de las reglas de reabastecimiento",
    'description': """
Con cientos de miles de reglas de reabastecimiento, Odoo recalcula la «cantidad a pedir»
regla por regla cada vez que cambia una línea de compra, se confirma una orden, cambia el
múltiplo o se crean reglas. En Forum eso es más del 90 % del tiempo de las operaciones
que se cortan por tiempo.

- Calcula la cantidad a pedir EN LOTE (agrupando por contexto), con el mismo resultado.
- Difiere el recálculo de las operaciones grandes a un proceso en segundo plano por tandas.
- Atajos para procesos masivos de stock: no busca reglas push ni puntos de reorden
  automáticos uno por uno cuando no existe ninguno.
    """,
    'author': "Primate",
    'category': 'Inventory',
    'version': '17.0.1.0.0',
    'depends': ['purchase_stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron.xml',
    ],
    'license': 'LGPL-3',
}

# -*- coding: utf-8 -*-
{
    'name': 'Fenicio - Integración WIS',
    'version': '17.0.1.0.0',
    'author': 'PrimateUY',
    'website': 'https://primate.uy',
    'category': 'Sales',
    'license': 'AGPL-3',
    'summary': """Integra con WIS los clientes que llegan desde órdenes de Fenicio.""",
    'description': """
Módulo puente entre odoo_fenicio e integracion_wis.

Cuando Fenicio crea u obtiene el cliente de una orden de venta, el contacto
queda marcado con integración WIS y se envía (alta o actualización) como
agente CLI. Un error de WIS no bloquea la orden: queda registrado en los
logs WIS del contacto.
    """,
    'depends': ['odoo_fenicio', 'integracion_wis'],
    'data': [],
    'auto_install': True,
    'installable': True,
    'application': False,
}

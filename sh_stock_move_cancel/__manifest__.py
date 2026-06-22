# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

{
    "name": "Cancel Stock Moves",
    "author": "Softhealer Technologies",
    "website": "https://www.softhealer.com",
    "support": "support@softhealer.com",
    "category": "Warehouse",
    "license": "OPL-1",
    "summary": """Stock moves Cancel Inventory moves Cancel Stock move cancel
    Inventory moves cancel Delete stock moves delete inventory moves remove
    inventory moves remove Stock moves delete inventory moves Odoo""",
    "description": """This module helps to cancel multiple stock moves from the tree view.""",
    "version": "0.0.1",
    "depends": [
                "stock",

    ],
    "application": True,
    "data": [
        'security/stock_move_groups.xml',
        'data/server_action_data.xml',
    ],
    "images": ["static/description/background.png", ],
    "auto_install": False,
    "installable": True,
    "price": 8,
    "currency": "EUR"
}

# Copyright 2024 PrimateUY
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

{
    "name": "Product Variant Import from Excel",
    "summary": "Importa variantes de producto desde un archivo Excel.",
    "version": "17.0.1.0.0",
    "category": "Inventory/Products",
    "author": "PrimateUY",
    "website": "https://github.com/primateuy/WMS/",
    "license": "AGPL-3",
    "depends": ["product"],
    "data": [
        "security/ir.model.access.csv",
        "views/product_variant_import_wizard_views.xml",
        "views/product_template_views.xml",
    ],
    "installable": True,
    "application": False,
}

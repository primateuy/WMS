# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import tagged

from .common import PDF_MINIMO, RemitoPrintCommon, respuesta_ucfe_ok

RUTA_REQUESTS = (
    'odoo.addons.stock_remito_print.models.stock_picking.requests.post')
RUTA_RENDER = (
    'odoo.addons.base.models.ir_actions_report.IrActionsReport._render_qweb_pdf')

CFE_RESPUESTA = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<RespBody xmlns:a="http://schemas.datacontract.org/2004/07/">'
    '<Resp><a:CodRta>00</a:CodRta><a:Serie>A</a:Serie>'
    '<a:NumeroCfe>1001</a:NumeroCfe></Resp></RespBody>'
)


@tagged('post_install', '-at_install')
class TestPrintOnlyProfile(RemitoPrintCommon):
    """Perfil «Impresión de remitos (solo)» para operadores logísticos."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.grupo_print_only = cls.env.ref(
            'stock_remito_print.group_remito_print_only')
        cls.operador = cls._crear_usuario(
            'remito.operador', cls.grupo_print_only,
            tipos_permitidos=cls.picking_type_a)

    # ------------------------------------------------------------------
    # Perfil y menús
    # ------------------------------------------------------------------

    def test_no_hereda_el_grupo_de_inventario(self):
        grupo_stock = self.env.ref('stock.group_stock_user')
        self.assertNotIn(grupo_stock, self.grupo_print_only.implied_ids)
        self.assertNotIn(grupo_stock, self.operador.groups_id)

    def test_no_ve_la_app_inventario(self):
        menu_stock = self.env.ref('stock.menu_stock_root')
        visibles = self.env['ir.ui.menu'].with_user(
            self.operador).search([('id', '=', menu_stock.id)])
        self.assertFalse(visibles)

    def test_solo_ve_el_menu_de_remitos(self):
        """El perfil no tiene que ver los menús del resto del ERP.

        Odoo solo no alcanza: los menús de usuario interno son compartidos.
        En FORUM se resuelve con la lista blanca de
        generic_security_restriction, que el módulo completa al instalarse.
        """
        if 'menu_access_only' not in self.env['res.groups']._fields:
            self.skipTest('Sin generic_security_restriction instalado')

        menu_remitos = self.env.ref(
            'stock_remito_print.menu_remito_root')
        self.assertIn(
            menu_remitos, self.grupo_print_only.menu_access_only,
            'El módulo tiene que dejar el menú Remitos en la lista blanca')

        visibles = self.env['ir.ui.menu'].with_user(self.operador).search(
            [('parent_id', '=', False)])
        self.assertEqual(
            visibles, menu_remitos,
            'El operador debería ver únicamente el menú Remitos, y ve: %s'
            % visibles.mapped('name'))

    def test_ve_el_menu_de_remitos(self):
        menu_remitos = self.env.ref(
            'stock_remito_print.menu_remito_root')
        visibles = self.env['ir.ui.menu'].with_user(
            self.operador).search([('id', '=', menu_remitos.id)])
        self.assertEqual(visibles, menu_remitos)

    def test_la_restriccion_es_obligatoria(self):
        with self.assertRaises(ValidationError):
            self.operador.write({
                'restrict_picking_types': False,
                'allowed_picking_type_ids': [(5, 0, 0)],
            })

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def test_puede_listar_sus_operaciones(self):
        visibles = self.env['stock.picking'].with_user(self.operador).search(
            [('id', 'in', (self.picking_a | self.picking_b).ids)])
        self.assertEqual(visibles, self.picking_a)

    def test_puede_leer_los_campos_de_la_lista_de_remitos(self):
        """Los campos de la vista del menú Remitos tienen que ser legibles."""
        campos = [
            'name', 'picking_type_id', 'remito_warehouse_id', 'date_done',
            'partner_id', 'remito_cfe_ref', 'ucfe_pdf_available',
            'remito_printed', 'remito_printed_date', 'remito_printed_by',
            'remito_print_count', 'state',
        ]
        datos = self.picking_a.with_user(self.operador).read(campos)
        self.assertEqual(len(datos), 1)

    # ------------------------------------------------------------------
    # Impresión
    # ------------------------------------------------------------------

    def test_puede_renderizar_el_reporte_estandar(self):
        """Exige que las ACL alcancen para todo lo que toca el reporte."""
        reporte = self.env['ir.actions.report'].with_user(self.operador)
        html, tipo = reporte._render_qweb_html(
            'stock.report_deliveryslip', self.picking_a.ids)
        self.assertEqual(tipo, 'html')
        self.assertIn(self.picking_a.name.encode(), html)

    def test_puede_imprimir_con_reporte_estandar(self):
        picking = self.picking_a.with_user(self.operador)
        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            contenido, origen, error = picking._remito_get_pdf()
        self.assertEqual(origen, 'standard')
        self.assertFalse(error)
        self.assertEqual(contenido, PDF_MINIMO)

    def test_puede_imprimir_con_pdf_de_ucfe(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self.env.company.write({
            'fe_activa': True,
            'einvoice_mode': 'testing',
            'url_testing': 'https://ucfe.example.com',
            'cfe_user': 'usuario',
            'cfe_password': 'clave',
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'url_query_value', '/Query116_2')
        self.marcar_cfe_aceptado(self.picking_a)
        self.picking_a.cfe = CFE_RESPUESTA

        picking = self.picking_a.with_user(self.operador)
        with patch(RUTA_REQUESTS, return_value=respuesta_ucfe_ok()):
            contenido, origen, error = picking._remito_get_pdf()

        self.assertEqual(origen, 'ucfe')
        self.assertFalse(error)
        self.assertEqual(contenido, PDF_MINIMO)

    def test_puede_marcar_como_impresa(self):
        picking = self.picking_a.with_user(self.operador)
        picking.action_remito_mark_printed()
        self.assertTrue(self.picking_a.remito_printed)

    def test_no_puede_marcar_lo_que_no_ve(self):
        picking = self.picking_b.with_user(self.operador)
        with self.assertRaises(AccessError):
            picking.action_remito_mark_printed()

    # ------------------------------------------------------------------
    # Lo que NO puede hacer
    # ------------------------------------------------------------------

    def test_no_puede_escribir_otros_campos(self):
        picking = self.picking_a.with_user(self.operador)
        with self.assertRaises(AccessError):
            picking.write({'origin': 'intento de edición'})

    def test_no_puede_validar(self):
        picking = self.picking_a.with_user(self.operador)
        with self.assertRaises(AccessError):
            picking.button_validate()

    def test_no_puede_crear_operaciones(self):
        with self.assertRaises(AccessError):
            self.env['stock.picking'].with_user(self.operador).create({
                'picking_type_id': self.picking_type_a.id,
                'location_id': self.location_src.id,
                'location_dest_id': self.location_dest.id,
            })

    def test_no_puede_borrar_operaciones(self):
        picking = self.picking_a.with_user(self.operador)
        with self.assertRaises(AccessError):
            picking.unlink()

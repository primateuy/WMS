# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests.common import tagged

from .common import (
    PDF_MINIMO,
    RemitoPrintCommon,
    respuesta_ucfe_ok,
    respuesta_ucfe_sin_pdf,
)

RUTA_REQUESTS = (
    'odoo.addons.stock_remito_print.models.stock_picking.requests.post')
RUTA_RENDER = (
    'odoo.addons.base.models.ir_actions_report.IrActionsReport._render_qweb_pdf')

# Respuesta de UCFE a la emisión del CFE, de donde salen serie() y
# numero_cfe() al parsear el XML guardado en el campo `cfe`.
CFE_RESPUESTA = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<RespBody xmlns:a="http://schemas.datacontract.org/2004/07/">'
    '<Resp><a:CodRta>00</a:CodRta><a:Serie>A</a:Serie>'
    '<a:NumeroCfe>1001</a:NumeroCfe></Resp></RespBody>'
)


@tagged('post_install', '-at_install')
class TestPdfSource(RemitoPrintCommon):
    """Cubre los cuatro casos de selección del PDF."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.capas['uruware']:
            cls._configurar_ucfe()

    @classmethod
    def _configurar_ucfe(cls):
        """Deja la compañía con credenciales de UCFE de mentira."""
        cls.env.company.write({
            'fe_activa': True,
            'einvoice_mode': 'testing',
            'url_testing': 'https://ucfe.example.com',
            'cfe_user': 'usuario',
            'cfe_password': 'clave',
        })
        cls.env['ir.config_parameter'].sudo().set_param(
            'url_query_value', '/Query116_2')

    def _preparar_cfe(self, picking):
        self.marcar_cfe_aceptado(picking)
        if 'cfe' in picking._fields:
            picking.cfe = CFE_RESPUESTA

    # ------------------------------------------------------------------
    # Caso 4: sin CFE -> reporte estándar
    # ------------------------------------------------------------------

    def test_sin_cfe_usa_reporte_estandar(self):
        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            contenido, origen, error = self.picking_a._remito_get_pdf()
        self.assertEqual(origen, 'standard')
        self.assertFalse(error)
        self.assertEqual(contenido, PDF_MINIMO)

    def test_reporte_estandar_configurado_por_tipo(self):
        """El reporte del tipo de operación gana sobre el de Odoo."""
        reporte = self.env.ref('stock.action_report_picking')
        self.picking_type_a.remito_report_id = reporte.id
        self.assertEqual(
            self.picking_a._remito_get_standard_report(), reporte)

        self.picking_type_a.remito_report_id = False
        self.assertEqual(
            self.picking_a._remito_get_standard_report(),
            self.env.ref('stock.action_report_delivery'))

    # ------------------------------------------------------------------
    # Caso 1: CFE aceptado con el PDF ya adjunto
    # ------------------------------------------------------------------

    def test_cfe_con_pdf_adjunto(self):
        if not self.capas['base']:
            self.skipTest('Sin l10n_uy_einvoice_base instalado')
        self._preparar_cfe(self.picking_a)
        self.adjuntar_pdf_ucfe(self.picking_a)

        self.assertTrue(self.picking_a.ucfe_pdf_available)
        # No se llama a UCFE si el PDF ya está.
        with patch(RUTA_REQUESTS, side_effect=AssertionError('no debía llamar a UCFE')):
            contenido, origen, error = self.picking_a._remito_get_pdf()
        self.assertEqual(origen, 'ucfe')
        self.assertFalse(error)
        self.assertEqual(contenido, PDF_MINIMO)

    # ------------------------------------------------------------------
    # Caso 2: CFE aceptado sin PDF adjunto -> se le pide a UCFE
    # ------------------------------------------------------------------

    def test_cfe_sin_pdf_lo_pide_a_ucfe(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self._preparar_cfe(self.picking_a)
        self.assertFalse(self.picking_a.ucfe_pdf_available)

        with patch(RUTA_REQUESTS, return_value=respuesta_ucfe_ok()) as llamada:
            contenido, origen, error = self.picking_a._remito_get_pdf()

        self.assertEqual(origen, 'ucfe')
        self.assertFalse(error)
        self.assertEqual(contenido, PDF_MINIMO)
        # El PDF queda adjunto para no volver a pedirlo.
        self.assertTrue(self.picking_a.ucfe_pdf_available)
        # Y se llamó con timeout acotado.
        self.assertIn('timeout', llamada.call_args.kwargs)
        self.assertGreater(llamada.call_args.kwargs['timeout'], 0)

    def test_no_se_duplica_el_adjunto(self):
        """Pedir el PDF dos veces no deja dos adjuntos."""
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self._preparar_cfe(self.picking_a)
        with patch(RUTA_REQUESTS, return_value=respuesta_ucfe_ok()):
            self.picking_a._remito_get_pdf()
            self.picking_a._remito_get_pdf()
        adjuntos = self.env['ir.attachment'].search([
            ('res_model', '=', 'stock.picking'),
            ('res_id', '=', self.picking_a.id),
            ('mimetype', '=', 'application/pdf'),
        ])
        self.assertEqual(len(adjuntos), 1)

    # ------------------------------------------------------------------
    # Caso 3: falla UCFE
    # ------------------------------------------------------------------

    def test_falla_ucfe_sin_fallback_es_error(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self.env['ir.config_parameter'].sudo().set_param(
            'stock_remito_print.fallback_on_ucfe_error', 'False')
        self._preparar_cfe(self.picking_a)

        with patch(RUTA_REQUESTS, return_value=respuesta_ucfe_sin_pdf()):
            contenido, origen, error = self.picking_a._remito_get_pdf()

        self.assertEqual(origen, 'error')
        self.assertFalse(contenido)
        self.assertTrue(error)

    def test_falla_ucfe_con_fallback_usa_estandar(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self.env['ir.config_parameter'].sudo().set_param(
            'stock_remito_print.fallback_on_ucfe_error', 'True')
        self._preparar_cfe(self.picking_a)
        mensajes_antes = len(self.picking_a.message_ids)

        with patch(RUTA_REQUESTS, return_value=respuesta_ucfe_sin_pdf()):
            with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
                contenido, origen, error = self.picking_a._remito_get_pdf()

        self.assertEqual(origen, 'standard_fallback')
        self.assertFalse(error)
        self.assertEqual(contenido, PDF_MINIMO)
        # Tiene que quedar constancia en el chatter.
        self.assertGreater(len(self.picking_a.message_ids), mensajes_antes)

    def test_timeout_de_ucfe_se_informa_como_error(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        import requests
        self.env['ir.config_parameter'].sudo().set_param(
            'stock_remito_print.fallback_on_ucfe_error', 'False')
        self._preparar_cfe(self.picking_a)

        with patch(RUTA_REQUESTS, side_effect=requests.Timeout('se acabó el tiempo')):
            contenido, origen, error = self.picking_a._remito_get_pdf()

        self.assertEqual(origen, 'error')
        self.assertFalse(contenido)

    # ------------------------------------------------------------------
    # Criterio de CFE aceptado
    # ------------------------------------------------------------------

    def test_cfe_rechazado_no_es_aceptado(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self._preparar_cfe(self.picking_a)
        self.picking_a.ucfe_state = '05'  # rechazado por DGI
        self.assertFalse(self.picking_a._remito_cfe_aceptado())

        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            _contenido, origen, _error = self.picking_a._remito_get_pdf()
        self.assertEqual(origen, 'standard')

    def test_cfe_esperando_dgi_es_aceptado(self):
        if not self.capas['uruware']:
            self.skipTest('Sin l10n_uy_einvoice_uruware instalado')
        self._preparar_cfe(self.picking_a)
        self.picking_a.ucfe_state = '11'
        self.assertTrue(self.picking_a._remito_cfe_aceptado())

    def test_sin_capa_de_cfe_nunca_es_aceptado(self):
        """El módulo tiene que funcionar sin facturación electrónica."""
        with patch.object(
            type(self.env['stock.picking']),
            '_remito_cfe_layers',
            return_value={'base': False, 'uruware': False},
        ):
            self.assertFalse(self.picking_a._remito_cfe_aceptado())
            self.assertFalse(self.picking_a.remito_usa_cfe)
            with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
                _contenido, origen, _error = self.picking_a._remito_get_pdf()
            self.assertEqual(origen, 'standard')

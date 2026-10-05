# -*- coding: utf-8 -*-
import base64
import json
from unittest.mock import patch

from odoo import http
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged

from .common import PDF_MINIMO, RemitoPrintSetupMixin

RUTA_RENDER = (
    'odoo.addons.base.models.ir_actions_report.IrActionsReport._render_qweb_pdf')

CLAVE_HTTP = 'remito_http_test'


@tagged('post_install', '-at_install')
class TestControllerBatch(RemitoPrintSetupMixin, HttpCase):
    """Controlador de lotes: mezcla de casos ok y error."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.usuario_http = cls.env['res.users'].with_context(
            no_reset_password=True).create({
                'name': 'Usuario HTTP remitos',
                'login': 'remito.http',
                'password': CLAVE_HTTP,
                'email': 'remito.http@example.com',
                'groups_id': [(6, 0, [
                    cls.env.ref('stock.group_stock_user').id])],
            })

    def _postear(self, picking_ids, marcar=True):
        token = http.Request.csrf_token(self)
        return self.url_open(
            '/stock_remito_print/batch',
            data={
                'picking_ids': json.dumps(picking_ids),
                'mark_printed': '1' if marcar else '0',
                'batch_index': '1',
                'batch_date': '2026-10-04',
                'csrf_token': token,
            },
        )

    def _resultados(self, respuesta):
        cabecera = respuesta.headers.get('X-Remito-Result')
        self.assertTrue(cabecera, 'Falta la cabecera X-Remito-Result')
        crudo = base64.b64decode(cabecera).decode('utf-8')
        return json.loads(crudo)['results']

    # ------------------------------------------------------------------

    def test_lote_completo_ok(self):
        self.authenticate('remito.http', CLAVE_HTTP)
        ids = [self.picking_a.id, self.picking_b.id]

        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            respuesta = self._postear(ids)

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.headers['Content-Type'], 'application/pdf')
        self.assertTrue(respuesta.content.startswith(b'%PDF'))

        resultados = self._resultados(respuesta)
        self.assertEqual(len(resultados), 2)
        self.assertTrue(all(r['source'] == 'standard' for r in resultados))

        # Las dos quedaron marcadas.
        (self.picking_a | self.picking_b).invalidate_recordset()
        self.assertTrue(self.picking_a.remito_printed)
        self.assertTrue(self.picking_b.remito_printed)
        self.assertEqual(self.picking_a.remito_last_source, 'standard')

    def test_lote_mixto_no_aborta(self):
        """Una operación que falla no tumba el lote ni marca de más."""
        self.authenticate('remito.http', CLAVE_HTTP)
        id_malo = self.picking_b.id

        def render_falso(self_report, report_ref, res_ids=None, data=None):
            if res_ids and id_malo in res_ids:
                raise UserError('El reporte de esta operación está roto')
            return (PDF_MINIMO, 'pdf')

        with patch(RUTA_RENDER, autospec=True, side_effect=render_falso):
            respuesta = self._postear([self.picking_a.id, id_malo])

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.headers['Content-Type'], 'application/pdf')

        resultados = {r['id']: r for r in self._resultados(respuesta)}
        self.assertEqual(resultados[self.picking_a.id]['source'], 'standard')
        self.assertEqual(resultados[id_malo]['source'], 'error')
        self.assertIn('roto', resultados[id_malo]['error'])

        # Solo se marca la que entró en el PDF.
        (self.picking_a | self.picking_b).invalidate_recordset()
        self.assertTrue(self.picking_a.remito_printed)
        self.assertFalse(self.picking_b.remito_printed)

    def test_lote_sin_ningun_pdf_responde_json(self):
        self.authenticate('remito.http', CLAVE_HTTP)

        with patch(RUTA_RENDER, side_effect=UserError('todo roto')):
            respuesta = self._postear([self.picking_a.id])

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('application/json', respuesta.headers['Content-Type'])
        payload = respuesta.json()
        self.assertTrue(payload['error'])
        self.assertEqual(payload['results'][0]['source'], 'error')

        self.picking_a.invalidate_recordset()
        self.assertFalse(self.picking_a.remito_printed)

    def test_no_se_marca_si_no_se_pide(self):
        self.authenticate('remito.http', CLAVE_HTTP)
        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            respuesta = self._postear([self.picking_a.id], marcar=False)

        self.assertEqual(respuesta.status_code, 200)
        self.picking_a.invalidate_recordset()
        self.assertFalse(self.picking_a.remito_printed)

    def test_las_reglas_de_registro_se_aplican(self):
        """Lo que el usuario no puede ver vuelve como error, no en el PDF."""
        self.usuario_http.write({
            'restrict_picking_types': True,
            'allowed_picking_type_ids': [(6, 0, self.picking_type_a.ids)],
        })
        self.authenticate('remito.http', CLAVE_HTTP)

        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            respuesta = self._postear([self.picking_a.id, self.picking_b.id])

        resultados = {r['id']: r for r in self._resultados(respuesta)}
        self.assertEqual(resultados[self.picking_a.id]['source'], 'standard')
        self.assertEqual(resultados[self.picking_b.id]['source'], 'error')
        self.assertIn('permiso', resultados[self.picking_b.id]['error'])

        self.picking_b.invalidate_recordset()
        self.assertFalse(self.picking_b.remito_printed)

    def test_lote_vacio(self):
        self.authenticate('remito.http', CLAVE_HTTP)
        respuesta = self._postear([])
        self.assertEqual(respuesta.status_code, 400)
        self.assertTrue(respuesta.json()['error'])

    def test_se_respeta_el_orden_de_seleccion(self):
        self.authenticate('remito.http', CLAVE_HTTP)
        ids = [self.picking_b.id, self.picking_a.id]

        with patch(RUTA_RENDER, return_value=(PDF_MINIMO, 'pdf')):
            respuesta = self._postear(ids)

        resultados = self._resultados(respuesta)
        self.assertEqual([r['id'] for r in resultados], ids)

    def test_sin_sesion_no_se_puede(self):
        """Sin sesión no se obtiene ningún PDF.

        Según el dbfilter del entorno la respuesta puede ser una redirección
        al login o un 404 (sin sesión no se resuelve la base y la ruta ni
        siquiera se registra). Lo que importa es que no salga el PDF.
        """
        respuesta = self.url_open(
            '/stock_remito_print/batch',
            data={'picking_ids': json.dumps([self.picking_a.id])},
            allow_redirects=False,
        )
        self.assertNotEqual(respuesta.status_code, 200)
        self.assertNotIn(
            'application/pdf', respuesta.headers.get('Content-Type', ''))

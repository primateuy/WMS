# -*- coding: utf-8 -*-
"""La configuración de WIS se busca una vez y queda en caché (17 búsquedas por boleta del PDV
en la prueba de volumen del 27-11-2026)."""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestConfigCache(TransactionCase):

    def _vals(self, compania, cliente):
        return {'client_id': cliente, 'empresa_id': 1, 'company_id': compania.id, 'url_access_token': 'https://x',
                'client_secret': 'x', 'apiLink': 'https://x', 'campo_stock_wis': 'stockDisponible'}

    def setUp(self):
        super().setUp()
        self.Config = self.env['integracion_wis.integracion_wis']
        self.env.registry.clear_cache()

    def test_segunda_busqueda_sin_consultas(self):
        self.Config._wis_config_id_compania(self.env.company.id)
        with self.assertQueryCount(0):
            self.Config._wis_config_id_compania(self.env.company.id)

    def test_devuelve_la_de_la_compania(self):
        config = self.Config.search([('company_id', '=', self.env.company.id)], limit=1)
        if not config:
            config = self.Config.create(self._vals(self.env.company, 'test'))
        self.assertEqual(self.Config._get_config(), config)

    def test_crear_y_borrar_limpian_la_cache(self):
        otra = self.env['res.company'].create({'name': 'Otra compañía WIS'})
        self.assertFalse(self.Config._wis_config_id_compania(otra.id))
        nueva = self.Config.create(self._vals(otra, 'otra'))
        self.assertEqual(self.Config._wis_config_id_compania(otra.id), nueva.id)
        nueva.unlink()
        self.assertFalse(self.Config._wis_config_id_compania(otra.id))

# -*- coding: utf-8 -*-
"""`detalles[].memo` en /Pedido/Create: el CB (SKU Forum) para la terminal de RF.

WIS lo pidió para que el operario vea en el colector un código que reconoce;
`codigoProducto` es el PRD-<id> interno. No golpea la red: `consultarAPI` se mockea.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

MODELO_API = 'odoo.addons.integracion_wis.models.models.IntegracionWIS'


@tagged('post_install', '-at_install', 'wis_pedido_memo')
class TestPedidoMemoWIS(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, skip_wms_integration=True, _avoid_wms=True))

        cls.config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1
        )
        if not cls.config:
            cls.config = cls.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test',
                'client_secret': 'test',
                'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api',
                'company_id': cls.env.company.id,
            })

        Producto = cls.env['product.product']
        cls.con_barcode = Producto.create({
            'name': 'Memo WIS con CB',
            'type': 'product',
            'barcode': 'TEST-MEMO-7790001',
            'default_code': 'REF-OTRA',
            'codigo_unico': 'PRD-TEST-MEMO-1',
        })
        cls.sin_barcode = Producto.create({
            'name': 'Memo WIS sin CB',
            'type': 'product',
            'default_code': 'TEST-MEMO-SKU',
            'codigo_unico': 'PRD-TEST-MEMO-2',
        })
        cls.sin_codigos = Producto.create({
            'name': 'Memo WIS sin códigos',
            'type': 'product',
            'codigo_unico': 'PRD-TEST-MEMO-3',
        })

        cls.tipo_salida = cls.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('company_id', '=', cls.env.company.id),
        ], limit=1)
        cls.origen = cls.tipo_salida.default_location_src_id
        cls.destino = cls.env.ref('stock.stock_location_customers')

    def _picking(self, productos):
        return self.env['stock.picking'].create({
            'picking_type_id': self.tipo_salida.id,
            'location_id': self.origen.id,
            'location_dest_id': self.destino.id,
            'move_ids': [(0, 0, {
                'name': p.name,
                'product_id': p.id,
                'product_uom_qty': 3,
                'product_uom': p.uom_id.id,
                'location_id': self.origen.id,
                'location_dest_id': self.destino.id,
            }) for p in productos],
        })

    def _payload(self, picking, tipo):
        with patch(f'{MODELO_API}.consultarAPI', return_value={}) as api:
            self.config.insertarPedidos(picking, tipo)
        return api.call_args.kwargs['body']

    def test_memo_prioriza_barcode_y_cae_a_referencia(self):
        self.assertEqual(self.config._wis_memo_producto(self.con_barcode), 'TEST-MEMO-7790001')
        self.assertEqual(self.config._wis_memo_producto(self.sin_barcode), 'TEST-MEMO-SKU')
        self.assertEqual(self.config._wis_memo_producto(self.sin_codigos), '')

    def test_memo_respeta_largo_maximo(self):
        largo = self.config.LARGO_MEMO_WIS
        self.con_barcode.barcode = 'X' * (largo + 50)
        self.assertEqual(len(self.config._wis_memo_producto(self.con_barcode)), largo)

    def test_pedido_normal_lleva_memo_por_linea(self):
        picking = self._picking(self.con_barcode | self.sin_barcode)
        detalles = self._payload(picking, 'NORM')['pedidos'][0]['detalles']
        memos = {d['codigoProducto']: d['memo'] for d in detalles}
        self.assertEqual(memos, {
            'PRD-TEST-MEMO-1': 'TEST-MEMO-7790001',
            'PRD-TEST-MEMO-2': 'TEST-MEMO-SKU',
        })

    def test_fint_con_cajas_lleva_memo_en_lineas_sueltas(self):
        picking = self._picking(self.con_barcode | self.sin_barcode)
        picking.action_confirm()
        paquete = self.env['stock.quant.package'].create({'name': 'CAJA-TEST-MEMO'})
        for move, pkg in zip(picking.move_ids, (paquete, self.env['stock.quant.package'])):
            self.env['stock.move.line'].create({
                'move_id': move.id,
                'picking_id': picking.id,
                'product_id': move.product_id.id,
                'product_uom_id': move.product_uom.id,
                'quantity': 3,
                'location_id': self.origen.id,
                'location_dest_id': self.destino.id,
                'result_package_id': pkg.id,
            })
        picking.invalidate_recordset(['wms_nro_caja'])
        self.assertTrue(picking.wms_nro_caja)

        pedido = self._payload(picking, 'FINT')['pedidos'][0]
        # Lo empaquetado viaja por `lpns`; la línea suelta sigue llevando memo.
        self.assertEqual(pedido['detalles'], [{
            'codigoProducto': 'PRD-TEST-MEMO-2',
            'identificador': '*',
            'cantidad': 3.0,
            'memo': 'TEST-MEMO-SKU',
        }])

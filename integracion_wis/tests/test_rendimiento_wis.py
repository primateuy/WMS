# -*- coding: utf-8 -*-
"""Lo que dejó de esperar a WIS dentro del guardado.

No golpean la red: los envíos se reemplazan por dobles.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'wis_rendimiento')
class TestRendimientoWIS(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env['integracion_wis.integracion_wis'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        if not cls.config:
            cls.config = cls.env['integracion_wis.integracion_wis'].create({
                'client_id': 'test', 'client_secret': 'test', 'empresa_id': 1,
                'url_access_token': 'https://example.invalid/token',
                'apiLink': 'https://example.invalid/api',
                'company_id': cls.env.company.id,
            })
        cls.config.write({'comunicacion_activa': True, 'cola_max_intentos': 3})
        Producto = cls.env['product.product'].with_context(_avoid_wms=True)
        cls.productos = Producto.create([
            {'name': 'Rendimiento WIS %s' % i, 'type': 'product'} for i in range(3)])
        for i, producto in enumerate(cls.productos):
            producto.write({'integracion_wms': True, 'codigo_unico': 'PRD-REND-%s' % i})
        # Sin el contexto de creación: `_avoid_wms` queda pegado al recordset.
        cls.productos = cls.env['product.product'].browse(cls.productos.ids)
        cls.almacen = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        cls.tipo = cls.env['stock.picking.type'].create({
            'name': 'Recepción WIS rendimiento', 'code': 'internal', 'sequence_code': 'TWRE',
            'warehouse_id': cls.almacen.id,
            'default_location_src_id': cls.almacen.lot_stock_id.id,
            'default_location_dest_id': cls.almacen.lot_stock_id.id,
            'integracion_wms': True, 'estado_disparo_wis': 'confirmed',
            'tipo_pedido_wis': 'REP', 'reservation_method': 'manual',
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Sucursal rendimiento WIS', 'codigo_unico_cliente': 'CLI-REND'})

    def _picking(self, productos):
        picking = self.env['stock.picking'].with_context(skip_wms_integration=True).create({
            'picking_type_id': self.tipo.id, 'partner_id': self.partner.id,
            'location_id': self.almacen.lot_stock_id.id,
            'location_dest_id': self.almacen.lot_stock_id.id,
            'move_ids': [(0, 0, {
                'name': p.name, 'product_id': p.id, 'product_uom_qty': 5,
                'product_uom': p.uom_id.id, 'location_id': self.almacen.lot_stock_id.id,
                'location_dest_id': self.almacen.lot_stock_id.id,
            }) for p in productos],
        })
        return self.env['stock.picking'].browse(picking.id)

    # --- productos ----------------------------------------------------------
    def test_varias_variantes_van_a_la_cola(self):
        Producto = type(self.env['product.product'])
        with patch.object(Producto, 'enviarWS', autospec=True) as enviar:
            self.productos.write({'weight': 2.5})
        enviar.assert_not_called()
        encoladas = self.env['wis.sync.queue'].search([
            ('product_id', 'in', self.productos.ids), ('estado', '=', 'pendiente')])
        self.assertEqual(encoladas.product_id, self.productos)

    def test_una_sola_variante_va_en_el_momento(self):
        Producto = type(self.env['product.product'])
        with patch.object(Producto, 'enviarWS', autospec=True) as enviar:
            self.productos[0].write({'weight': 2.5})
        enviar.assert_called_once()

    # --- cola de operaciones -------------------------------------------------
    def test_la_operacion_espera_a_los_productos_sin_codigo(self):
        sin_codigo = self.env['product.product'].with_context(_avoid_wms=True).create(
            {'name': 'Rendimiento WIS sin código', 'type': 'product', 'integracion_wms': True})
        self.env['wis.sync.queue']._encolar(sin_codigo, origen='oc', origen_ref='prueba')
        picking = self._picking(self.productos[:1] | sin_codigo)
        entrada = self.env['wis.picking.cola'].create({'picking_id': picking.id, 'tipo': 'REP'})
        with patch.object(type(self.env['stock.picking']), 'enviarWS', autospec=True) as enviar:
            self.assertFalse(entrada._enviar())
        enviar.assert_not_called()
        self.assertEqual(entrada.estado, 'pendiente')
        self.assertEqual(entrada.intentos, 0, "esperar no gasta intentos")
        self.assertIn('Esperando', entrada.ultimo_error)

    # --- cambios de demanda --------------------------------------------------
    def test_cambiar_varias_lineas_actualiza_una_vez_por_operacion(self):
        picking = self._picking(self.productos)
        picking.with_context(skip_wms_integration=True).write(
            {'wms_estado': 'enviado', 'codigo_unico': 'W-P-REND'})
        Config = type(self.config)
        with patch.object(Config, 'actualizarReferenciaRecepcion', autospec=True) as actualizar:
            for i, move in enumerate(picking.move_ids):
                move.product_uom_qty = 10 + i
            actualizar.assert_not_called()
            self.env.cr.flush()
        actualizar.assert_called_once()
        self.assertEqual(actualizar.call_args.args[1], picking)
        log = self.env['wms.integracion.log'].search([('picking_id', '=', picking.id)])
        self.assertEqual(log.resultado, 'exito', "el log de la actualización llega a la base")

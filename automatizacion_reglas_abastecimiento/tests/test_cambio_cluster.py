# -*- coding: utf-8 -*-
"""Cambiar una sucursal de Cluster (documento de Forum «Automatización de Rutas Productos»).

Jerarquía de la prueba, con el criterio vigente (una sucursal recibe los productos de los
Clusters de nivel `seq` mayor o igual al suyo):

    G1 (seq 1) ── W1          producto P1 → W1
    G2 (seq 2) ── W2          producto P2 → W1, W2
    G3 (seq 3) ── W3, W4      producto P3 → W1, W2, W3, W4

Pasar W4 de G3 a G1 le suma P1 y P2; volver le saca P1 y P2. P3 no cambia.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'cluster_cambio')
class TestCambioCluster(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        # La jerarquía es global: los Clusters reales de la base quedan fuera (sin nivel), si
        # no sus miles de productos entrarían en cada cambio de la prueba.
        env['stock.warehouse.group'].search([('nivel_jerarquia_id', '!=', False)]).with_context(
            cluster_moviendo=True).write({'nivel_jerarquia_id': False})
        niveles = [env['niveles.jerarquia'].create({'nombre': f'Nivel {i} prueba', 'seq': i})
                   for i in (1, 2, 3)]
        Almacen = env['stock.warehouse']
        cls.w = {i: Almacen.create({'name': f'Sucursal cambio {i}', 'code': f'SC{i}'}) for i in (1, 2, 3, 4)}
        for almacen in cls.w.values():
            almacen.lot_stock_id.write({'replenish_location': True, 'automate_reordering': True,
                                        'default_min_qty': 1, 'default_max_qty': 4})
        Grupo = env['stock.warehouse.group']
        cls.g1 = Grupo.create({'name': 'G1 cambio', 'nivel_jerarquia_id': niveles[0].id,
                               'warehouse_ids': [(6, 0, cls.w[1].ids)]})
        cls.g2 = Grupo.create({'name': 'G2 cambio', 'nivel_jerarquia_id': niveles[1].id,
                               'warehouse_ids': [(6, 0, cls.w[2].ids)]})
        cls.g3 = Grupo.create({'name': 'G3 cambio', 'nivel_jerarquia_id': niveles[2].id,
                               'warehouse_ids': [(6, 0, (cls.w[3] | cls.w[4]).ids)]})
        # La creación de los grupos deja cambios: se dan por aplicados (punto de partida).
        env['cluster.cambio.almacen'].search([]).write({'estado': 'aplicado'})

        Ruta = env['stock.route']
        cls.ruta = {i: Ruta.create({'name': f'Reabastecer cambio {i}', 'product_selectable': True,
                                    'supplied_wh_id': cls.w[i].id}) for i in cls.w}
        cls.ruta_otra = Ruta.create({'name': 'Comprar cambio', 'product_selectable': True})

        # Automatizaciones de rutas por Cluster, como las de Forum (una acción por sucursal).
        modelo = env['ir.model']._get('product.template')
        campo = env['ir.model.fields']._get('product.template', 'route_ids')
        cls.aut = {}
        for grupo in (cls.g1, cls.g2, cls.g3):
            aut = env['base.automation'].create({
                'name': f'Rutas {grupo.name}', 'model_id': modelo.id,
                'trigger': 'on_create_or_write', 'es_de_cluster': True,
                'filter_domain': "[('warehouse_group_id', 'in', [%d])]" % grupo.id})
            for i, almacen in cls.w.items():
                cluster = env['cluster.cambio.almacen']._cluster_de(almacen)
                op = 'add' if cluster.nivel_jerarquia_id.seq <= grupo.nivel_jerarquia_id.seq else 'remove'
                env['ir.actions.server'].create({
                    'name': f'Ruta {i}', 'model_id': modelo.id, 'state': 'object_write',
                    'base_automation_id': aut.id, 'usage': 'base_automation',
                    'update_path': 'route_ids', 'update_field_id': campo.id,
                    'update_m2m_operation': op, 'evaluation_type': 'value',
                    'value': str(cls.ruta[i].id), 'resource_ref': 'stock.route,%d' % cls.ruta[i].id})
            cls.aut[grupo] = aut

        Plantilla = env['product.template'].with_context(skip_auto_rules=True)
        cls.p = {}
        for n, grupo, sucursales in ((1, cls.g1, (1,)), (2, cls.g2, (1, 2)), (3, cls.g3, (1, 2, 3, 4))):
            rutas = [cls.ruta[i].id for i in sucursales] + [cls.ruta_otra.id]
            cls.p[n] = Plantilla.create({'name': f'Producto cambio {n}', 'type': 'product',
                                         'route_ids': [(6, 0, rutas)]})
            cls.p[n].with_context(skip_auto_rules=True).write({'warehouse_group_id': grupo.id})
            cls.p[n].product_variant_id.with_context(skip_auto_rules=True).write(
                {'warehouse_group_id': grupo.id})
        variantes = env['product.product'].browse([p.product_variant_id.id for p in cls.p.values()])
        env['product.product']._cluster_sincronizar_reglas(variantes)

    # --- ayudas ----------------------------------------------------------
    def _aplicar(self):
        procesos = self.env['cluster.cambio.almacen']._aplicar_pendientes()
        for proceso in procesos:
            vueltas = 0
            while proceso._avanzar():
                vueltas += 1
                self.assertLess(vueltas, 50)
        return procesos

    def _tiene_ruta(self, n, i):
        return self.ruta[i] in self.p[n].route_ids

    def _reglas_en(self, n, i):
        return self.env['stock.warehouse.orderpoint'].search_count([
            ('product_id', '=', self.p[n].product_variant_id.id), ('warehouse_id', '=', self.w[i].id)])

    def _accion(self, grupo, i):
        return self.aut[grupo].action_server_ids.filtered(lambda a: a.value == str(self.ruta[i].id))

    # --- pertenencia única ----------------------------------------------
    def test_cambiar_el_cluster_desde_la_sucursal(self):
        self.w[4].cluster_id = self.g1
        self.assertEqual(self.w[4].cluster_id, self.g1)
        self.assertNotIn(self.w[4], self.g3.warehouse_ids, 'sale del anterior')
        cambio = self.env['cluster.cambio.almacen'].search([('estado', '=', 'pendiente')])
        self.assertEqual((cambio.warehouse_id, cambio.grupo_anterior_id, cambio.grupo_nuevo_id),
                         (self.w[4], self.g3, self.g1))

    def test_agregarla_a_otro_cluster_la_saca_del_anterior(self):
        self.g2.write({'warehouse_ids': [(4, self.w[4].id)]})
        self.assertNotIn(self.w[4], self.g3.warehouse_ids)
        self.assertEqual(self.w[4].cluster_id, self.g2)

    def test_editar_grupo_de_almacen_desde_la_sucursal_tambien_mueve(self):
        """«Grupo de almacén» de la ficha es la misma relación vista desde la sucursal."""
        self.w[4].write({'warehouse_group_ids': [(4, self.g2.id)]})
        self.assertNotIn(self.w[4], self.g3.warehouse_ids)
        self.assertEqual(self.w[4].cluster_id, self.g2)
        cambio = self.env['cluster.cambio.almacen'].search([('estado', '=', 'pendiente')])
        self.assertEqual((cambio.grupo_anterior_id, cambio.grupo_nuevo_id), (self.g3, self.g2))

    def test_una_repetida_vieja_no_deja_guardar_hasta_resolverla(self):
        self.g1.with_context(cluster_moviendo=True).write({'warehouse_ids': [(4, self.w[3].id)]})
        self.assertIn(self.w[3], self.env['cluster.cambio.almacen']._sucursales_repetidas())
        with self.assertRaises(ValidationError):
            self.g1.write({'warehouse_ids': [(4, self.w[2].id)]})

    # --- actualizar ---------------------------------------------------------
    def test_pasar_a_un_cluster_mas_alto_suma_productos(self):
        self.w[4].cluster_id = self.g1
        self._aplicar()
        self.assertTrue(self._tiene_ruta(1, 4) and self._tiene_ruta(2, 4))
        self.assertTrue(self._tiene_ruta(3, 4), 'lo que ya tenía se mantiene')
        self.assertTrue(self._reglas_en(1, 4) and self._reglas_en(2, 4))
        for n in (1, 2, 3):
            self.assertIn(self.ruta_otra, self.p[n].route_ids, 'las demás rutas no se tocan')
        self.assertEqual(self._accion(self.g1, 4).update_m2m_operation, 'add')
        self.assertEqual(self._accion(self.g2, 4).update_m2m_operation, 'add')
        self.assertFalse(self.env['cluster.cambio.almacen'].search_count([('estado', '!=', 'aplicado')]))

    def test_volver_saca_lo_que_ya_no_corresponde(self):
        self.w[4].cluster_id = self.g1
        self._aplicar()
        self.w[4].cluster_id = self.g3
        self._aplicar()
        self.assertFalse(self._tiene_ruta(1, 4) or self._tiene_ruta(2, 4))
        self.assertFalse(self._reglas_en(1, 4) or self._reglas_en(2, 4))
        self.assertTrue(self._tiene_ruta(3, 4) and self._reglas_en(3, 4))
        self.assertEqual(self._accion(self.g1, 4).update_m2m_operation, 'remove')
        # Las otras sucursales, como estaban.
        self.assertTrue(self._tiene_ruta(1, 1) and self._reglas_en(2, 2))

    def test_salir_de_todos_los_clusters(self):
        self.w[4].cluster_id = False
        self._aplicar()
        self.assertFalse(self._tiene_ruta(3, 4))
        self.assertFalse(self._reglas_en(3, 4))
        self.assertEqual(self._accion(self.g3, 4).update_m2m_operation, 'remove')

    def test_aplicar_de_nuevo_no_cambia_nada(self):
        self.w[4].cluster_id = self.g1
        self._aplicar()
        rutas = {n: self.p[n].route_ids for n in self.p}
        self.assertFalse(self._aplicar(), 'sin pendientes no hay procesos')
        self.assertEqual({n: self.p[n].route_ids for n in self.p}, rutas)

    def test_varios_movimientos_valen_desde_el_primero(self):
        self.w[4].cluster_id = self.g2
        self.w[4].cluster_id = self.g1
        self._aplicar()
        self.assertTrue(self._tiene_ruta(1, 4) and self._tiene_ruta(2, 4))

    def test_actualizar_reglas_desde_otro_grupo_aplica_los_pendientes(self):
        self.w[4].cluster_id = self.g1
        asistente = self.env['stock.warehouse.group.rules.wizard'].with_context(
            default_warehouse_group_id=self.g2.id).create({'warehouse_group_id': self.g2.id})
        asistente.actualizarReglas()
        cambio = self.env['cluster.cambio.almacen'].search([('warehouse_id', '=', self.w[4].id)],
                                                           limit=1)
        self.assertEqual(cambio.estado, 'en_proceso')
        self.assertEqual(cambio.proceso_id.almacen_cambio_id, self.w[4])

    # --- alineación inicial ------------------------------------------------
    def test_alineacion_inicial_registra_las_acciones_desalineadas(self):
        self._accion(self.g3, 3).update_m2m_operation = 'remove'
        Cambio = self.env['cluster.cambio.almacen']
        creados = Cambio._registrar_alineacion_inicial().filtered(
            lambda c: c.warehouse_id in self.w.values())
        self.assertEqual(len(creados), 1)
        self.assertEqual((creados.warehouse_id, creados.grupo_producto_id, creados.operacion),
                         (self.w[3], self.g3, 'add'))
        self.p[3].route_ids = [(3, self.ruta[3].id)]
        self._aplicar()
        self.assertTrue(self._tiene_ruta(3, 3))
        self.assertEqual(self._accion(self.g3, 3).update_m2m_operation, 'add')

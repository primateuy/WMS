# -*- coding: utf-8 -*-
"""Cambio de Cluster en segundo plano: el guardado no procesa nada, el proceso
deja el mismo resultado que el camino anterior, y sólo toca lo que cambia."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


class _Revertir(Exception):
    pass


@tagged('post_install', '-at_install', 'cluster_proceso')
class TestClusterProceso(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        # La jerarquía es GLOBAL por `seq`: los Clusters reales de la base se
        # sumarían a los de la prueba. Se corren hacia arriba (la transacción
        # del test se revierte).
        for nivel in env['niveles.jerarquia'].search([]):
            nivel.seq += 1000
        # Dos niveles: el del grupo A es más alto (seq 1). Un producto del grupo
        # B (seq 2) lleva reglas de A y de B; uno del A, sólo de A.
        n1 = env['niveles.jerarquia'].create({'nombre': 'Nivel 1 prueba', 'seq': 1})
        n2 = env['niveles.jerarquia'].create({'nombre': 'Nivel 2 prueba', 'seq': 2})
        cls.almacen_a = env['stock.warehouse'].create({'name': 'Cluster A prueba', 'code': 'CAP'})
        cls.almacen_b = env['stock.warehouse'].create({'name': 'Cluster B prueba', 'code': 'CBP'})
        for almacen, (mn, mx) in ((cls.almacen_a, (3, 9)), (cls.almacen_b, (2, 5))):
            almacen.lot_stock_id.write({'replenish_location': True, 'automate_reordering': True,
                                        'default_min_qty': mn, 'default_max_qty': mx})
        cls.grupo_a = env['stock.warehouse.group'].create({
            'name': 'Grupo A prueba', 'nivel_jerarquia_id': n1.id,
            'warehouse_ids': [(6, 0, cls.almacen_a.ids)]})
        cls.grupo_b = env['stock.warehouse.group'].create({
            'name': 'Grupo B prueba', 'nivel_jerarquia_id': n2.id,
            'warehouse_ids': [(6, 0, cls.almacen_b.ids)]})
        cls.categ = env['product.category'].create({'name': 'Categoría cluster prueba'})
        env['warehouse.group.category.rule'].create({
            'warehouse_group_id': cls.grupo_b.id, 'categ_id': cls.categ.id,
            'min_qty': 7, 'max_qty': 21, 'use_multiples': True, 'qty_multiple': 3})

        cls.ruta_1 = env['stock.route'].create({'name': 'Ruta cluster 1 prueba', 'product_selectable': True})
        cls.ruta_2 = env['stock.route'].create({'name': 'Ruta cluster 2 prueba', 'product_selectable': True})
        # Automatización de rutas del grupo B: agrega la 1 y quita la 2.
        modelo = env['ir.model']._get('product.template')
        cls.automatizacion = env['base.automation'].create({
            'name': 'Rutas cluster B prueba', 'model_id': modelo.id,
            'trigger': 'on_create_or_write', 'es_de_cluster': True,
            'filter_domain': "[('warehouse_group_id', 'in', [%d])]" % cls.grupo_b.id,
        })
        campo = env['ir.model.fields']._get('product.template', 'route_ids')
        for op, ruta in (('add', cls.ruta_1), ('remove', cls.ruta_2)):
            env['ir.actions.server'].create({
                'name': 'Rutas prueba', 'model_id': modelo.id, 'state': 'object_write',
                'base_automation_id': cls.automatizacion.id, 'usage': 'base_automation',
                'update_path': 'route_ids', 'update_field_id': campo.id,
                'update_m2m_operation': op, 'evaluation_type': 'value',
                'value': str(ruta.id), 'resource_ref': 'stock.route,%d' % ruta.id,
            })

        atributo = env['product.attribute'].create({'name': 'Talle cluster prueba'})
        valores = env['product.attribute.value'].create(
            [{'name': t, 'attribute_id': atributo.id} for t in ('S', 'M', 'L')])
        cls.plantilla = env['product.template'].create({
            'name': 'Remera cluster prueba', 'type': 'product', 'categ_id': cls.categ.id,
            'route_ids': [(6, 0, cls.ruta_2.ids)],
            'attribute_line_ids': [(0, 0, {'attribute_id': atributo.id,
                                           'value_ids': [(6, 0, valores.ids)]})],
        })
        cls.variantes = cls.plantilla.product_variant_ids

    # --- ayudas ----------------------------------------------------------
    def _procesar(self, proceso):
        vueltas = 0
        while proceso._avanzar():
            vueltas += 1
            self.assertLess(vueltas, 50, "el proceso no termina")
        return proceso

    def _ultimo_proceso(self):
        return self.env['cluster.proceso'].search([], order='id desc', limit=1)

    def _reglas(self):
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT product_id, location_id, warehouse_id, product_min_qty, product_max_qty,
                   qty_multiple, trigger, active
              FROM stock_warehouse_orderpoint WHERE product_id = ANY(%s)""", (self.variantes.ids,))
        return set(self.env.cr.fetchall())

    # --- el guardado ------------------------------------------------------
    def test_el_guardado_no_procesa_nada(self):
        """🔴 Lo pedido: el cambio queda registrado y el usuario sigue."""
        with patch.object(type(self.env['stock.warehouse.orderpoint']), 'create') as crear, \
                patch.object(type(self.env['cluster.proceso']), '_encolar_cron') as cron:
            self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        crear.assert_not_called()
        cron.assert_called()
        self.assertEqual(self.variantes.mapped('warehouse_group_id'), self.grupo_b)
        proceso = self._ultimo_proceso()
        self.assertEqual(proceso.template_ids, self.plantilla)
        self.assertEqual(proceso.estado, 'en_proceso')
        self.assertEqual(self.plantilla.cluster_proceso_id, proceso)
        self.assertNotIn(self.ruta_1, self.plantilla.route_ids,
                         "las rutas no se tocan en el guardado")

    def test_las_automatizaciones_de_cluster_no_corren_al_guardar(self):
        self.plantilla.with_context(skip_auto_rules=True).write(
            {'warehouse_group_id': self.grupo_b.id})
        self.plantilla.write({'name': 'Remera cluster prueba (editada)'})
        self.assertEqual(self.plantilla.route_ids, self.ruta_2)

    # --- el proceso ---------------------------------------------------------
    def test_el_proceso_deja_rutas_y_reglas(self):
        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        proceso = self._procesar(self._ultimo_proceso())
        self.assertEqual(proceso.estado, 'terminado')
        self.assertEqual(self.plantilla.route_ids, self.ruta_1, "agrega la 1 y quita la 2")
        reglas = self._reglas()
        esperado = set()
        for v in self.variantes:
            # Grupo B: la regla de categoría manda (7/21, múltiplo 3).
            esperado.add((v.id, self.almacen_b.lot_stock_id.id, self.almacen_b.id, 7.0, 21.0, 3.0,
                          'manual', True))
            # Grupo A (nivel superior): sin regla de categoría, defaults de la ubicación.
            esperado.add((v.id, self.almacen_a.lot_stock_id.id, self.almacen_a.id, 3.0, 9.0, 1.0,
                          'manual', True))
        self.assertEqual(reglas, esperado)
        self.assertEqual(proceso.reglas_creadas, 6)
        self.assertEqual(proceso.reglas_hecho, 3)
        self.assertEqual(proceso.rutas_cambiadas, 1)

    def test_mismo_resultado_que_el_camino_anterior(self):
        """El criterio de aceptación: rutas y reglas idénticas a las de antes."""
        self.variantes.with_context(skip_auto_rules=True).write(
            {'warehouse_group_id': self.grupo_a.id})
        self.variantes.generarReglasAbastecimiento()
        foto = {}
        try:
            with self.env.cr.savepoint():
                self.plantilla.with_context(cluster_proceso_aplicando=True,
                                            skip_auto_rules=True).write(
                    {'warehouse_group_id': self.grupo_b.id})
                self.variantes.with_context(skip_auto_rules=True).write(
                    {'warehouse_group_id': self.grupo_b.id})
                self.variantes.generarReglasAbastecimiento()
                foto['anterior'] = (set(self.plantilla.route_ids.ids), self._reglas())
                raise _Revertir()
        except _Revertir:
            pass
        self.env.invalidate_all()
        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        self._procesar(self._ultimo_proceso())
        self.assertEqual((set(self.plantilla.route_ids.ids), self._reglas()), foto['anterior'])

    def test_solo_toca_lo_que_cambia(self):
        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        self._procesar(self._ultimo_proceso())
        Op = self.env['stock.warehouse.orderpoint']
        antes = Op.search([('product_id', 'in', self.variantes.ids)])
        # Mismo Cluster otra vez: nada cambia, las reglas son las mismas (ids).
        self.plantilla.generarReglasAbastecimiento()
        proceso = self._procesar(self._ultimo_proceso())
        self.assertEqual(proceso.reglas_creadas + proceso.reglas_modificadas
                         + proceso.reglas_borradas, 0)
        self.assertEqual(proceso.reglas_sin_cambio, 6)
        self.assertEqual(Op.search([('product_id', 'in', self.variantes.ids)]), antes)

    def test_al_subir_de_nivel_borra_las_que_sobran(self):
        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        self._procesar(self._ultimo_proceso())
        self.plantilla.write({'warehouse_group_id': self.grupo_a.id})
        proceso = self._procesar(self._ultimo_proceso())
        self.assertEqual(proceso.reglas_borradas, 3, "las del almacén B")
        self.assertEqual(proceso.reglas_sin_cambio, 3, "las del almacén A quedan")
        self.assertEqual({r[1] for r in self._reglas()}, {self.almacen_a.lot_stock_id.id})

    def test_una_regla_archivada_se_reactiva(self):
        """El camino anterior reventaba con IntegrityError: la restricción de
        unicidad incluye las archivadas."""
        v = self.variantes[0]
        self.env['stock.warehouse.orderpoint'].create({
            'product_id': v.id, 'location_id': self.almacen_a.lot_stock_id.id,
            'warehouse_id': self.almacen_a.id, 'product_min_qty': 1, 'product_max_qty': 1,
        }).active = False
        self.plantilla.write({'warehouse_group_id': self.grupo_a.id})
        self._procesar(self._ultimo_proceso())
        self.assertIn((v.id, self.almacen_a.lot_stock_id.id, self.almacen_a.id, 3.0, 9.0, 1.0,
                       'manual', True), self._reglas())

    def test_pocas_variantes_sueltas_en_linea_muchas_en_segundo_plano(self):
        self.variantes[:2].write({'warehouse_group_id': self.grupo_a.id})
        self.assertEqual(len(self._reglas()), 2, "dos variantes: en línea")
        with patch('odoo.addons.automatizacion_reglas_abastecimiento.models.product.'
                   'MAX_VARIANTES_EN_LINEA', 1):
            self.variantes.write({'warehouse_group_id': self.grupo_b.id})
        proceso = self._ultimo_proceso()
        self.assertEqual(proceso.variant_ids, self.variantes)
        self.assertFalse(proceso.con_rutas)

    def test_un_error_detiene_sin_reintentar(self):
        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        proceso = self._ultimo_proceso()
        Cr = type(self.env.cr)
        with patch.object(Cr, 'commit', lambda s: None), \
                patch.object(Cr, 'rollback', lambda s: None), \
                patch.object(type(proceso), '_avanzar', side_effect=ValueError('falla de prueba')):
            proceso._una_tanda()
        self.assertEqual(proceso.estado, 'detenido')
        self.assertIn('falla de prueba', proceso.error_msg)
        self.assertNotIn(proceso, self.env['cluster.proceso'].search([('estado', '=', 'en_proceso')]))

    # --- rutas derivadas del Cluster ------------------------------------------
    def _ruta_de_sucursal(self, almacen, nombre):
        return self.env['stock.route'].create({
            'name': nombre, 'product_selectable': True, 'supplied_wh_id': almacen.id})

    def test_rutas_desde_cluster(self):
        """Con el parámetro, las rutas de sucursal salen de los almacenes del Cluster.

        Las automatizaciones no corren (agregarían la 1 y quitarían la 2), las rutas que no
        son de sucursal quedan, y la de un almacén que el Cluster ya no tiene se va.
        """
        ruta_a = self._ruta_de_sucursal(self.almacen_a, 'Sucursal A prueba')
        ruta_b = self._ruta_de_sucursal(self.almacen_b, 'Sucursal B prueba')
        almacen_c = self.env['stock.warehouse'].create({'name': 'Cluster C prueba', 'code': 'CCP'})
        n3 = self.env['niveles.jerarquia'].create({'nombre': 'Nivel 3 prueba', 'seq': 3})
        self.env['stock.warehouse.group'].create({
            'name': 'Grupo C prueba', 'nivel_jerarquia_id': n3.id,
            'warehouse_ids': [(6, 0, almacen_c.ids)]})
        ruta_c = self._ruta_de_sucursal(almacen_c, 'Sucursal C prueba')
        self.plantilla.route_ids = [(6, 0, (self.ruta_2 | ruta_c).ids)]
        self.env['ir.config_parameter'].sudo().set_param(
            'automatizacion_reglas_abastecimiento.rutas_desde_cluster', 'True')

        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        proceso = self._procesar(self._ultimo_proceso())

        self.assertEqual(proceso.estado, 'terminado')
        self.assertEqual(self.plantilla.route_ids, self.ruta_2 | ruta_a | ruta_b,
                         "B lleva sus almacenes y los del nivel superior (A); C se va")
        self.assertEqual(proceso.rutas_cambiadas, 1)
        # Cada regla creada tiene la ruta de su almacén entre las del producto.
        for _p, _ub, almacen_id, *_resto in self._reglas():
            self.assertTrue(self.plantilla.route_ids.filtered(
                lambda r: r.supplied_wh_id.id == almacen_id))

    def test_rutas_por_automatizacion_sin_el_parametro(self):
        self._ruta_de_sucursal(self.almacen_b, 'Sucursal B prueba')
        self.plantilla.write({'warehouse_group_id': self.grupo_b.id})
        self._procesar(self._ultimo_proceso())
        self.assertEqual(self.plantilla.route_ids, self.ruta_1)

    # --- wizard «Actualizar Reglas» del grupo --------------------------------
    def test_wizard_del_grupo_encola_solo_los_almacenes_elegidos(self):
        # Los Clusters reales de la base quedaron con nivel más alto que los de la prueba:
        # el wizard los incluiría con todos sus productos. Se les saca el nivel.
        self.env['stock.warehouse.group'].search([
            ('id', 'not in', (self.grupo_a | self.grupo_b).ids)]).nivel_jerarquia_id = False
        self.variantes.with_context(skip_auto_rules=True).write(
            {'warehouse_group_id': self.grupo_b.id})
        wizard = self.env['stock.warehouse.group.rules.wizard'].create({
            'warehouse_group_id': self.grupo_a.id,
            'warehouse_ids': [(6, 0, self.almacen_a.ids)]})
        with patch.object(type(self.env['stock.warehouse.orderpoint']), 'create') as crear:
            wizard.actualizarReglas()
        crear.assert_not_called()
        proceso = self._ultimo_proceso()
        # Grupo B es de nivel mayor que A: sus productos llevan reglas en los almacenes de A.
        self.assertEqual(proceso.variant_ids, self.variantes)
        self.assertEqual(proceso.warehouse_ids, self.almacen_a)
        self._procesar(proceso)
        almacenes = {fila[2] for fila in self._reglas()}
        self.assertEqual(almacenes, {self.almacen_a.id}, "B no se toca: no se eligió")

    # --- variantes nuevas -----------------------------------------------------
    def test_variante_nueva_hereda_el_cluster_y_se_encola(self):
        self.plantilla.with_context(skip_auto_rules=True).write(
            {'warehouse_group_id': self.grupo_b.id})
        self.variantes.with_context(skip_auto_rules=True).write(
            {'warehouse_group_id': self.grupo_b.id})
        # En la misma transacción plantilla y variante tendrían la misma fecha de alta.
        self.env.flush_all()
        self.env.cr.execute("UPDATE product_template SET create_date = create_date - "
                            "interval '1 day' WHERE id = %s", [self.plantilla.id])
        self.plantilla.invalidate_recordset(['create_date'])
        atributo = self.plantilla.attribute_line_ids.attribute_id
        valor = self.env['product.attribute.value'].create(
            {'name': 'XL', 'attribute_id': atributo.id})
        self.plantilla.attribute_line_ids.value_ids = [(4, valor.id)]
        nueva = self.plantilla.product_variant_ids - self.variantes
        self.assertEqual(len(nueva), 1)
        self.assertEqual(nueva.warehouse_group_id, self.grupo_b)
        proceso = self._ultimo_proceso()
        self.assertEqual(proceso.variant_ids, nueva)
        self.assertFalse(proceso.con_rutas)

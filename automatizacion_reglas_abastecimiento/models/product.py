
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from odoo.fields import Command

import logging
import time
_logger = logging.getLogger(__name__);

# Variantes por debajo de las cuales un cambio de Cluster hecho sobre variantes
# sueltas se resuelve en línea (es barato); por encima, va al proceso en
# segundo plano.
MAX_VARIANTES_EN_LINEA = 10
# Variantes por sub-tanda dentro de una tanda del proceso: entre una y otra se
# mira el reloj para no pasar el tope de tiempo.
SUBTANDA = 10
# Tolerancia para comparar cantidades.
EPS = 1e-6

class ProductProduct(models.Model):
    _inherit = 'product.product'
    
    warehouse_group_id = fields.Many2one(
        'stock.warehouse.group',
        string="Grupo de Almacenes",
        help="Define a qué grupo de almacenes pertenece esta variante",
    )

    def botonListaReglas(self):
        """Regenera las reglas en segundo plano (antes: en línea, variante por variante)."""
        proceso = self.env['cluster.proceso']._encolar(
            variants=self, con_rutas=False, origen=_("Generar Reglas (variantes)"))
        return proceso._notificacion_encolado(len(self))

    def _actualizar_reglas_abastecimiento(self, almacen_id):
        """Actualiza las reglas de abastecimiento de forma optimizada para procesos masivos"""
        _logger.info("Actualizando reglas de abastecimiento para el almacén: %s", almacen_id)
        almacen = self.env['stock.warehouse'].browse(almacen_id)
    
        if not almacen:
            _logger.info("NO SE HA ENCONTRADO NINGUN ALMACEN")
            return False

        existing_rules = self.env['stock.warehouse.orderpoint'].search([
            ('product_id', '=', self.id),
            ('warehouse_id', '=', almacen.id),
            ('company_id', '=', self.env.company.id)
        ])
        if existing_rules:
            existing_rules.unlink()
            _logger.debug("Eliminadas %d reglas existentes", len(existing_rules))

        ubicaciones = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('id', 'child_of', almacen.view_location_id.id),
            ('replenish_location', '=', True),
            ('automate_reordering', '=', True)
        ])

        if not ubicaciones:
            _logger.debug("No hay ubicaciones automatizadas en almacén %s", almacen.name)
            return True

        categoriaGrupo = False
        if self.warehouse_group_id and self.warehouse_group_id.category_rule_ids:
            for cat in self.warehouse_group_id.category_rule_ids:
                if cat.categ_id.id == self.categ_id.id:
                    categoriaGrupo = cat
                    break

        reglas_data = []
        for ubicacion in ubicaciones:
            regla_data = {
                'product_id': self.id,
                'location_id': ubicacion.id,
                'warehouse_id': almacen.id,
                'product_min_qty': categoriaGrupo.min_qty if categoriaGrupo and categoriaGrupo.min_qty else ubicacion.default_min_qty,
                'product_max_qty': categoriaGrupo.max_qty if categoriaGrupo and categoriaGrupo.max_qty else ubicacion.default_max_qty,
                'qty_multiple': categoriaGrupo.qty_multiple if categoriaGrupo and categoriaGrupo.use_multiples else 1,
                'company_id': self.env.company.id
            }
            reglas_data.append(regla_data)

        if len(reglas_data) > 20:
            _logger.info("Creando %d reglas de abastecimiento para el producto %s en el almacén %s", 
                         len(reglas_data), self.display_name, almacen.name)
            self.actualizar_reglas_masivo([self.id], [almacen.id], lote_size=20);
        else:
            self.env['stock.warehouse.orderpoint'].create(reglas_data)
            _logger.debug("Creadas %d reglas para producto %s", len(reglas_data), self.display_name)

        return True

    @api.model
    def actualizar_reglas_masivo(self, product_ids, almacen_ids, lote_size=50):
        
        estadisticas = {
            'productos_procesados': 0,
            'reglas_creadas': 0,
            'errores': 0,
            'tiempo_inicio': fields.Datetime.now()
        }

        productos = self.browse(product_ids)
        total_productos = len(productos)
        
        

        try:
            for i in range(0, total_productos, lote_size):
                lote_productos = productos[i:i+lote_size]
                lote_actual = i // lote_size + 1
                total_lotes = (total_productos + lote_size - 1) // lote_size
                
                _logger.info("Procesando lote %d/%d (%d productos)", 
                           lote_actual, total_lotes, len(lote_productos))
                
                # Usar savepoint para proteger cada lote
                with self.env.cr.savepoint():
                    for producto in lote_productos:
                        for almacen_id in almacen_ids:
                            try:
                                if producto._actualizar_reglas_abastecimiento(almacen_id):
                                    estadisticas['productos_procesados'] += 1
                                else:
                                    estadisticas['errores'] += 1
                            except Exception as e:
                                _logger.error("Error procesando producto %s en almacén %s: %s", 
                                             producto.display_name, almacen_id, str(e))
                                estadisticas['errores'] += 1

                # Commit intermedio después de cada lote
                self.env.cr.commit()
                
                # Log de progreso cada 5 lotes
                if lote_actual % 5 == 0:
                    progreso = (lote_actual / total_lotes) * 100
                    _logger.info("Progreso: %.1f%% - Procesados: %d | Errores: %d", 
                               progreso, estadisticas['productos_procesados'], estadisticas['errores'])

        except Exception as e:
            _logger.error("Error crítico en procesamiento masivo: %s", str(e))
            estadisticas['errores'] += 1

        estadisticas['tiempo_fin'] = fields.Datetime.now()
        duracion = estadisticas['tiempo_fin'] - estadisticas['tiempo_inicio']
        
        _logger.info("=== PROCESAMIENTO MASIVO COMPLETADO ===")
        _logger.info("Duración: %s | Éxitos: %d | Errores: %d", 
                    duracion, estadisticas['productos_procesados'], estadisticas['errores'])
        
        return estadisticas

    def reglasDesdeWarehouseGroup(self, warehouse_id):
        for product in self:

            _logger.info("Entranod a reglas desde warehouse group");
            

            warehouse = self.env['stock.warehouse'].browse(warehouse_id);

            if not warehouse:
                continue

            

            categoriaGrupo = '';


            for group in warehouse.warehouse_group_ids:
                if group.id == product.warehouse_group_id.id:
                    for cat in group.category_rule_ids:
                        if cat.categ_id.id == product.categ_id.id:
                            categoriaGrupo = cat
                            break
            
            


            grupoActual = product.warehouse_group_id;
            ubicacionesActual = self.env['stock.location'].search([
                    ('usage', '=', 'internal'),
                    ('id', 'child_of', warehouse.view_location_id.id),
                    ('replenish_location', '=', True),
                    ('automate_reordering', '=', True)
                ]);
            
            for ub in ubicacionesActual:
                existing_rules = product.env['stock.warehouse.orderpoint'].search([
                    ('product_id', '=', product.id),
                    ('company_id', '=', product.env.company.id),
                    ('warehouse_id', '=', warehouse.id)
                ])
                if existing_rules:
                    existing_rules.unlink()

                product.env['stock.warehouse.orderpoint'].create({
                    'product_id': product.id,
                    'location_id': ub.id,
                    'warehouse_id': warehouse.id,
                    'product_min_qty': categoriaGrupo.min_qty if categoriaGrupo and categoriaGrupo.min_qty else ub.default_min_qty,
                    'product_max_qty': categoriaGrupo.max_qty if categoriaGrupo and categoriaGrupo.max_qty else ub.default_max_qty,
                    'qty_multiple': categoriaGrupo.qty_multiple if categoriaGrupo and categoriaGrupo.use_multiples else 1,
                    'company_id': product.env.company.id
                })

            restoGrupos = [];


            _logger.info("Grupos a procesar: %s", restoGrupos);

            if product.warehouse_group_id.nivel_jerarquia_id:
                grupos_jerarquia = product.env['stock.warehouse.group'].search([
                    ('nivel_jerarquia_id.seq', '<', product.warehouse_group_id.nivel_jerarquia_id.seq),
                    ('id', '!=', product.warehouse_group_id.id)
                ])
                restoGrupos += (grupos_jerarquia)

            _logger.info("Grupos a procesar: %s",restoGrupos);
            for grupo in restoGrupos:

                for alm in grupo.warehouse_ids:

                    ubicaciones_internas = product.env['stock.location'].search([
                            ('usage', '=', 'internal'),
                            ('id', 'child_of', alm.view_location_id.id),
                            ('replenish_location', '=', True),
                            ('automate_reordering', '=', True)
                        ])

                    for ubicacion in ubicaciones_internas:

                        if categoriaGrupo and categoriaGrupo.use_multiples and categoriaGrupo.qty_multiple <= 0:
                            raise ValidationError("Se encontró la categoría pero la misma tiene un múltiplo menor o igual a 0")

                        existing_rules = product.env['stock.warehouse.orderpoint'].search([
                            ('product_id', '=', product.id),
                            ('company_id', '=', product.env.company.id),
                            ('warehouse_id', '=', warehouse.id)
                        ])
                        if existing_rules:
                            existing_rules.unlink()

                        product.env['stock.warehouse.orderpoint'].create({
                            'product_id': product.id,
                            'location_id': ubicacion.id,
                            'warehouse_id': warehouse.id,
                            'product_min_qty': categoriaGrupo.min_qty if categoriaGrupo and categoriaGrupo.min_qty else ubicacion.default_min_qty,
                            'product_max_qty': categoriaGrupo.max_qty if categoriaGrupo and categoriaGrupo.max_qty else ubicacion.default_max_qty,
                            'qty_multiple': categoriaGrupo.qty_multiple if categoriaGrupo and categoriaGrupo.use_multiples else 1,
                            'company_id': product.env.company.id
                        })
            



            

    def generarReglasAbastecimiento(self):
        """
        Genera reglas de abastecimiento de forma optimizada para procesos masivos.
        Soporta procesamiento en lotes y operaciones bulk.
        """

        _logger.info(f"Iniciando generación masiva de reglas de abastecimiento para {self.warehouse_group_id} productos")

        productos_con_grupo = self.filtered(lambda p: p.warehouse_group_id)
        
        if not productos_con_grupo:
            _logger.info("No hay productos con grupo de almacenes para procesar")
            return
        
        total_productos = len(productos_con_grupo)
        _logger.info("Iniciando generación de reglas para %d productos", total_productos)
        
        # OPTIMIZACIÓN 1: Pre-cargar datos necesarios en memoria
        tiempo_inicio = fields.Datetime.now()
        
        # Obtener todos los grupos únicos involucrados
        grupos_ids = productos_con_grupo.mapped('warehouse_group_id').ids
        grupos = self.env['stock.warehouse.group'].browse(grupos_ids)
        
        # Pre-cargar niveles de jerarquía y grupos relacionados
        grupos_con_jerarquia = grupos.filtered(lambda g: g.nivel_jerarquia_id)
        max_seq = max(grupos_con_jerarquia.mapped('nivel_jerarquia_id.seq')) if grupos_con_jerarquia else 0
        
        # Cargar todos los grupos que podrían ser necesarios
        todos_grupos = self.env['stock.warehouse.group'].search([
            ('nivel_jerarquia_id.seq', '<=', max_seq)
        ]) if max_seq > 0 else grupos
        
        # Pre-cargar todas las relaciones de categorías
        categorias_por_grupo = {}
        for grupo in todos_grupos:
            categorias_por_grupo[grupo.id] = {
                cat.categ_id.id: cat 
                for cat in grupo.category_rule_ids
            }
        
        # Pre-cargar todos los almacenes y ubicaciones
        almacenes_por_grupo = {}
        ubicaciones_por_almacen = {}
        
        for grupo in todos_grupos:
            almacenes_por_grupo[grupo.id] = grupo.warehouse_ids.ids
            
            for almacen in grupo.warehouse_ids:
                if almacen.id not in ubicaciones_por_almacen:
                    ubicaciones = self.env['stock.location'].search([
                        ('usage', '=', 'internal'),
                        ('id', 'child_of', almacen.view_location_id.id),
                        ('replenish_location', '=', True),
                        ('automate_reordering', '=', True)
                    ])
                    ubicaciones_por_almacen[almacen.id] = ubicaciones
        
        _logger.info("Pre-carga completada. Eliminando reglas existentes...")
        
        # OPTIMIZACIÓN 2: Eliminar todas las reglas existentes en una sola operación
        reglas_existentes = self.env['stock.warehouse.orderpoint'].search([
            ('product_id', 'in', productos_con_grupo.ids),
            ('company_id', '=', self.env.company.id)
        ])
        
        if reglas_existentes:
            reglas_existentes.unlink()

        todas_reglas = []
        productos_procesados = 0
        errores = 0
        
        for product in productos_con_grupo:
            try:
                grupos_a_procesar = []
                
                if product.warehouse_group_id.nivel_jerarquia_id:
                    seq_producto = product.warehouse_group_id.nivel_jerarquia_id.seq
                    grupos_a_procesar = [
                        g for g in todos_grupos 
                        if g.nivel_jerarquia_id and g.nivel_jerarquia_id.seq <= seq_producto
                    ]
                else:
                    grupos_a_procesar = [product.warehouse_group_id]
                
                for grupo in grupos_a_procesar:
                    almacenes_ids = almacenes_por_grupo.get(grupo.id, [])
                    categorias_grupo = categorias_por_grupo.get(grupo.id, {})
                    categoria_producto = categorias_grupo.get(product.categ_id.id, False)
                    
                    for almacen_id in almacenes_ids:
                        ubicaciones = ubicaciones_por_almacen.get(almacen_id, [])
                        
                        if (categoria_producto and 
                            categoria_producto.use_multiples and 
                            categoria_producto.qty_multiple <= 0):
                            _logger.warning(
                                "Producto %s tiene categoría con múltiplo inválido <= 0",
                                product.display_name
                            )
                            continue
                        
                        for ubicacion in ubicaciones:
                            regla_data = {
                                'product_id': product.id,
                                'location_id': ubicacion.id,
                                'warehouse_id': almacen_id,
                                'product_min_qty': (
                                    categoria_producto.min_qty 
                                    if categoria_producto and categoria_producto.min_qty 
                                    else ubicacion.default_min_qty
                                ),
                                'product_max_qty': (
                                    categoria_producto.max_qty 
                                    if categoria_producto and categoria_producto.max_qty 
                                    else ubicacion.default_max_qty
                                ),
                                'qty_multiple': (
                                    categoria_producto.qty_multiple 
                                    if categoria_producto and categoria_producto.use_multiples 
                                    else 1
                                ),
                                'company_id': self.env.company.id
                            }
                            todas_reglas.append(regla_data)
                
                productos_procesados += 1
                
                # Log de progreso cada 100 productos
                if productos_procesados % 100 == 0:
                    _logger.info(
                        "Progreso: %d/%d productos procesados (%d reglas preparadas)",
                        productos_procesados, total_productos, len(todas_reglas)
                    )
                    
            except Exception as e:
                errores += 1
                _logger.error(
                    "Error procesando producto %s: %s",
                    product.display_name, str(e)
                )
        
        _logger.info(
            "Preparación completada: %d reglas listas para crear",
            len(todas_reglas)
        )
        
        LOTE_SIZE = 500
        reglas_creadas = 0
        
        for i in range(0, len(todas_reglas), LOTE_SIZE):
            lote = todas_reglas[i:i+LOTE_SIZE]
            try:
                self.env['stock.warehouse.orderpoint'].create(lote)
                reglas_creadas += len(lote)
                
                if i > 0 and i % (LOTE_SIZE * 5) == 0:
                    self.env.cr.commit()
                    _logger.info(
                        "Creadas %d/%d reglas (%.1f%%)",
                        reglas_creadas, len(todas_reglas),
                        (reglas_creadas / len(todas_reglas)) * 100
                    )
            except Exception as e:
                _logger.error("Error creando lote de reglas: %s", str(e))
                errores += 1
        
        tiempo_fin = fields.Datetime.now()
        duracion = tiempo_fin - tiempo_inicio
        
        _logger.info("=== GENERACIÓN DE REGLAS COMPLETADA ===")
        _logger.info("Duración: %s", duracion)
        _logger.info("Productos procesados: %d/%d", productos_procesados, total_productos)
        _logger.info("Reglas creadas: %d", reglas_creadas)
        _logger.info("Errores: %d", errores)

    @api.model_create_multi
    def create(self, vals_list):
        """La variante nueva hereda el Cluster de su plantilla y genera sus reglas.

        Antes nacía sin Cluster —el campo es propio de la variante— y por lo tanto sin
        reglas de reabastecimiento: había que reasignar el Cluster a mano. Las reglas se
        generan en segundo plano. Si la variante se crea junto con su plantilla (alta del
        producto), no se encola nada acá: lo hace el alta de la plantilla.
        """
        productos = super().create(vals_list)
        if self.env.context.get('skip_auto_rules'):
            return productos
        sin_cluster = productos.filtered(
            lambda p: not p.warehouse_group_id and p.product_tmpl_id.warehouse_group_id)
        for grupo in sin_cluster.product_tmpl_id.warehouse_group_id:
            sin_cluster.filtered(
                lambda p: p.product_tmpl_id.warehouse_group_id == grupo
            ).with_context(skip_auto_rules=True).write({'warehouse_group_id': grupo.id})
        sueltas = productos.filtered(
            lambda p: p.warehouse_group_id and p.product_tmpl_id.create_date != p.create_date)
        if sueltas:
            self.env['cluster.proceso']._encolar(
                variants=sueltas, con_rutas=False, origen=_("Variantes nuevas"))
        return productos

    def write(self, vals):
        res = super(ProductProduct, self).write(vals)

        # 🔴 `skip_auto_rules` se respeta. Antes se ignoraba, y la plantilla —que
        # escribe el Cluster en sus variantes con ese contexto y después las
        # regenera ella— terminaba regenerando cada variante TRES veces.
        if ('warehouse_group_id' in vals or 'categ_id' in vals) \
                and not self.env.context.get('skip_auto_rules'):
            if len(self) > MAX_VARIANTES_EN_LINEA:
                self.env['cluster.proceso']._encolar(
                    variants=self, con_rutas=False, origen=_("Cambio de Cluster en variantes"))
            else:
                self._cluster_sincronizar_reglas(self)

        return res;

    # ------------------------------------------------------------------
    # Reglas de abastecimiento: cálculo y sincronización optimizados
    # ------------------------------------------------------------------
    @api.model
    def _cluster_reglas_objetivo(self, variantes):
        """Las reglas que dejaría `generarReglasAbastecimiento`, sin tocar nada.

        Misma lógica, campo por campo: grupos por nivel de jerarquía, regla por
        categoría (si hay dos para la misma categoría gana la última), almacenes
        con múltiplo inválido salteados, ubicaciones internas con reposición
        automática, y la compañía de `env.company`.

        Una diferencia deliberada: los grupos se cargan como la UNIÓN de los de
        cada variante. La versión anterior los armaba para el lote entero, y si
        el lote mezclaba grupos con y sin jerarquía, los sin jerarquía quedaban
        sin reglas. Por plantilla —que es como se llamaba— daba lo mismo.

        Devuelve {product_id: [(location_id, warehouse_id, min, max, múltiplo), ...]}
        sólo para las variantes con Cluster: las que no tienen no se tocan.
        """
        con_grupo = variantes.filtered('warehouse_group_id')
        if not con_grupo:
            return {}
        Grupo = self.env['stock.warehouse.group']
        grupos = con_grupo.mapped('warehouse_group_id')
        con_jer = grupos.filtered('nivel_jerarquia_id')
        max_seq = max(con_jer.mapped('nivel_jerarquia_id.seq')) if con_jer else 0
        todos = (Grupo.search([('nivel_jerarquia_id.seq', '<=', max_seq)]) | grupos) \
            if max_seq > 0 else grupos

        categorias = {g.id: {cat.categ_id.id: cat for cat in g.category_rule_ids} for g in todos}
        almacenes = {g.id: g.warehouse_ids.ids for g in todos}
        ubicaciones = {}
        for g in todos:
            for almacen in g.warehouse_ids:
                if almacen.id not in ubicaciones:
                    ubicaciones[almacen.id] = self.env['stock.location'].search([
                        ('usage', '=', 'internal'),
                        ('id', 'child_of', almacen.view_location_id.id),
                        ('replenish_location', '=', True),
                        ('automate_reordering', '=', True)])
        ordenados = Grupo.search([('id', 'in', todos.ids)]) if max_seq > 0 else todos

        objetivo = {}
        for product in con_grupo:
            grupo_producto = product.warehouse_group_id
            if grupo_producto.nivel_jerarquia_id:
                seq = grupo_producto.nivel_jerarquia_id.seq
                a_procesar = [g for g in ordenados
                              if g.nivel_jerarquia_id and g.nivel_jerarquia_id.seq <= seq]
            else:
                a_procesar = [grupo_producto]
            reglas = []
            for grupo in a_procesar:
                cat = categorias.get(grupo.id, {}).get(product.categ_id.id, False)
                for almacen_id in almacenes.get(grupo.id, []):
                    if cat and cat.use_multiples and cat.qty_multiple <= 0:
                        continue
                    for ub in ubicaciones.get(almacen_id, []):
                        reglas.append((
                            ub.id, almacen_id,
                            cat.min_qty if cat and cat.min_qty else ub.default_min_qty,
                            cat.max_qty if cat and cat.max_qty else ub.default_max_qty,
                            cat.qty_multiple if cat and cat.use_multiples else 1,
                        ))
            objetivo[product.id] = reglas
        return objetivo

    @api.model
    def _cluster_sincronizar_reglas(self, variantes, deadline=None, almacenes=None):
        """Deja las reglas de `variantes` como las dejaría `generarReglasAbastecimiento`.

        La versión anterior BORRABA todas las reglas del producto y las volvía a
        crear. Crear una regla es lo caro —Odoo calcula en ese momento el
        pronóstico, lo que hay que pedir y los tiempos de entrega, le toma una
        secuencia y le escribe el chatter—, y al cambiar de Cluster muchas
        quedan exactamente iguales. Acá se compara contra lo que hay y sólo se
        crea, modifica o borra lo que cambia. Las nuevas nacen en `manual`, que
        es como las deja la automatización «Trigger Manual», y sin chatter.

        Resultado final: el mismo conjunto de reglas (ubicación, almacén,
        mínimo, máximo, múltiplo). Lo que NO se pierde, a diferencia de antes: el
        nombre y cualquier dato cargado a mano en las reglas que no cambian.

        Si llega `deadline` (time.time()), procesa de a sub-tandas y corta al
        pasarlo. Devuelve contadores y las variantes procesadas.

        Con `almacenes`, sólo se tocan reglas de esos almacenes: las de los demás no se
        crean, no se modifican y no se borran.
        """
        Op = self.env['stock.warehouse.orderpoint'].with_context(active_test=False)
        company_id = self.env.company.id
        cuenta = {'creadas': 0, 'modificadas': 0, 'borradas': 0, 'sin_cambio': 0,
                  'procesadas': self.browse()}
        variantes = variantes.exists()
        for i in range(0, len(variantes), SUBTANDA):
            if deadline and i and time.time() >= deadline:
                break
            lote = variantes[i:i + SUBTANDA]
            objetivo = self._cluster_reglas_objetivo(lote)
            if objetivo and almacenes:
                objetivo = {pid: [r for r in reglas if r[1] in almacenes.ids]
                            for pid, reglas in objetivo.items()}
            if objetivo:
                dominio = [('product_id', 'in', list(objetivo)), ('company_id', '=', company_id)]
                if almacenes:
                    dominio.append(('warehouse_id', 'in', almacenes.ids))
                existentes = Op.search(dominio)
                # (producto, ubicación, compañía) es único en la base, archivadas
                # incluidas: no puede haber dos por clave.
                por_clave = {(op.product_id.id, op.location_id.id): op for op in existentes}
                a_borrar = Op.browse()
                crear, usadas = [], set()
                modificar = {}
                for product_id, reglas in objetivo.items():
                    for location_id, warehouse_id, minimo, maximo, multiplo in reglas:
                        clave = (product_id, location_id)
                        if clave in usadas:
                            continue      # la misma ubicación por dos grupos: una regla
                        usadas.add(clave)
                        op = por_clave.get(clave)
                        if not op:
                            crear.append({
                                'product_id': product_id, 'location_id': location_id,
                                'warehouse_id': warehouse_id, 'product_min_qty': minimo,
                                'product_max_qty': maximo, 'qty_multiple': multiplo,
                                'company_id': company_id, 'trigger': 'manual'})
                            continue
                        vals = {}
                        if not op.active:
                            vals['active'] = True
                        if op.warehouse_id.id != warehouse_id:
                            vals['warehouse_id'] = warehouse_id
                        if abs(op.product_min_qty - minimo) > EPS:
                            vals['product_min_qty'] = minimo
                        if abs(op.product_max_qty - maximo) > EPS:
                            vals['product_max_qty'] = maximo
                        if abs(op.qty_multiple - multiplo) > EPS:
                            vals['qty_multiple'] = multiplo
                        if op.trigger != 'manual':
                            # Como la deja «Trigger Manual» al recrearla.
                            vals['trigger'] = 'manual'
                        if vals:
                            clave_vals = tuple(sorted(vals.items()))
                            modificar.setdefault(clave_vals, Op.browse())
                            modificar[clave_vals] |= op
                        else:
                            cuenta['sin_cambio'] += 1
                # Lo que existía (activo) y ya no corresponde se borra, como antes.
                a_borrar |= Op.browse([op.id for clave, op in por_clave.items()
                                       if clave not in usadas and op.active])
                ctx = dict(self.env.context, tracking_disable=True, mail_create_nolog=True,
                           mail_create_nosubscribe=True, mail_notrack=True)
                if a_borrar:
                    a_borrar.with_context(ctx).unlink()
                    cuenta['borradas'] += len(a_borrar)
                for clave_vals, ops in modificar.items():
                    ops.with_context(ctx).write(dict(clave_vals))
                    cuenta['modificadas'] += len(ops)
                if crear:
                    Op.with_context(ctx).create(crear)
                    cuenta['creadas'] += len(crear)
            cuenta['procesadas'] |= lote
        return cuenta

    def crear100Almacenes(self):
        """Crea 100 almacenes con ubicaciones preconfiguradas para automatización"""
        for i in range(1, 101):
            nombre_almacen = f'Almacén {i}'
            
            # Crear el almacén
            almacen = self.env['stock.warehouse'].create({
                'name': nombre_almacen,
                'code': f'WH{i:03}',
                'partner_id': self.env.company.partner_id.id,
            })
            
            # Configurar las ubicaciones del almacén para automatización
            self._configurar_ubicaciones_automatizacion(almacen)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Almacenes Creados',
                'message': 'Se crearon 100 almacenes con ubicaciones configuradas para automatización',
                'type': 'success',
                'sticky': False,
            }
        }

    def _configurar_ubicaciones_automatizacion(self, almacen):
        """Configura las ubicaciones del almacén para participar en automatización"""
        
        # Buscar ubicaciones internas del almacén
        ubicaciones_internas = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('id', 'child_of', almacen.view_location_id.id),
            ('id', '!=', almacen.view_location_id.id)  # Excluir la vista principal
        ])
        
        for ubicacion in ubicaciones_internas:
            # Habilitar automatización y establecer cantidades por defecto
            ubicacion.write({
                'automate_reordering': True,
                'replenish_location': True,
                'default_min_qty': 10.0,  # Cantidad mínima por defecto
                'default_max_qty': 100.0,  # Cantidad máxima por defecto
                'location_src_id': ubicacion.id,  # Puede ser otra ubicación si necesario
            })
        
        # Si no hay ubicaciones internas, crear una por defecto
        if not ubicaciones_internas:
            self.env['stock.location'].create({
                'name': f'Stock {almacen.name}',
                'location_id': almacen.lot_stock_id.id,
                'usage': 'internal',
                'automate_reordering': True,
                'replenish_location': True,
                'default_min_qty': 10.0,
                'default_max_qty': 100.0,
            })


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    warehouse_group_id = fields.Many2one(
        'stock.warehouse.group',
        string="Grupo de Almacenes",
        help="Define a qué grupo de almacenes pertenece esta variante",
    )

    def crear100Almacenes(self):
        """Crea 100 almacenes con ubicaciones preconfiguradas para automatización"""
        for i in range(1, 101):
            nombre_almacen = f'Almacén {i}'
            
            # Crear el almacén
            almacen = self.env['stock.warehouse'].create({
                'name': nombre_almacen,
                'code': f'WH{i:03}',
                'partner_id': self.env.company.partner_id.id,
            })
            
            # Configurar las ubicaciones del almacén para automatización
            self._configurar_ubicaciones_automatizacion(almacen)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Almacenes Creados',
                'message': 'Se crearon 100 almacenes con ubicaciones configuradas para automatización',
                'type': 'success',
                'sticky': False,
            }
        }

    def _configurar_ubicaciones_automatizacion(self, almacen):
        """Configura las ubicaciones del almacén para participar en automatización"""
        
        # Buscar ubicaciones internas del almacén
        ubicaciones_internas = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('id', 'child_of', almacen.view_location_id.id),
            ('id', '!=', almacen.view_location_id.id)  # Excluir la vista principal
        ])
        
        for ubicacion in ubicaciones_internas:
            # Habilitar automatización y establecer cantidades por defecto
            ubicacion.write({
                'automate_reordering': True,
                'replenish_location': True,
                'default_min_qty': 10.0,  # Cantidad mínima por defecto
                'default_max_qty': 100.0,  # Cantidad máxima por defecto
                'location_src_id': ubicacion.id,  # Puede ser otra ubicación si necesario
            })
        
        # Si no hay ubicaciones internas, crear una por defecto
        if not ubicaciones_internas:
            self.env['stock.location'].create({
                'name': f'Stock {almacen.name}',
                'location_id': almacen.lot_stock_id.id,
                'usage': 'internal',
                'automate_reordering': True,
                'replenish_location': True,
                'default_min_qty': 10.0,
                'default_max_qty': 100.0,
            })


    cluster_proceso_id = fields.Many2one(
        'cluster.proceso', string='Actualización por Cluster en curso',
        compute='_compute_cluster_proceso_id',
        help="Proceso en segundo plano que está actualizando rutas y reglas de este producto.")

    def _compute_cluster_proceso_id(self):
        Proceso = self.env['cluster.proceso'].sudo()
        for template in self:
            template.cluster_proceso_id = template.id and Proceso.search(
                [('template_ids', 'in', template.id),
                 ('estado', 'in', ('en_proceso', 'detenido'))], limit=1) or False

    def botonListaReglas(self):
        """Regenera las reglas de todas las variantes, en segundo plano."""
        proceso = self.env['cluster.proceso']._encolar(
            templates=self, con_rutas=False, origen=_("Generar Reglas"))
        return proceso._notificacion_encolado(len(self.product_variant_ids))

    def generarReglasAbastecimiento(self):
        """Botón «Actualizar Reglas» de la ficha: en segundo plano."""
        proceso = self.env['cluster.proceso']._encolar(
            templates=self, con_rutas=False, origen=_("Actualizar Reglas"))
        return proceso._notificacion_encolado(len(self.product_variant_ids))

    def write(self, vals):
        cambia_cluster = 'warehouse_group_id' in vals and not self.env.context.get('skip_auto_rules')
        res = super(ProductTemplate, self).write(vals)
        if cambia_cluster:
            self._cluster_encolar(_("Cambio de Cluster"))
        return res

    @api.model_create_multi
    def create(self, vals_list):
        templates = super(ProductTemplate, self).create(vals_list)
        con_cluster = templates.filtered('warehouse_group_id')
        if con_cluster and not self.env.context.get('skip_auto_rules'):
            con_cluster._cluster_encolar(_("Producto nuevo con Cluster"))
        return templates

    def _cluster_encolar(self, origen):
        """Pasa el Cluster a las variantes SIN regenerar y encola el resto.

        Lo único que queda en el guardado es copiar el Cluster a las variantes
        (una escritura por plantilla). Rutas y reglas de abastecimiento van al
        proceso en segundo plano.
        """
        for template in self:
            template.product_variant_ids.with_context(skip_auto_rules=True).write(
                {'warehouse_group_id': template.warehouse_group_id.id})
        proceso = self.env['cluster.proceso']._encolar(templates=self, origen=origen)
        if proceso and len(self) <= 20:
            for template in self:
                template.message_post(body=_(
                    "Cluster: %(c)s. Las rutas y las reglas de abastecimiento de sus "
                    "%(n)d variante(s) se actualizan en segundo plano (%(p)s).",
                    c=template.warehouse_group_id.display_name or _('sin Cluster'),
                    n=len(template.product_variant_ids), p=proceso.name))
        return proceso

    @api.model
    def _cluster_rutas_contexto(self):
        """Lo necesario para derivar las rutas del Cluster, leído una vez por tanda.

        La ruta que abastece una sucursal ya la marca Odoo: es la de reabastecimiento entre
        almacenes, con `supplied_wh_id` = la sucursal. Se gestionan sólo las de almacenes
        que están en algún Cluster; cualquier otra ruta del producto (Comprar, Fabricar,
        cross dock, una sucursal fuera de los Clusters) queda como está.
        """
        grupos = self.env['stock.warehouse.group'].search([])
        rutas = self.env['stock.route'].search([
            ('supplied_wh_id', 'in', grupos.mapped('warehouse_ids').ids),
            ('product_selectable', '=', True)])
        por_almacen = {}
        for ruta in rutas:
            por_almacen.setdefault(ruta.supplied_wh_id.id, set()).add(ruta.id)
        return {'grupos': grupos, 'gestionadas': set(rutas.ids), 'por_almacen': por_almacen}

    def _cluster_rutas_objetivo(self, contexto):
        """Las rutas de sucursal que le corresponden por su Cluster, o None si no tiene.

        Misma jerarquía que las reglas de abastecimiento (`_cluster_reglas_objetivo`): con
        nivel de jerarquía, los almacenes de todos los Clusters de nivel menor o igual; sin
        nivel, los de su Cluster. Así cada regla que el proceso crea tiene su ruta.
        """
        self.ensure_one()
        grupo = self.warehouse_group_id
        if not grupo:
            return None
        if grupo.nivel_jerarquia_id:
            seq = grupo.nivel_jerarquia_id.seq
            grupos = [g for g in contexto['grupos']
                      if g.nivel_jerarquia_id and g.nivel_jerarquia_id.seq <= seq]
        else:
            grupos = [grupo]
        objetivo = set()
        for g in grupos:
            for almacen in g.warehouse_ids:
                objetivo |= contexto['por_almacen'].get(almacen.id, set())
        return objetivo

    def _cluster_aplicar_rutas_desde_cluster(self, contexto):
        """Deja las rutas de sucursal iguales a los almacenes de su Cluster. True si cambió."""
        self.ensure_one()
        objetivo = self._cluster_rutas_objetivo(contexto)
        if objetivo is None:
            return False
        antes = set(self.route_ids.ids)
        rutas = (antes - contexto['gestionadas']) | objetivo
        if rutas != antes:
            self.write({'route_ids': [Command.set(sorted(rutas))]})
        return rutas != antes

    def _cluster_aplicar_rutas(self, automatizaciones):
        """Aplica las automatizaciones de rutas de su Cluster, escribiendo UNA vez.

        Mismo resultado que dejar correr cada automatización: se evalúa su
        dominio contra la plantilla y sus acciones (agregar / quitar / fijar /
        vaciar una ruta) se aplican en memoria, en el orden en que Odoo las
        ejecuta. Si alguna acción no es una escritura simple de rutas, esa
        automatización se corre como siempre. Devuelve True si cambió algo.
        """
        from .cluster_proceso import CTX_APLICANDO
        self.ensure_one()
        antes = set(self.route_ids.ids)
        rutas = set(antes)
        for automatizacion in automatizaciones:
            aplicada = automatizacion.with_context(**{CTX_APLICANDO: True})
            if not aplicada._filter_post(self):
                continue
            operaciones = automatizacion._cluster_operaciones_rutas()
            if operaciones is None:
                self.env.flush_all()
                if rutas != set(self.route_ids.ids):
                    self.write({'route_ids': [Command.set(sorted(rutas))]})
                aplicada._process(self.with_context(**{CTX_APLICANDO: True}))
                self.invalidate_recordset(['route_ids'])
                rutas = set(self.route_ids.ids)
                continue
            for operacion, ruta_id in operaciones:
                if operacion == 'add':
                    rutas.add(ruta_id)
                elif operacion == 'remove':
                    rutas.discard(ruta_id)
                elif operacion == 'set':
                    rutas = {ruta_id}
                else:
                    rutas = set()
        if rutas != set(self.route_ids.ids):
            self.write({'route_ids': [Command.set(sorted(rutas))]})
        return rutas != antes

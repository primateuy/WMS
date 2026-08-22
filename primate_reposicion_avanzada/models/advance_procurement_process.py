# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from dateutil import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

from .distribution_strategy_mixin import DISTRIBUTION_STRATEGIES

_logger = logging.getLogger(__name__)

# Ventana fija para la estrategia por rotación. Se deja como constante en esta versión:
# hacerla configurable por proceso suma complejidad sin necesidad confirmada.
ROTACION_DIAS = 90


class AdvanceProcurementProcess(models.Model):
    # _name explicito: con _inherit como lista, Odoo toma el nombre de la clase si no
    # se declara, y falla al inicializar el modelo.
    _name = 'advance.procurement.process'
    _inherit = ['advance.procurement.process', 'primate.distribution.strategy.mixin']

    product_category_ids = fields.Many2many(
        'product.category',
        'primate_procurement_process_categ_rel',
        'procurement_process_id',
        'categ_id',
        string='Categorías de Producto',
        help='Categorías a incluir en la carga asistida. Se incluyen las subcategorías.',
    )
    warehouse_group_ids = fields.Many2many(
        'stock.warehouse.group',
        'primate_procurement_process_wh_group_rel',
        'procurement_process_id',
        'warehouse_group_id',
        string='Grupos de Almacenes',
        help='Grupos a abastecer. Se usan tanto para filtrar los productos (deben tener el '
             'grupo asignado) como para derivar los almacenes destino.',
    )
    distribution_strategy = fields.Selection(
        selection=DISTRIBUTION_STRATEGIES,
        string='Estrategia de Distribución',
        help='Cómo repartir el stock del almacén origen entre los productos que quedaron a '
             'definir criterio. Se aplica cuando se presiona "Aplicar criterio", no al '
             'confirmar: el sistema no reparte nada por su cuenta. También se puede cargar '
             'la cantidad a mano en cada línea, sin elegir estrategia.',
    )
    strategy_warehouse_ids = fields.Many2many(
        'stock.warehouse',
        'primate_procurement_process_strategy_wh_rel',
        'procurement_process_id',
        'warehouse_id',
        string='Prioridad de Almacenes',
        help='Almacenes en orden de prioridad. El orden efectivo es el del campo "Secuencia" '
             'de cada almacén; los almacenes con demanda que no figuren acá se atienden al final.',
    )
    multiplo_rounding_method = fields.Selection(
        [('ceil', 'Hacia arriba'),
         ('floor', 'Hacia abajo')],
        string='Redondeo al Múltiplo',
        default='ceil',
        help='Hacia arriba asegura cubrir la necesidad. Hacia abajo evita pasarse cuando el '
             'stock es ajustado.',
    )

    # Contadores para ver de un vistazo la composición de las líneas, sin ocultar ninguna.
    count_executable = fields.Integer('Ejecutables', compute='_compute_primate_counts')
    count_partial = fields.Integer('A definir criterio', compute='_compute_primate_counts')
    count_impossible = fields.Integer('Sin ejecución', compute='_compute_primate_counts')
    count_no_demand = fields.Integer('Sin demanda', compute='_compute_primate_counts')
    count_mismatch = fields.Integer('Fuera de grupo', compute='_compute_primate_counts')

    @api.depends('line_ids.execution_status', 'line_ids.warehouse_group_mismatch',
                 'line_ids.qty_adjusted')
    def _compute_primate_counts(self):
        for proceso in self:
            lineas = proceso.line_ids
            propias = lineas.filtered(lambda l: l.warehouse_id != proceso.warehouse_id)
            proceso.count_executable = len(propias.filtered(
                lambda l: l.execution_status == 'executable'))
            proceso.count_partial = len(propias.filtered(
                lambda l: l.execution_status == 'partial'))
            proceso.count_impossible = len(propias.filtered(
                lambda l: l.execution_status == 'impossible'))
            proceso.count_mismatch = len(propias.filtered('warehouse_group_mismatch'))
            proceso.count_no_demand = len(propias.filtered(
                lambda l: not l.warehouse_group_mismatch and not l.execution_status))

    def action_open_lines(self):
        """Abre las líneas en una vista propia, donde sí se puede buscar, filtrar y agrupar.

        La lista embebida en el formulario no tiene barra de búsqueda (es una limitación de
        Odoo para los One2many), así que los filtros por estado viven acá.
        """
        self.ensure_one()
        return {
            'name': _('Líneas de %s') % (self.name or self.id),
            'type': 'ir.actions.act_window',
            'res_model': 'advance.procurement.process.line',
            'view_mode': 'tree,form',
            'domain': [('procurement_process_id', '=', self.id)],
            'context': {'search_default_filtro_a_definir': 1},
        }

    def action_create_process_from_pending(self):
        """Crea un proceso nuevo con las líneas que quedaron sin resolver.

        Sirve para cerrar el proceso actual con lo que sí se cumplió y seguir trabajando lo
        pendiente aparte, en vez de arrastrarlo todo junto.
        """
        self.ensure_one()
        pendientes = self.line_ids.filtered(
            lambda l: l.warehouse_id != self.warehouse_id
            and not l.warehouse_group_mismatch
            and l.execution_status in ('partial', 'impossible')
            and l.qty_distributable <= 0
        )
        if not pendientes:
            raise UserError(_(
                'No hay líneas pendientes: todas están resueltas, sin demanda o fuera de grupo.'))

        productos = pendientes.mapped('product_id')
        almacenes = pendientes.mapped('warehouse_id')
        nuevo = self.copy({
            'name': False,
            'state': 'draft',
            'procurement_date': fields.Datetime.now(),
            'product_ids': [(6, 0, productos.ids)],
            'line_ids': [(5, 0, 0)],
            'summary_ids': [(5, 0, 0)],
            'config_ids': [(5, 0, 0)],
            'distribution_strategy': False,
        })
        procurement_date = nuevo.procurement_date.date()
        advance_stock_date = procurement_date + relativedelta.relativedelta(days=1)
        nuevo.config_ids = [
            (0, 0, {
                'warehouse_id': almacen.id,
                'shipment_date': procurement_date,
                'shipment_arrival_date': procurement_date,
                'advance_stock_start_date': advance_stock_date,
                'advance_stock_end_date': advance_stock_date,
            })
            for almacen in (self.warehouse_id | almacenes)
        ]
        for config in nuevo.config_ids:
            config.onchange_warehouse_id()
            config.onchange_transit_days()
            config.onchange_shipment_arrival_date()

        self.message_post(body=_(
            'Se derivaron %(prod)s producto(s) y %(alm)s almacén(es) sin resolver al proceso '
            '%(nuevo)s.', prod=len(productos), alm=len(almacenes), nuevo=nuevo.name or nuevo.id))
        nuevo.message_post(body=_(
            'Proceso creado a partir de las líneas sin resolver de %s.') % (self.name or self.id))
        return {
            'name': _('Reposición'),
            'type': 'ir.actions.act_window',
            'res_model': 'advance.procurement.process',
            'res_id': nuevo.id,
            'view_mode': 'form',
        }

    # ------------------------------------------------------------------
    # Carga asistida
    # ------------------------------------------------------------------

    def action_load_products_by_category_and_group(self):
        """Carga productos que cumplan categoría Y grupo de almacenes (regla 5.1).

        La carga es aditiva: no reemplaza lo que el usuario haya puesto a mano en product_ids.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_('Solo se pueden cargar productos mientras el proceso está en borrador.'))
        if not self.product_category_ids and not self.warehouse_group_ids:
            raise UserError(_('Elegí al menos una categoría o un grupo de almacenes.'))

        candidatos = self.env['product.product'].primate_products_from_categories_and_groups(
            self.product_category_ids,
            self.warehouse_group_ids,
            extra_domain=[('type', '=', 'product')],
        )
        nuevos = candidatos - self.product_ids
        if not nuevos:
            raise UserError(_(
                'La combinación de categorías y grupos elegida no aporta productos nuevos. '
                'Recordá que el producto tiene que tener el grupo de almacenes asignado para '
                'entrar por esta vía.'))
        self.product_ids = [(4, product.id) for product in nuevos]
        self.message_post(body=_('Carga asistida: se agregaron %s productos.') % len(nuevos))
        # Sin acción de retorno para que el cliente web recargue el registro y los productos
        # aparezcan en el momento. Devolver una notificación no refresca la vista.
        return None

    def action_add_warehouses_from_groups(self):
        """Expande los grupos elegidos en líneas de config_ids, una por almacén destino.

        - Saltea el almacén origen: onchange_procurement_warehouse_id ya lo carga como
          primera línea y action_procurement_internal_transfer lo excluye de las
          transferencias. Duplicarlo dispararía el @api.constrains('config_ids') de Setu.
        - Saltea los almacenes ya cargados, por el mismo constraint.
        - Valida que el almacén tenga un canal inter-almacén activo; si no lo tiene, lo
          excluye y lo informa, sin cortar el resto (regla 5.2).
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_('Solo se pueden agregar almacenes mientras el proceso está en borrador.'))
        if not self.warehouse_group_ids:
            raise UserError(_('Elegí al menos un grupo de almacenes.'))
        if not self.warehouse_id:
            raise UserError(_('Definí primero el almacén de origen del proceso.'))

        almacenes_grupo = self.warehouse_group_ids.mapped('warehouse_ids')
        ya_cargados = self.config_ids.mapped('warehouse_id')
        candidatos = almacenes_grupo - ya_cargados - self.warehouse_id

        canal_obj = self.env['setu.interwarehouse.channel']
        sin_canal = self.env['stock.warehouse']
        con_canal = self.env['stock.warehouse']
        for almacen in candidatos:
            canal = canal_obj.search([
                ('requestor_warehouse_id', '=', almacen.id),
                ('active', '=', True),
            ], limit=1)
            if canal:
                con_canal |= almacen
            else:
                sin_canal |= almacen

        if not con_canal:
            raise UserError(_(
                'Ninguno de los almacenes pendientes tiene canal inter-almacén activo: %s')
                % (', '.join(sin_canal.mapped('display_name')) or _('no quedan candidatos')))

        procurement_date = self.procurement_date.date()
        advance_stock_date = procurement_date + relativedelta.relativedelta(days=1)
        self.config_ids = [
            (0, 0, {
                'warehouse_id': almacen.id,
                'shipment_date': procurement_date,
                'shipment_arrival_date': procurement_date,
                'advance_stock_start_date': advance_stock_date,
                'advance_stock_end_date': advance_stock_date,
            })
            for almacen in con_canal
        ]

        # Resolver canal y plazos de las líneas nuevas, igual que create_warehouse_replenishment.
        nuevas = self.config_ids.filtered(lambda c: c.warehouse_id in con_canal)
        for config in nuevas:
            config.onchange_warehouse_id()
            config.onchange_transit_days()
            config.onchange_shipment_arrival_date()

        if sin_canal:
            self.message_post(body=_(
                'Carga asistida: se agregaron %(ok)s almacenes destino. Quedaron afuera por '
                'no tener canal inter-almacén activo: %(ko)s',
                ok=len(con_canal), ko=', '.join(sin_canal.mapped('display_name'))))
        else:
            self.message_post(body=_(
                'Carga asistida: se agregaron %s almacenes destino.') % len(con_canal))
        # Sin acción de retorno, para que el cliente web recargue y las líneas se vean.
        return None

    # ------------------------------------------------------------------
    # Cálculo: múltiplo, factibilidad y distribución
    # ------------------------------------------------------------------

    def get_sales_data(self, config):
        """Garantiza una fila por producto del proceso, aunque no tenga ventas en el período.

        El motor de Setu arma las líneas a partir del resultado de la consulta de ventas
        (get_products_sales_warehouse_group_wise). Los productos sin ventas en la ventana no
        vuelven en esa consulta, así que nunca llegan a tener línea: el cálculo no se hace y,
        si ningún producto tuvo ventas, el proceso queda en "Incapaz de Reponer" sin una sola
        línea que explique por qué.

        Completar los faltantes con ceros hace que prepare_reorder_line_vals corra para todos
        con la misma fórmula del vendor: el producto queda con su línea, su stock y su
        demanda (cero si no hay rotación), visible en la pantalla en vez de desaparecido.
        """
        sales_data = super().get_sales_data(config) or []
        con_datos = {fila.get('product_id') for fila in sales_data}
        faltantes = self.product_ids.filtered(lambda p: p.id not in con_datos)
        if not faltantes:
            return sales_data

        for product in faltantes:
            fila = {'product_id': product.id, 'product_name': product.display_name, 'ads': 0.0}
            if self.generate_demand_with != 'history_sales':
                # Claves que prepare_reorder_line_vals lee en el modo pronóstico.
                fila.update({'lead_days_demand_stock': 0.0, 'expected_sales_stock': 0.0})
            sales_data.append(fila)

        _logger.info(
            "Reposición %s / almacén %s: %s producto(s) sin ventas en el período, se les "
            "arma línea con demanda cero para no cortar el proceso.",
            self.name or self.id, config.warehouse_id.display_name, len(faltantes))
        return sales_data

    def action_procurement_confirm(self):
        """Después del cálculo de Setu, aplica múltiplo, factibilidad y estrategia.

        También rescata el proceso del estado "Incapaz de Reponer": el base lo aplica cuando
        prepare_reorder_line_vals no devuelve tuplas nuevas, lo que pasa tanto cuando no hay
        demanda como cuando todas las líneas ya existían y se actualizaron en el lugar. Si
        hay líneas, el proceso tiene que seguir y mostrar qué se puede y qué no, en vez de
        cortarse ahí.
        """
        res = super().action_procurement_confirm()
        self.primate_process_lines()
        for proceso in self:
            if proceso.state == 'unable_to_replenish' and proceso.line_ids:
                proceso.state = 'inprogress'
                sin_demanda = len(proceso.line_ids.filtered(lambda l: l.qty_adjusted <= 0))
                proceso.message_post(body=_(
                    'El cálculo se completó con %(total)s línea(s), de las cuales %(cero)s '
                    'quedaron sin demanda. El proceso sigue en curso para que se pueda '
                    'revisar el detalle.',
                    total=len(proceso.line_ids), cero=sin_demanda))
        return res

    def action_apply_distribution_strategy(self):
        """Aplica el criterio elegido sobre las líneas que quedaron a definir.

        Se llama a mano desde el botón: al confirmar no se reparte nada de lo que está en
        falta, justamente para que la decisión sea del usuario.
        """
        self.ensure_one()
        if self.state in ('done', 'cancel'):
            raise UserError(_('El proceso ya está cerrado: no se puede aplicar un criterio.'))
        if not self.distribution_strategy:
            raise UserError(_(
                'Elegí una estrategia de distribución antes de aplicar el criterio, o cargá '
                'las cantidades a mano en las líneas que quedaron a definir.'))
        self.primate_process_lines()
        self.message_post(body=_(
            'Se recalculó el reparto con la estrategia "%s".')
            % dict(self._fields['distribution_strategy'].selection).get(self.distribution_strategy))
        # Sin acción de retorno, para que el cliente web recargue y se vea el nuevo reparto.
        return None

    def primate_process_lines(self):
        """Marca fuera de grupo, ajusta al múltiplo, clasifica y reparte.

        La escasez se evalúa **agregada por producto**: se compara la suma de la demanda de
        todos los almacenes destino contra el stock que el origen tiene libre para repartir.
        Evaluar cada línea por separado contra el stock total del origen daría todas las
        líneas como ejecutables y generaría transferencias que suman más que el stock.
        """
        for proceso in self:
            proceso._primate_marcar_fuera_de_grupo()
            proceso._primate_ajustar_al_multiplo()
            for product in proceso.line_ids.mapped('product_id'):
                proceso._primate_procesar_producto(product)
        return True

    def _primate_marcar_fuera_de_grupo(self):
        """Marca las líneas cuyo almacén destino no pertenece al grupo del producto (5.3).

        Se ponen las cantidades en cero para que el resumen y la generación de
        transferencias de Setu las ignoren sin necesidad de tocar esos métodos.
        """
        self.ensure_one()
        for linea in self.line_ids:
            grupo_producto = linea.product_id.warehouse_group_id
            if linea.warehouse_id == self.warehouse_id:
                # El almacén origen no se valida contra el grupo: no recibe, entrega.
                linea.warehouse_group_mismatch = False
                continue
            compatible = bool(grupo_producto) and grupo_producto in linea.warehouse_id.warehouse_group_ids
            linea.warehouse_group_mismatch = not compatible
            if not compatible:
                linea.write({
                    'qty_adjusted': 0.0,
                    'qty_distributable': 0.0,
                    'demand_adjustment_qty': 0,
                    'execution_status': False,
                })

    def _primate_ajustar_al_multiplo(self):
        """Copia el múltiplo del producto y ajusta la demanda calculada por Setu (5.4)."""
        self.ensure_one()
        metodo = self.multiplo_rounding_method or 'ceil'
        for linea in self.line_ids.filtered(lambda l: not l.warehouse_group_mismatch):
            multiple = linea.product_id.mutiplos_distribucion or 1
            ajustada = linea.product_id.primate_round_to_multiple(
                linea.demand_adjustment_qty, rounding_method=metodo)
            linea.write({
                'multiplo_distribucion': multiple,
                'qty_adjusted': ajustada,
                'demand_adjustment_qty': ajustada,
            })

    def _primate_stock_origen(self, product, lineas_producto):
        """Stock del origen libre para repartir, ya descontada su propia demanda.

        Replica el criterio de prepare_reorder_summary_vals de Setu: el almacén origen se
        reserva primero lo que necesita para sí mismo y solo el excedente se distribuye.
        """
        self.ensure_one()
        linea_origen = lineas_producto.filtered(lambda l: l.warehouse_id == self.warehouse_id)
        if linea_origen:
            stock_bruto = sum(linea_origen.filtered(lambda l: l.available_stock > 0).mapped('available_stock'))
        else:
            ctx = product.with_context(warehouse=self.warehouse_id.id)
            stock_bruto = ctx.qty_available - ctx.outgoing_qty
        demanda_propia = sum(linea_origen.mapped('qty_adjusted'))
        return max(stock_bruto - demanda_propia, 0.0)

    def _primate_procesar_producto(self, product):
        """Clasifica y reparte las líneas de un producto."""
        self.ensure_one()
        lineas_producto = self.line_ids.filtered(
            lambda l: l.product_id == product and not l.warehouse_group_mismatch)
        destinos = lineas_producto.filtered(
            lambda l: l.warehouse_id != self.warehouse_id and l.qty_adjusted > 0)
        if not destinos:
            lineas_producto.write({'execution_status': False, 'qty_distributable': 0.0})
            return

        multiple = product.mutiplos_distribucion or 1
        rounding = product.uom_id.rounding or 0.01
        stock = self._primate_stock_origen(product, lineas_producto)
        destinos.write({'qty_available_fulfiller': stock})

        demanda_total = sum(destinos.mapped('qty_adjusted'))

        # Sin escasez: cada destino recibe su demanda completa y la estrategia no interviene.
        if float_compare(demanda_total, stock, precision_rounding=rounding) <= 0:
            for linea in destinos:
                linea.write({'execution_status': 'executable',
                             'qty_distributable': linea.qty_adjusted})
            return

        # Escasez agregada: si no entra ni un múltiplo, nada es ejecutable.
        if float_compare(stock, multiple, precision_rounding=rounding) < 0:
            destinos.write({'execution_status': 'impossible', 'qty_distributable': 0.0})
            return

        # Escasez del producto: TODAS sus líneas quedan a definir criterio, ninguna se
        # cumple al 100% por su cuenta. Elegir cuál de los destinos se cubre entero y cuál
        # se recorta es precisamente la decisión que toma el usuario, así que el sistema no
        # la anticipa: deja las cantidades en cero hasta que se aplique un criterio o se
        # carguen a mano.
        destinos.write({'execution_status': 'partial'})
        if not self.distribution_strategy:
            destinos.write({'qty_distributable': 0.0})
            return
        asignaciones = self._primate_distribuir(product, destinos, stock, multiple)
        for linea in destinos:
            linea.qty_distributable = asignaciones.get(linea.id, 0.0)

    # ------------------------------------------------------------------
    # Estrategias de distribución (5.6) — delegadas al mixin (5.9)
    # ------------------------------------------------------------------

    def _primate_distribuir(self, product, lineas, stock, multiple):
        """Arma los candidatos y delega el reparto en el motor reusable.

        La aritmética de las 5 estrategias vive en primate.distribution.strategy.mixin para
        que otros módulos (por ejemplo forum_reorder_origin, sobre reglas de
        reabastecimiento) puedan reusarla sin acoplarse a este modelo.
        """
        self.ensure_one()
        candidatos = [
            {
                'key': linea.id,
                'demand_qty': linea.qty_adjusted,
                'ads': self._primate_ads_por_almacen(product, linea.warehouse_id),
                'sequence': linea.warehouse_id.id,
            }
            for linea in lineas
        ]
        prioridad = [
            linea.id
            for wh in self.strategy_warehouse_ids
            for linea in lineas.filtered(lambda l: l.warehouse_id == wh)
        ]
        return self.compute_partial_distribution(
            self.distribution_strategy or 'proporcional',
            candidatos,
            stock,
            multiple,
            rounding_method=self.multiplo_rounding_method or 'ceil',
            priority_ids=prioridad,
            # Paso de reparto tomado del redondeo de la unidad de medida: para "Units" es 1
            # y el reparto queda en unidades enteras. Sin esto el mixin usa su default de
            # 0,01 y asigna cantidades como 0,28 en productos que se manejan por unidad.
            precision_rounding=product.uom_id.rounding or 1.0,
        )

    def _primate_ads_por_almacen(self, product, warehouse):
        """Venta diaria promedio del producto en un almacén, para la estrategia por rotación.

        Ventana fija de ROTACION_DIAS días sobre product.sales.history. Sin historial
        devuelve 0, y el mixin se encarga de caer al orden de prioridad si ningún candidato
        tiene rotación.
        """
        desde = fields.Date.context_today(self) - timedelta(days=ROTACION_DIAS)
        registros = self.env['product.sales.history'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
            ('start_date', '>=', desde),
        ])
        if not registros:
            return 0.0
        valores = registros.mapped('average_daily_sale')
        return sum(valores) / len(valores)

    # ------------------------------------------------------------------
    # Generación de transferencias
    # ------------------------------------------------------------------

    def create_ict_lines(self, summary_ids, config_id):
        """Usa qty_distributable en lugar del reparto proporcional fijo de Setu.

        El método base calcula la cantidad como order_qty * wh_sharing_percentage / 100, que
        es la regla de 3 hardcodeada. Acá la cantidad ya viene resuelta por la estrategia
        elegida, con el múltiplo aplicado, así que se usa directamente.
        """
        ict_lines = []
        for summary_line in summary_ids:
            lineas = self.line_ids.filtered(
                lambda l: l.product_id == summary_line.product_id
                and l.warehouse_id == config_id.warehouse_id
                and not l.warehouse_group_mismatch
                and l.qty_distributable > 0
            )
            if not lineas:
                continue
            ict_lines.append((0, 0, {
                'product_id': summary_line.product_id.id,
                'quantity': sum(lineas.mapped('qty_distributable')),
                'unit_price': summary_line.product_id.lst_price,
            }))
        return ict_lines

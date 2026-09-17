# -*- coding: utf-8 -*-
import logging

from markupsafe import Markup, escape

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class PrimateStockImportSession(models.Model):
    """Sesión de importación de operaciones de inventario.

    Cada corrida del asistente (validación o importación) queda registrada
    con el archivo original, las filas ya resueltas a ids de Odoo y el log de
    errores/advertencias por fila. Sirve para auditoría y para importar sin
    volver a parsear el archivo.
    """

    _name = 'primate_stock_import.session'
    _description = 'Sesión de importación de operaciones de inventario'
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string="Referencia",
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _("Nuevo"),
    )
    company_id = fields.Many2one(
        'res.company',
        string="Compañía",
        required=True,
        readonly=True,
        default=lambda self: self.env.company,
    )
    user_id = fields.Many2one(
        'res.users',
        string="Usuario",
        readonly=True,
        default=lambda self: self.env.user,
    )
    date_import = fields.Datetime(
        string="Fecha de importación",
        readonly=True,
        default=fields.Datetime.now,
    )
    file_name = fields.Char(string="Nombre del archivo", readonly=True)
    file = fields.Binary(string="Archivo original", readonly=True, attachment=True)
    state = fields.Selection(
        [
            ('pending', "Pendiente de validación"),
            ('validated', "Validado"),
            ('failed', "Con errores"),
            ('imported', "Importado"),
        ],
        string="Estado",
        required=True,
        readonly=True,
        copy=False,
        default='pending',
    )
    total_rows = fields.Integer(string="Filas procesadas", readonly=True)
    move_count = fields.Integer(string="Movimientos creados", readonly=True)
    picking_ids = fields.One2many(
        'stock.picking',
        'import_session_id',
        string="Operaciones creadas",
        readonly=True,
    )
    picking_count = fields.Integer(
        string="Operaciones",
        compute='_compute_picking_count',
    )
    # Resultado de la validación (grupos ya resueltos a ids) para poder importar
    # sin volver a leer el archivo. Ver tools/row_validator.py para la estructura.
    validation_json = fields.Json(string="Resultado de validación", readonly=True)
    error_log = fields.Json(string="Errores por fila", readonly=True)
    warning_log = fields.Json(string="Advertencias por fila", readonly=True)
    error_count = fields.Integer(string="Errores", compute='_compute_log_counts')
    warning_count = fields.Integer(string="Advertencias", compute='_compute_log_counts')
    has_errors = fields.Boolean(string="Tiene errores", compute='_compute_log_counts')
    log_html = fields.Html(
        string="Detalle de validación",
        compute='_compute_log_html',
        sanitize=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Asigna la secuencia a las sesiones nuevas sin referencia."""
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _("Nuevo"):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('primate_stock_import.session')
                    or _("Nuevo")
                )
        return super().create(vals_list)

    @api.depends('picking_ids')
    def _compute_picking_count(self):
        for session in self:
            session.picking_count = len(session.picking_ids)

    @api.depends('error_log', 'warning_log')
    def _compute_log_counts(self):
        for session in self:
            session.error_count = sum(len(msgs) for msgs in (session.error_log or {}).values())
            session.warning_count = sum(
                len(msgs) for msgs in (session.warning_log or {}).values()
            )
            session.has_errors = bool(session.error_log)

    @api.depends('error_log', 'warning_log', 'validation_json', 'state')
    def _compute_log_html(self):
        for session in self:
            session.log_html = session._render_log_html()

    def _render_log_html(self):
        """Arma el HTML del resumen de validación (contadores + tabla por fila).

        Returns:
            Markup: HTML seguro para mostrar en un campo Html.
        """
        self.ensure_one()
        result = self.validation_json or {}
        parts = [Markup(
            '<div class="o_primate_stock_import_summary">'
            '<p><strong>%s</strong> %s &nbsp;|&nbsp; <strong>%s</strong> %s '
            '&nbsp;|&nbsp; <strong>%s</strong> %s</p></div>'
        ) % (
            _("Filas:"), result.get('total_rows', self.total_rows or 0),
            _("Operaciones:"), result.get('picking_count', 0),
            _("Movimientos:"), result.get('move_count', 0),
        )]
        if self.error_log:
            parts.append(Markup('<p class="text-danger"><strong>%s</strong></p>') % (
                _("❌ %s errores bloqueantes. Corregí el archivo y volvé a validar.",
                  self.error_count)
            ))
        if self.warning_log:
            parts.append(Markup('<p class="text-warning"><strong>%s</strong></p>') % (
                _("⚠️ %s advertencias. No bloquean la importación.", self.warning_count)
            ))
        rows = []
        for kind, log, css in (
            ('error', self.error_log or {}, 'text-danger'),
            ('warning', self.warning_log or {}, 'text-warning'),
        ):
            for row, messages in log.items():
                for message in messages:
                    rows.append((int(row), kind, css, message))
        if rows:
            rows.sort(key=lambda item: (item[0], item[1]))
            body = Markup('').join(
                Markup('<tr><td>%s</td><td class="%s">%s</td><td>%s</td></tr>') % (
                    row,
                    css,
                    _("Error") if kind == 'error' else _("Advertencia"),
                    escape(message),
                )
                for row, kind, css, message in rows
            )
            parts.append(Markup(
                '<table class="table table-sm table-striped">'
                '<thead><tr><th>%s</th><th>%s</th><th>%s</th></tr></thead>'
                '<tbody>%s</tbody></table>'
            ) % (_("Fila"), _("Tipo"), _("Detalle"), body))
        return Markup('').join(parts)

    def action_view_pickings(self):
        """Abre la lista de operaciones creadas por esta sesión.

        Returns:
            dict: action window sobre stock.picking filtrada por la sesión.
        """
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id('stock.action_picking_tree_all')
        action['domain'] = [('import_session_id', '=', self.id)]
        action['context'] = {'create': False}
        return action

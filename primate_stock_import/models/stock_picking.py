# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    import_session_id = fields.Many2one(
        'primate_stock_import.session',
        string="Sesión de importación",
        readonly=True,
        index=True,
        copy=False,
    )
    # store=True: se usa como filtro en la lista de operaciones.
    is_imported = fields.Boolean(
        string="Importado",
        compute='_compute_is_imported',
        store=True,
        help="Marcado automáticamente en operaciones creadas por importación desde Excel.",
    )

    @api.depends('import_session_id')
    def _compute_is_imported(self):
        for picking in self:
            picking.is_imported = bool(picking.import_session_id)

    def action_check_availability_batch(self):
        """Confirma y comprueba disponibilidad de las operaciones seleccionadas.

        Recorre las operaciones una a una dentro de un savepoint: si una falla
        (por ejemplo, sin movimientos o con un producto sin ruta) se deja
        constancia en su chatter y se sigue con la siguiente, en lugar de
        abortar toda la selección. ``action_assign`` ya confirma los borradores.

        Returns:
            dict: notificación con la cantidad procesada y la cantidad con error.
        """
        pickings = self.filtered(lambda picking: picking.state not in ('done', 'cancel'))
        processed = self.env['stock.picking']
        failed = self.env['stock.picking']
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    picking.action_assign()
                processed |= picking
            except UserError as error:
                failed |= picking
                _logger.warning(
                    "Comprobar disponibilidad falló en %s: %s", picking.name, error
                )
                picking.message_post(
                    body=_("Comprobar disponibilidad (masivo) falló: %s", error.args[0]),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
        skipped = len(self) - len(pickings)
        message = _("%s operaciones procesadas.", len(processed))
        if failed:
            message += " " + _(
                "%(count)s con error (ver chatter): %(names)s",
                count=len(failed), names=", ".join(failed.mapped('name')),
            )
        if skipped:
            message += " " + _("%s omitidas por estar hechas o canceladas.", skipped)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Comprobar disponibilidad"),
                'message': message,
                'type': 'warning' if failed else 'success',
                'sticky': bool(failed),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

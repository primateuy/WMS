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

        ``action_assign`` ya confirma los borradores. Se omiten las operaciones
        hechas o canceladas.

        Returns:
            dict: notificación con la cantidad procesada y la cantidad con error.
        """
        pickings = self.filtered(lambda picking: picking.state not in ('done', 'cancel'))
        return self._run_batch_action(
            pickings, lambda picking: picking.action_assign(), _("Comprobar disponibilidad"),
            _("%s omitidas por estar hechas o canceladas."),
        )

    def action_mark_as_todo_batch(self):
        """Marca por realizar las operaciones en borrador seleccionadas.

        Equivale al botón *Marcar por realizar* del formulario
        (``action_confirm``): los movimientos pasan a confirmados y, según el
        método de reserva del tipo de operación, se reservan, por lo que la
        operación queda *Lista* si hay stock o *Esperando* si no. Se omiten las
        que no están en borrador.

        Returns:
            dict: notificación con la cantidad procesada y la cantidad con error.
        """
        pickings = self.filtered(lambda picking: picking.state == 'draft')
        return self._run_batch_action(
            pickings, lambda picking: picking._mark_as_todo(), _("Marcar por realizar"),
            _("%s omitidas por no estar en borrador."),
        )

    def _mark_as_todo(self):
        """Confirma una operación en borrador, exigiendo que tenga movimientos.

        ``action_confirm`` no falla sobre una operación sin movimientos: la deja
        en borrador sin avisar, y en el resumen masivo figuraría como procesada.

        Raises:
            UserError: si la operación no tiene movimientos.
        """
        self.ensure_one()
        if not self.move_ids:
            raise UserError(_("La operación %s no tiene movimientos.", self.name))
        self.action_confirm()

    def _run_batch_action(self, pickings, method, title, skipped_message):
        """Ejecuta ``method`` sobre cada operación, aislando los errores.

        Recorre las operaciones una a una dentro de un savepoint: si una falla
        (por ejemplo, sin movimientos o con un producto sin ruta) se deja
        constancia en su chatter y se sigue con la siguiente, en lugar de
        abortar toda la selección.

        Args:
            pickings (stock.picking): subconjunto de ``self`` a procesar.
            method (callable): recibe una operación y ejecuta la acción.
            title (str): título de la notificación y del mensaje en el chatter.
            skipped_message (str): mensaje con ``%s`` para las omitidas.

        Returns:
            dict: notificación con la cantidad procesada y la cantidad con error.
        """
        processed = self.env['stock.picking']
        failed = self.env['stock.picking']
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    method(picking)
                processed |= picking
            except UserError as error:
                failed |= picking
                _logger.warning("%s falló en %s: %s", title, picking.name, error)
                picking.message_post(
                    body=_("%(title)s (masivo) falló: %(error)s", title=title, error=error.args[0]),
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
            message += " " + skipped_message % skipped
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': 'warning' if failed else 'success',
                'sticky': bool(failed),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

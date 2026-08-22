# -*- coding: utf-8 -*-
import logging

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


class AdvanceReorderPlanner(models.Model):
    _inherit = 'advance.reorder.planner'

    def create_real_demand_record(self):
        """Avisa por correo cuando el planificador dejó reposiciones para revisar.

        No se agrega un campo execution_mode: el flujo automático ya se configura con
        advance.reorder.process.auto.workflow, que tiene create_replenishment y
        validate_replenishment con onchange en cascada. El "modo notificación" es
        create_replenishment activo, y este override solo le suma el aviso.

        Nota sobre el alcance: el auto workflow de Setu nunca genera transferencias
        (action_procurement_internal_transfer solo se dispara desde el botón de la vista),
        así que toda ejecución programada termina, como máximo, con la demanda calculada
        esperando revisión manual.
        """
        inicio = fields.Datetime.now()
        res = super().create_real_demand_record()
        for planner in self:
            if not planner.auto_workflow_id.create_replenishment:
                continue
            reposiciones = self.env['advance.procurement.process'].search([
                ('reorder_id.reorder_planner_id', '=', planner.id),
                ('create_date', '>=', inicio),
                ('state', 'in', ('draft', 'inprogress', 'verified')),
            ])
            if reposiciones:
                planner._primate_notificar_reposiciones(reposiciones)
        return res

    def _primate_notificar_reposiciones(self, reposiciones):
        """Manda un correo con resumen numérico y enlace a cada reposición pendiente.

        Solo totales, sin detalle línea por línea: en procesos con miles de líneas el mail
        sería inmanejable y el detalle se revisa entrando al proceso.
        """
        self.ensure_one()
        destinatario = self.user_id.partner_id
        if not destinatario or not destinatario.email:
            _logger.info(
                "Planificador %s: no se envía aviso de reposición, el responsable no tiene "
                "correo configurado.", self.name)
            return False

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        bloques = []
        for reposicion in reposiciones:
            lineas = reposicion.line_ids
            ejecutables = len(lineas.filtered(lambda l: l.execution_status == 'executable'))
            parciales = len(lineas.filtered(lambda l: l.execution_status == 'partial'))
            imposibles = len(lineas.filtered(lambda l: l.execution_status == 'impossible'))
            fuera_grupo = len(lineas.filtered('warehouse_group_mismatch'))
            monto = sum(
                linea.qty_distributable * linea.product_id.lst_price for linea in lineas
            )
            enlace = '%s/odoo/action-setu_advance_reordering.actions_advance_procurement_process/%s' % (
                base_url, reposicion.id)
            bloques.append(_(
                '<p><b>%(nombre)s</b> — almacén origen %(almacen)s<br/>'
                'Ejecutables: %(ok)s · Parciales: %(parcial)s · Sin ejecución: %(no)s · '
                'Fuera de grupo: %(fuera)s<br/>'
                'Monto estimado a transferir: %(monto).2f<br/>'
                '<a href="%(enlace)s">Abrir la reposición</a></p>',
                nombre=reposicion.name or reposicion.id,
                almacen=reposicion.warehouse_id.display_name,
                ok=ejecutables, parcial=parciales, no=imposibles, fuera=fuera_grupo,
                monto=monto, enlace=enlace,
            ))

        cuerpo = _(
            '<p>El planificador <b>%(planner)s</b> generó %(cantidad)s reposición(es) que '
            'quedaron esperando revisión manual.</p>%(detalle)s',
            planner=self.name, cantidad=len(reposiciones), detalle=''.join(bloques),
        )
        self.env['mail.mail'].sudo().create({
            'subject': _('Reposiciones pendientes de revisión — %s') % self.name,
            'body_html': cuerpo,
            'email_to': destinatario.email,
            'auto_delete': False,
        }).send()
        return True

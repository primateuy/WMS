# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Campos que, al cambiar, obligan a invalidar la caché de reglas de registro:
# el dominio de las ir.rule de este módulo se evalúa leyendo el usuario, y
# _compute_domain está cacheado por usuario.
CAMPOS_RESTRICCION = ('restrict_picking_types', 'allowed_picking_type_ids')


class ResUsers(models.Model):
    _inherit = 'res.users'

    restrict_picking_types = fields.Boolean(
        string='Restringir tipos de operación',
        help='Si está activo, el usuario solo ve las operaciones de '
             'inventario de los tipos de operación que tenga asignados.',
    )
    allowed_picking_type_ids = fields.Many2many(
        comodel_name='stock.picking.type',
        relation='res_users_allowed_picking_type_rel',
        column1='user_id',
        column2='picking_type_id',
        string='Tipos de operación permitidos',
    )

    @api.constrains('restrict_picking_types', 'allowed_picking_type_ids')
    def _check_allowed_picking_types(self):
        """Una restricción sin tipos asignados dejaría al usuario sin nada."""
        for user in self:
            if user.restrict_picking_types and not user.allowed_picking_type_ids:
                raise ValidationError(
                    _('El usuario %s tiene la restricción de tipos de '
                      'operación activada pero no tiene ninguno asignado: '
                      'no podría ver ninguna operación.') % user.display_name)

    @api.constrains('groups_id', 'restrict_picking_types')
    def _check_remito_print_only_restriction(self):
        """El perfil de solo impresión siempre va restringido.

        Es un perfil pensado para operadores logísticos externos: sin la
        restricción verían todas las operaciones de la empresa.
        """
        grupo = self.env.ref(
            'stock_remito_print.group_remito_print_only',
            raise_if_not_found=False)
        if not grupo:
            return
        for user in self:
            if grupo in user.groups_id and not user.restrict_picking_types:
                raise ValidationError(
                    _('El usuario %s pertenece al perfil «Impresión de '
                      'remitos (solo)», que exige restringir los tipos de '
                      'operación.') % user.display_name)

    @api.model_create_multi
    def create(self, vals_list):
        usuarios = super().create(vals_list)
        # Un usuario nuevo puede nacer ya restringido.
        if any(campo in vals for vals in vals_list for campo in CAMPOS_RESTRICCION):
            self.env.registry.clear_cache()
        return usuarios

    def write(self, vals):
        resultado = super().write(vals)
        if any(campo in vals for campo in CAMPOS_RESTRICCION):
            # Sin esto el usuario sigue viendo lo de antes hasta que se
            # reinicie el servidor: _compute_domain está cacheado.
            self.env.registry.clear_cache()
        return resultado

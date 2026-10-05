# -*- coding: utf-8 -*-
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class ResGroups(models.Model):
    _inherit = 'res.groups'

    @api.model
    def _remito_configurar_menus(self):
        """Deja al perfil de solo impresión con el menú Remitos y nada más.

        Odoo por sí solo no puede ocultarle a un grupo los menús que vienen
        habilitados para cualquier usuario interno (Conversaciones,
        Contactos, Calendario, Aplicaciones...), porque esos menús son
        compartidos con todos.

        En FORUM eso se resuelve con `generic_security_restriction`, que
        agrega a res.groups una lista blanca de menús (`menu_access_only`):
        si el grupo la tiene cargada, el usuario ve únicamente esos menús y
        sus hijos. Es el mismo mecanismo que usa el perfil «Usuario
        Sucursal».

        Este método se llama desde el data del módulo, así que corre tanto
        al instalar como al actualizar. Si `generic_security_restriction` no
        está instalado no hace nada: el módulo no depende de él.
        """
        if 'menu_access_only' not in self._fields:
            _logger.info(
                'generic_security_restriction no está instalado: el perfil de '
                'impresión de remitos va a ver los menús estándar de usuario '
                'interno.')
            return False

        grupo = self.env.ref(
            'stock_remito_print.group_remito_print_only',
            raise_if_not_found=False)
        menu = self.env.ref(
            'stock_remito_print.menu_remito_root',
            raise_if_not_found=False)
        if not grupo or not menu:
            return False

        # Se respeta lo que haya configurado a mano: solo se agrega el menú
        # propio si todavía no está en la lista.
        if menu not in grupo.menu_access_only:
            grupo.sudo().write({'menu_access_only': [(4, menu.id)]})
            _logger.info(
                'Perfil de impresión de remitos restringido al menú Remitos.')

        self._remito_ocultar_acciones_de_inventario(grupo)
        return True

    @api.model
    def _remito_ocultar_acciones_de_inventario(self, grupo):
        """Saca del menú Acciones lo que el perfil no puede usar.

        Las acciones de servidor de stock (Validar, Anular reserva,
        Bloquear, Desechar) están publicadas para cualquiera: no tienen
        restricción de grupo en Odoo. Al operador le aparecían en el menú
        Acciones y, si las usaba, recibía un error de permisos.

        `generic_security_restriction` permite ocultárselas a un grupo sin
        tocar las acciones del core, que son compartidas con todos.
        """
        if 'hidden_server_actions_ids' not in self._fields:
            return False

        xmlids = (
            'stock.action_validate_picking',
            'stock.action_unreserve_picking',
            'stock.action_toggle_is_locked',
            'stock.action_scrap',
        )
        comandos = []
        for xmlid in xmlids:
            accion = self.env.ref(xmlid, raise_if_not_found=False)
            if accion and accion not in grupo.hidden_server_actions_ids:
                comandos.append((4, accion.id))
        if comandos:
            grupo.sudo().write({'hidden_server_actions_ids': comandos})
            _logger.info(
                'Ocultas %s acciones de servidor de inventario al perfil de '
                'impresión de remitos.', len(comandos))
        return True

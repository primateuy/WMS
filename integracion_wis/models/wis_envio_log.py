# -*- coding: utf-8 -*-
"""Lo que Odoo le manda a WIS, tal como salió, con lo que WIS contestó.

Hasta ahora sólo los productos y los agentes guardaban lo enviado; las recepciones, los
pedidos, las anulaciones y las consultas no dejaban rastro, y ante un rechazo había que
reconstruir el JSON a mano.

El registro se escribe en un cursor propio: cuando WIS rechaza algo, la transacción que
mandó se deshace, y es justo el envío que hace falta ver. Por eso el picking se guarda
como número (`picking_ref`) y no como clave foránea: el picking puede haberse creado en
esa misma transacción, todavía sin confirmar, y desde otro cursor no existe.

No se guardan los encabezados (llevan el token) ni el pedido de token (lleva el secreto).
"""
import json
import logging
import re
import threading
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

PATRON_CODIGO_PICKING = re.compile(r'^W-[A-Z]+-(\d+)$')
DIAS_POR_DEFECTO = 90


class WisEnvioLog(models.Model):
    _name = 'wis.envio.log'
    _description = 'Envío a WIS'
    _order = 'id desc'
    _rec_name = 'endpoint'

    fecha = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    metodo = fields.Char(string="Método", readonly=True)
    endpoint = fields.Char(readonly=True, index=True)
    params = fields.Text(string="Parámetros", readonly=True)
    request = fields.Text(string="Enviado", readonly=True)
    status_http = fields.Integer(string="HTTP", readonly=True)
    respuesta = fields.Text(readonly=True)
    duracion_ms = fields.Integer(string="Duración (ms)", readonly=True)
    resultado = fields.Selection([
        ('exito', 'Éxito'),
        ('rechazado', 'Rechazado por WIS'),
        ('error', 'Sin respuesta'),
    ], readonly=True, index=True)
    error = fields.Text(readonly=True)
    codigo_unico = fields.Char(string="Código WMS", readonly=True, index=True)
    picking_ref = fields.Integer(string="Id del picking", readonly=True, index=True)
    picking_id = fields.Many2one('stock.picking', string="Picking", compute='_compute_picking_id',
                                 search='_search_picking_id')
    usuario_id = fields.Many2one('res.users', string="Usuario", readonly=True, ondelete='set null')

    @api.depends('picking_ref')
    def _compute_picking_id(self):
        existentes = self.env['stock.picking'].browse(
            set(self.filtered('picking_ref').mapped('picking_ref'))).exists()
        for rec in self:
            picking = existentes.filtered(lambda p: p.id == rec.picking_ref)
            rec.picking_id = picking[:1]

    def _search_picking_id(self, operator, value):
        pickings = self.env['stock.picking'].search([('id', operator, value)])
        return [('picking_ref', 'in', pickings.ids)]

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------
    @api.model
    def _registrar(self, metodo, endpoint, params=None, body=None, status_http=0,
                   respuesta=None, duracion_ms=0, error=None):
        """Guarda un envío. Nunca lanza: si no se puede registrar, el envío sigue igual."""
        try:
            vals = self._valores(metodo, endpoint, params, body, status_http, respuesta,
                                 duracion_ms, error)
            if getattr(threading.current_thread(), 'testing', False):
                # En un test no hay otra transacción que sobreviva: se escribe en la misma.
                self.sudo().create(vals)
                return
            with self.pool.cursor() as cr:
                self.env(cr=cr, su=True)['wis.envio.log'].create(vals)
        except Exception as e:
            _logger.warning("[WIS] No se pudo registrar el envío a %s: %s", endpoint, e)

    @api.model
    def _valores(self, metodo, endpoint, params, body, status_http, respuesta, duracion_ms, error):
        from odoo.addons.integracion_wis.controllers.controllers import WebhookWIS
        codigo = WebhookWIS()._extraer_codigo_wis(body) if body else ''
        if status_http == 200 and not error:
            resultado = 'exito'
        elif status_http:
            resultado = 'rechazado'
        else:
            resultado = 'error'
        return {
            'metodo': metodo,
            'endpoint': endpoint,
            'params': self._texto(params),
            'request': self._texto(body),
            'status_http': status_http or 0,
            'respuesta': self._texto(respuesta),
            'duracion_ms': duracion_ms,
            'resultado': resultado,
            'error': error or False,
            'codigo_unico': codigo or False,
            'picking_ref': self._picking_del_codigo(codigo),
            'usuario_id': self.env.uid,
        }

    @api.model
    def _texto(self, valor):
        if valor in (None, '', {}, []):
            return False
        if isinstance(valor, str):
            try:
                return json.dumps(json.loads(valor), indent=2, ensure_ascii=False)
            except ValueError:
                return valor
        try:
            return json.dumps(valor, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(valor)

    @api.model
    def _picking_del_codigo(self, codigo):
        """El código W-X-<id> lo arma Odoo con el id del picking que lo originó."""
        primero = (codigo or '').split(',')[0].strip()
        if not primero:
            return False
        m = PATRON_CODIGO_PICKING.match(primero)
        if m:
            return int(m.group(1))
        picking = self.env['stock.picking'].sudo().search([('codigo_unico', '=', primero)],
                                                         order='id', limit=1)
        return picking.id or False

    # ------------------------------------------------------------------
    # Limpieza
    # ------------------------------------------------------------------
    @api.model
    def _cron_limpiar(self):
        dias = self.env['ir.config_parameter'].sudo().get_param(
            'integracion_wis.envio_log_dias', DIAS_POR_DEFECTO)
        try:
            dias = int(dias)
        except (TypeError, ValueError):
            dias = DIAS_POR_DEFECTO
        if dias <= 0:
            return True
        viejos = self.sudo().search([('fecha', '<', fields.Datetime.now() - timedelta(days=dias))])
        viejos.unlink()
        _logger.info("[WIS] Envíos a WIS: %s registros de más de %s días borrados", len(viejos), dias)
        return True

    def action_ver_picking(self):
        self.ensure_one()
        if not self.picking_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': self.picking_id.id,
            'view_mode': 'form',
            'name': _("Picking"),
        }


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    wis_envio_count = fields.Integer(string="Envíos a WIS", compute='_compute_wis_envio_count')

    def _wis_envio_domain(self):
        self.ensure_one()
        dominio = [('picking_ref', '=', self.id)]
        if self.codigo_unico:
            dominio = ['|', ('codigo_unico', '=', self.codigo_unico)] + dominio
        return dominio

    def _compute_wis_envio_count(self):
        Log = self.env['wis.envio.log'].sudo()
        for picking in self:
            picking.wis_envio_count = Log.search_count(picking._wis_envio_domain()) if picking.id else 0

    def action_ver_envios_wis(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Envíos a WIS"),
            'res_model': 'wis.envio.log',
            'view_mode': 'tree,form',
            'domain': self._wis_envio_domain(),
        }

# -*- coding: utf-8 -*-
import base64
import json
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Prefijo de los adjuntos PDF que genera este módulo. Sirve para reconocer el
# PDF del CFE entre el resto de los adjuntos de la operación sin tener que
# agregarle campos propios a ir.attachment.
UCFE_ATTACHMENT_PREFIX = 'eRemito_'

# Estados de UCFE con los que consideramos que el CFE fue aceptado:
#   00 -> CFE procesado exitosamente y recibido por DGI
#   11 -> CFE procesado por UCFE, esperando respuesta de DGI
# Son los mismos dos códigos con los que l10n_uy_einvoice_uruware marca
# cfe_emitido = True, así que el criterio no se separa del de la integración.
UCFE_STATES_ACEPTADOS = ('00', '11')

# Equivalentes en el campo cfe_state de DGI, para las operaciones emitidas
# antes de que existiera ucfe_state y para las bases donde solo está
# instalado l10n_uy_einvoice_base:
#   7 -> Confirmado DGI (mapeado desde el código 00)
#   5 -> Registrado en servidor (mapeado desde el código 11)
CFE_STATES_ACEPTADOS = ('7', '5')

PARAM_FALLBACK = 'stock_remito_print.fallback_on_ucfe_error'
PARAM_TIMEOUT = 'stock_remito_print.ucfe_timeout'

# Segundos de espera máximos para la llamada a UCFE si el parámetro de
# sistema no está cargado o tiene un valor inválido.
DEFAULT_UCFE_TIMEOUT = 20


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    remito_printed = fields.Boolean(
        string='Remito impreso',
        index=True,
        tracking=True,
        copy=False,
        default=False,
    )
    remito_printed_date = fields.Datetime(
        string='Fecha de impresión',
        readonly=True,
        copy=False,
    )
    remito_printed_by = fields.Many2one(
        comodel_name='res.users',
        string='Impreso por',
        ondelete='set null',
        readonly=True,
        copy=False,
    )
    remito_print_count = fields.Integer(
        string='Cantidad de impresiones',
        default=0,
        readonly=True,
        copy=False,
    )
    remito_last_source = fields.Selection(
        selection=[
            ('ucfe', 'PDF de UCFE'),
            ('standard', 'Reporte estándar'),
            ('standard_fallback', 'Reporte estándar (falla de UCFE)'),
        ],
        string='Origen del último PDF',
        readonly=True,
        copy=False,
    )

    # stock.picking no tiene almacén propio: se agrega como related
    # almacenado para poder filtrar y agrupar por almacén en el menú de
    # remitos. Al instalar el módulo se completa de una pasada.
    remito_warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse',
        string='Almacén',
        related='picking_type_id.warehouse_id',
        store=True,
        index=True,
        readonly=True,
    )

    # Campos espejo de la facturación electrónica. Existen siempre, aunque no
    # haya ningún módulo l10n_uy_einvoice_* instalado, para que las vistas y
    # los dominios de este módulo nunca referencien campos que pueden faltar.
    remito_usa_cfe = fields.Boolean(
        string='Emite CFE',
        compute='_compute_remito_cfe_info',
        search='_search_remito_usa_cfe',
    )
    remito_cfe_ref = fields.Char(
        string='Serie/Número CFE',
        compute='_compute_remito_cfe_info',
    )
    ucfe_pdf_available = fields.Boolean(
        string='PDF UCFE disponible',
        compute='_compute_ucfe_pdf_available',
        search='_search_ucfe_pdf_available',
    )

    # ------------------------------------------------------------------
    # Detección de la integración de facturación electrónica
    # ------------------------------------------------------------------

    @api.model
    def _remito_cfe_layers(self):
        """Indica qué capas de facturación electrónica hay instaladas.

        El módulo no depende de LocalizacionUy: en vez de importar nada, mira
        si los campos existen en el registro. Devuelve un diccionario con dos
        claves:

        - ``base``: está l10n_uy_einvoice_base, o sea que hay campos de CFE
          sobre la operación y el tipo de operación.
        - ``uruware``: además está l10n_uy_einvoice_uruware, que es el que
          sabe pedirle el PDF a UCFE.
        """
        picking_fields = self._fields
        type_fields = self.env['stock.picking.type']._fields
        base = 'cfe_emitido' in picking_fields and 'uses_cfe' in type_fields
        uruware = base and 'ucfe_state' in picking_fields
        return {'base': base, 'uruware': uruware}

    def _remito_cfe_aceptado(self):
        """Indica si la operación tiene un CFE aceptado por UCFE."""
        self.ensure_one()
        capas = self._remito_cfe_layers()
        if not capas['base'] or not self.cfe_emitido:
            return False
        # Con uruware instalado manda ucfe_state, que es el estado real del
        # comprobante en el servidor de UCFE.
        if capas['uruware'] and self.ucfe_state:
            return self.ucfe_state in UCFE_STATES_ACEPTADOS
        # Sin ucfe_state caemos al estado DGI, que es lo único que hay en las
        # operaciones viejas y en las bases sin uruware.
        return self.cfe_state in CFE_STATES_ACEPTADOS

    @api.model
    def _remito_cfe_aceptado_domain(self):
        """Dominio equivalente a :meth:`_remito_cfe_aceptado`."""
        capas = self._remito_cfe_layers()
        if not capas['base']:
            return [('id', '=', False)]
        domain = [('cfe_emitido', '=', True)]
        if capas['uruware']:
            domain += [
                '|',
                ('ucfe_state', 'in', list(UCFE_STATES_ACEPTADOS)),
                '&',
                ('ucfe_state', '=', False),
                ('cfe_state', 'in', list(CFE_STATES_ACEPTADOS)),
            ]
        else:
            domain += [('cfe_state', 'in', list(CFE_STATES_ACEPTADOS))]
        return domain

    # ------------------------------------------------------------------
    # Campos calculados
    # ------------------------------------------------------------------

    @api.depends('picking_type_id')
    def _compute_remito_cfe_info(self):
        capas = self._remito_cfe_layers()
        for picking in self:
            if not capas['base']:
                picking.remito_usa_cfe = False
                picking.remito_cfe_ref = False
                continue
            picking.remito_usa_cfe = bool(picking.picking_type_id.uses_cfe)
            picking.remito_cfe_ref = picking.cfe_serie_num or False

    def _search_remito_usa_cfe(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(
                _('Operador no soportado para «Emite CFE»: %s') % operator)
        positivo = (operator == '=') == bool(value)
        if not self._remito_cfe_layers()['base']:
            # Sin facturación electrónica ninguna operación emite CFE.
            return [('id', '=', False)] if positivo else [('id', '!=', False)]
        return [('picking_type_id.uses_cfe', '=', positivo)]

    def _compute_ucfe_pdf_available(self):
        disponibles = self._remito_ucfe_adjuntos()
        for picking in self:
            picking.ucfe_pdf_available = picking.id in disponibles

    def _search_ucfe_pdf_available(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(
                _('Operador no soportado para «PDF UCFE disponible»: %s')
                % operator)
        positivo = (operator == '=') == bool(value)
        # Solo las operaciones con CFE aceptado son candidatas, así que el
        # recorrido en Python queda acotado a un conjunto chico.
        candidatos = self.search(self._remito_cfe_aceptado_domain())
        disponibles = list(candidatos._remito_ucfe_adjuntos())
        if positivo:
            return [('id', 'in', disponibles)]
        return [('id', 'not in', disponibles)]

    # ------------------------------------------------------------------
    # Adjunto con el PDF del CFE
    # ------------------------------------------------------------------

    def _remito_ucfe_nombre_archivo(self):
        """Nombre con el que este módulo adjunta el PDF del CFE."""
        self.ensure_one()
        return '%s%s.pdf' % (
            UCFE_ATTACHMENT_PREFIX, self.remito_cfe_ref or self.name or self.id)

    def _remito_ucfe_nombres_adjunto(self):
        """Nombres de adjunto que reconocemos como PDF del CFE.

        Además del nombre propio hay que reconocer los que deja
        l10n_uy_einvoice_uruware, que adjunta el PDF con el nombre de la
        operación (que a esa altura ya fue renombrada a la serie/número del
        CFE por ``carga_datos_firma``).
        """
        self.ensure_one()
        nombres = {self._remito_ucfe_nombre_archivo()}
        if self.name:
            nombres.add(self.name)
        if self.remito_cfe_ref:
            nombres.add(self.remito_cfe_ref)
        return nombres

    def _remito_ucfe_adjuntos(self):
        """Devuelve {id_operación: adjunto} con el PDF del CFE de cada una.

        Solo se consideran las operaciones con CFE aceptado: así un PDF que
        haya subido un usuario a mano, aunque coincida de nombre, no se
        confunde con el comprobante de UCFE.
        """
        resultado = {}
        candidatos = self.filtered(lambda p: p._remito_cfe_aceptado())
        if not candidatos:
            return resultado
        adjuntos = self.env['ir.attachment'].sudo().search(
            [
                ('res_model', '=', 'stock.picking'),
                ('res_id', 'in', candidatos.ids),
                ('mimetype', '=', 'application/pdf'),
            ],
            order='id desc',
        )
        nombres = {p.id: p._remito_ucfe_nombres_adjunto() for p in candidatos}
        for adjunto in adjuntos:
            # Los adjuntos vienen del más nuevo al más viejo: nos quedamos
            # con el primero que coincida.
            if adjunto.res_id in resultado:
                continue
            if adjunto.name in nombres.get(adjunto.res_id, ()):
                resultado[adjunto.res_id] = adjunto
        return resultado

    # ------------------------------------------------------------------
    # Obtención del PDF
    # ------------------------------------------------------------------

    @api.model
    def _remito_ucfe_timeout(self):
        """Segundos de espera para la llamada a UCFE."""
        valor = self.env['ir.config_parameter'].sudo().get_param(
            PARAM_TIMEOUT, DEFAULT_UCFE_TIMEOUT)
        try:
            timeout = float(valor)
        except (TypeError, ValueError):
            timeout = DEFAULT_UCFE_TIMEOUT
        return timeout if timeout > 0 else DEFAULT_UCFE_TIMEOUT

    @api.model
    def _remito_fallback_habilitado(self):
        """Indica si ante una falla de UCFE se imprime el reporte estándar."""
        valor = self.env['ir.config_parameter'].sudo().get_param(
            PARAM_FALLBACK, 'False')
        return str(valor).strip().lower() in ('1', 'true', 't', 'yes', 'si', 'sí')

    def _remito_ucfe_request_pdf(self):
        """Le pide a UCFE el PDF del CFE y devuelve el contenido en base64.

        Replica la llamada de ``_obtener_pdf_a4`` de
        l10n_uy_einvoice_uruware reutilizando sus helpers, pero con timeout
        acotado y devolviendo el contenido en vez de una acción de descarga.
        """
        self.ensure_one()
        # xmltodict es una dependencia de l10n_uy_einvoice_uruware. Se importa
        # acá adentro para que el módulo instale igual sin esa integración.
        import xmltodict

        picking = self.sudo()
        cfe_base_url = picking.get_param(param=picking.get_cfe_base_url_parm())
        user = picking.get_param(param='cfe_user')
        password = picking.get_param(param='cfe_password')
        query_url = self.env['ir.config_parameter'].sudo().get_param(
            'url_query_value')
        if not query_url:
            raise UserError(
                _('No está configurado el parámetro de sistema '
                  '«url_query_value», necesario para pedirle el PDF a UCFE.'))
        full_url = cfe_base_url + query_url + '/WebServicesFe.svc/rest/ObtenerPdf'

        cfe_type = picking.cfe_type or (
            picking.l10n_latam_document_type_id.dgi_cfe_type
            if picking.l10n_latam_document_type_id else False)
        if not cfe_type:
            raise UserError(
                _('No se pudo determinar el tipo de CFE de la operación %s.')
                % picking.name)

        token = base64.b64encode(
            ('%s:%s' % (user, password)).encode('utf-8')).decode('utf-8')
        payload = json.dumps({
            'rut': picking.company_id.vat or self.env.company.vat,
            'tipoCfe': str(cfe_type),
            'serieCfe': picking.serie(),
            'numeroCfe': str(picking.numero_cfe()),
        })
        headers = {
            'Authorization': 'Basic ' + token,
            'Content-Type': 'application/json',
        }
        response = requests.post(
            url=full_url,
            headers=headers,
            data=payload,
            timeout=self._remito_ucfe_timeout(),
        )
        if response.status_code >= 300:
            raise UserError(
                _('UCFE respondió con el código %(codigo)s al pedir el PDF '
                  'de %(operacion)s.')
                % {'codigo': response.status_code, 'operacion': picking.name})
        contenido = xmltodict.parse(response.content)
        pdf_b64 = (contenido.get('base64Binary') or {}).get('#text')
        if not pdf_b64:
            raise UserError(
                _('UCFE no devolvió el PDF del e-Remito %s.') % picking.name)
        return pdf_b64

    def _remito_fetch_ucfe_pdf(self):
        """Pide el PDF a UCFE, lo adjunta a la operación y devuelve los bytes."""
        self.ensure_one()
        if not self._remito_cfe_layers()['uruware']:
            raise UserError(
                _('No hay una integración con UCFE instalada para pedir el '
                  'PDF de la operación %s.') % self.name)
        pdf_b64 = self._remito_ucfe_request_pdf()
        self.env['ir.attachment'].sudo().create({
            'name': self._remito_ucfe_nombre_archivo(),
            'type': 'binary',
            'datas': pdf_b64,
            'mimetype': 'application/pdf',
            'res_model': 'stock.picking',
            'res_id': self.id,
        })
        # ucfe_pdf_available no es almacenado y no puede depender de
        # ir.attachment: hay que invalidarlo a mano para que el adjunto
        # recién creado se vea en lo que queda de la transacción.
        self.invalidate_recordset(['ucfe_pdf_available'])
        return base64.b64decode(pdf_b64)

    def _remito_get_standard_report(self):
        """Reporte estándar configurado para el tipo de operación."""
        self.ensure_one()
        # El tipo de operación se lee con sudo porque el perfil de solo
        # impresión no tiene por qué poder leer su configuración completa.
        report = self.picking_type_id.sudo().remito_report_id
        if not report:
            report = self.env.ref(
                'stock.action_report_delivery', raise_if_not_found=False)
        if not report:
            raise UserError(
                _('No hay un reporte de remito configurado para el tipo de '
                  'operación %s y tampoco está disponible el reporte de '
                  'entrega estándar de Odoo.') % self.picking_type_id.display_name)
        return report

    def _remito_render_standard_pdf(self):
        """Renderiza el reporte estándar de la operación y devuelve los bytes."""
        self.ensure_one()
        report = self._remito_get_standard_report()
        # Se renderiza con el entorno del usuario, no con sudo, para que las
        # reglas de registro y los permisos se sigan aplicando al reporte.
        contenido, tipo = self.env['ir.actions.report']._render_qweb_pdf(
            report.report_name, res_ids=self.ids)
        if tipo != 'pdf':
            # En modo test Odoo devuelve HTML en vez de llamar a wkhtmltopdf.
            raise UserError(
                _('El reporte %(reporte)s devolvió contenido «%(tipo)s» en '
                  'vez de un PDF.')
                % {'reporte': report.report_name, 'tipo': tipo})
        return contenido

    def _remito_get_pdf(self):
        """Resuelve el PDF del remito de una operación.

        Devuelve la tupla ``(contenido, origen, error)``, donde ``origen`` es
        uno de ``ucfe``, ``standard``, ``standard_fallback`` o ``error``.
        """
        self.ensure_one()

        # 4. La operación no emite CFE: reporte estándar.
        if not self._remito_cfe_aceptado():
            try:
                return self._remito_render_standard_pdf(), 'standard', False
            except Exception as error:
                _logger.warning(
                    'No se pudo renderizar el reporte estándar de %s: %s',
                    self.name, error)
                return False, 'error', str(error)

        # 1. Hay CFE aceptado y el PDF ya está adjunto.
        adjunto = self._remito_ucfe_adjuntos().get(self.id)
        if adjunto:
            return adjunto.raw, 'ucfe', False

        # 2. Hay CFE aceptado y hay que pedirle el PDF a UCFE.
        try:
            return self._remito_fetch_ucfe_pdf(), 'ucfe', False
        except Exception as error:
            mensaje = str(error)
            _logger.warning(
                'No se pudo obtener de UCFE el PDF de %s: %s',
                self.name, mensaje)

        # 3. Falló UCFE: error, salvo que el fallback esté habilitado.
        if not self._remito_fallback_habilitado():
            return False, 'error', mensaje
        try:
            contenido = self._remito_render_standard_pdf()
        except Exception as error:
            return False, 'error', _(
                'Falló UCFE (%(ucfe)s) y también el reporte estándar '
                '(%(estandar)s).') % {'ucfe': mensaje, 'estandar': error}
        self._remito_post_message(_(
            'Se imprimió el <b>reporte estándar</b> porque no se pudo obtener '
            'el PDF de UCFE: %s') % mensaje)
        return contenido, 'standard_fallback', False

    # ------------------------------------------------------------------
    # Marcado de impresión
    # ------------------------------------------------------------------

    def _remito_check_read_access(self):
        """Verifica que el usuario actual pueda leer estas operaciones."""
        self.check_access_rights('read')
        self.check_access_rule('read')

    def _remito_post_message(self, body):
        """Deja un mensaje en el chatter firmado por el usuario actual."""
        autor = self.env.user.partner_id.id
        for picking in self:
            picking.sudo().message_post(body=body, author_id=autor)

    def _remito_register_print(self, source=None):
        """Marca las operaciones como impresas.

        Se escribe con ``sudo()`` y solo sobre los campos de marcado: el
        perfil de solo impresión no tiene permiso de escritura sobre
        stock.picking.
        """
        ahora = fields.Datetime.now()
        usuario = self.env.user.id
        for picking in self:
            valores = {
                'remito_printed': True,
                'remito_printed_date': ahora,
                'remito_printed_by': usuario,
                'remito_print_count': picking.remito_print_count + 1,
            }
            if source:
                valores['remito_last_source'] = source
            picking.sudo().write(valores)

    def action_remito_mark_printed(self):
        """Marca manualmente las operaciones seleccionadas como impresas."""
        self._remito_check_read_access()
        self._remito_register_print()
        self._remito_post_message(
            _('Marcada manualmente como impresa por %s.')
            % self.env.user.display_name)
        return True

    def action_remito_mark_unprinted(self):
        """Quita manualmente la marca de impresa.

        Se conservan la cantidad de impresiones y el origen del último PDF,
        que son el historial de lo que efectivamente se imprimió.
        """
        self._remito_check_read_access()
        self.sudo().write({
            'remito_printed': False,
            'remito_printed_date': False,
            'remito_printed_by': False,
        })
        self._remito_post_message(
            _('Marcada manualmente como NO impresa por %s.')
            % self.env.user.display_name)
        return True

# -*- coding: utf-8 -*-
import base64
import io

from odoo.tests.common import TransactionCase
from odoo.tools.pdf import PdfFileWriter


def pdf_minimo():
    """Devuelve un PDF válido de una página en blanco.

    Sirve para no depender de wkhtmltopdf en los tests: en modo test Odoo
    ni siquiera lo llama, devuelve HTML.
    """
    writer = PdfFileWriter()
    writer.addBlankPage(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


PDF_MINIMO = pdf_minimo()
PDF_MINIMO_B64 = base64.b64encode(PDF_MINIMO).decode('ascii')


class RespuestaUcfeFalsa:
    """Imita lo justo de requests.Response que usa el módulo."""

    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def respuesta_ucfe_ok(pdf_b64=PDF_MINIMO_B64):
    """Respuesta de UCFE con el PDF adentro, como la devuelve ObtenerPdf."""
    cuerpo = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<base64Binary xmlns="http://schemas.microsoft.com/2003/10/Serialization/">'
        '%s</base64Binary>' % pdf_b64
    )
    return RespuestaUcfeFalsa(cuerpo.encode('utf-8'))


def respuesta_ucfe_sin_pdf():
    """Respuesta de UCFE sin contenido, el caso de error más habitual."""
    cuerpo = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<base64Binary xmlns="http://schemas.microsoft.com/2003/10/Serialization/"/>'
    )
    return RespuestaUcfeFalsa(cuerpo.encode('utf-8'))


class RemitoPrintSetupMixin:
    """Datos compartidos por los tests del módulo.

    Va como mixin para poder usarlo tanto desde TransactionCase como
    desde HttpCase.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

        cls.company = cls.env.company
        cls.partner = cls.env['res.partner'].create({
            'name': 'Cliente de prueba remitos',
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Producto de prueba remitos',
            'type': 'consu',
        })

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.location_src = cls.warehouse.lot_stock_id
        cls.location_dest = cls.env.ref('stock.stock_location_customers')

        # Dos tipos de operación de salida para poder probar la restricción.
        cls.picking_type_a = cls._crear_picking_type('Remitos A', 'RMA')
        cls.picking_type_b = cls._crear_picking_type('Remitos B', 'RMB')

        cls.picking_a = cls._crear_picking(cls.picking_type_a)
        cls.picking_b = cls._crear_picking(cls.picking_type_b)

        cls.capas = cls.env['stock.picking']._remito_cfe_layers()

    @classmethod
    def _crear_picking_type(cls, nombre, codigo):
        return cls.env['stock.picking.type'].create({
            'name': nombre,
            'sequence_code': codigo,
            'code': 'outgoing',
            'warehouse_id': cls.warehouse.id,
            'default_location_src_id': cls.location_src.id,
            'default_location_dest_id': cls.location_dest.id,
            'company_id': cls.company.id,
        })

    @classmethod
    def _crear_picking(cls, picking_type):
        picking = cls.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': cls.partner.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': picking_type.default_location_dest_id.id,
        })
        cls.env['stock.move'].create({
            'name': cls.product.name,
            'product_id': cls.product.id,
            'product_uom_qty': 1,
            'product_uom': cls.product.uom_id.id,
            'picking_id': picking.id,
            'location_id': picking.location_id.id,
            'location_dest_id': picking.location_dest_id.id,
        })
        return picking

    @classmethod
    def _crear_usuario(cls, login, grupos, tipos_permitidos=None):
        valores = {
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'groups_id': [(6, 0, [grupo.id for grupo in grupos])],
        }
        if tipos_permitidos is not None:
            valores['restrict_picking_types'] = True
            valores['allowed_picking_type_ids'] = [
                (6, 0, tipos_permitidos.ids)]
        return cls.env['res.users'].with_context(
            no_reset_password=True).create(valores)

    def marcar_cfe_aceptado(self, picking, ucfe_state='00'):
        """Deja la operación como si tuviera un CFE aceptado por UCFE."""
        valores = {'cfe_emitido': True}
        if 'ucfe_state' in picking._fields:
            valores['ucfe_state'] = ucfe_state
        if 'cfe_state' in picking._fields:
            valores['cfe_state'] = '7'
        if 'cfe_serie_num' in picking._fields:
            valores['cfe_serie_num'] = '181-A-1001'
        picking.picking_type_id.uses_cfe = True
        # El tipo de CFE sale del tipo de documento LATAM. El compute que lo
        # calcula necesita punto de emisión, que acá no hay, así que se
        # asigna a mano un e-Remito ya existente en la base.
        if 'l10n_latam_document_type_id' in picking._fields:
            documento = self.env['l10n_latam.document.type'].search(
                [('dgi_cfe_type', '=', '181'),
                 ('internal_type', '=', 'stock_picking')],
                limit=1)
            if documento:
                valores['l10n_latam_document_type_id'] = documento.id
        picking.write(valores)

    def adjuntar_pdf_ucfe(self, picking, contenido=None):
        """Adjunta un PDF con el nombre que reconoce el módulo."""
        return self.env['ir.attachment'].create({
            'name': picking._remito_ucfe_nombre_archivo(),
            'type': 'binary',
            'datas': base64.b64encode(contenido or PDF_MINIMO),
            'mimetype': 'application/pdf',
            'res_model': 'stock.picking',
            'res_id': picking.id,
        })


class RemitoPrintCommon(RemitoPrintSetupMixin, TransactionCase):
    """Caso base para los tests que no necesitan HTTP."""

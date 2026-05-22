# Copyright 2024 PrimateUY
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import io
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductVariantImportWizard(models.TransientModel):
	_name        = "product.variant.import.wizard"
	_description = "Importar Variantes de Producto desde Excel"

	import_file      = fields.Binary(string="Archivo Excel (.xlsx)")
	import_file_name = fields.Char(string="Nombre del archivo")
	state            = fields.Selection(
		[("draft", "Borrador"), ("done", "Hecho")],
		default="draft", readonly=True,
	)
	job_id = fields.Many2one(
		"primate.variant.import.job", string="Resultado", readonly=True
	)
	# Campo relacionado para compatibilidad con vistas anteriores cacheadas en BD
	result_info = fields.Text(
		string="Resultado detallado",
		related="job_id.result_info",
		readonly=True,
	)

	# ── Plantilla ─────────────────────────────────────────────────────────────

	def action_download_template(self):
		try:
			import openpyxl
		except ImportError as exc:
			raise UserError(_("La librería openpyxl no está instalada.")) from exc

		wb = openpyxl.Workbook()
		ws = wb.active
		ws.title = "Variantes"
		ws.append([
			"Referencia Interna", "Product Template",
			"Atributo 1", "Valor Atributo 1",
			"Atributo 2", "Valor Atributo 2",
			"Atributo 3", "Valor Atributo 3",
		])
		ws.append([
			"PROD-VAR-001", "product.template_1",
			"Color", "Rojo", "Talla", "M", "", "",
		])
		ws_info = wb.create_sheet("Instrucciones")
		for row in [
			["Campo", "Descripcion"],
			["Referencia Interna", "Ref. interna (default_code). Opcional."],
			["Product Template",   "ID externo o nombre exacto del template."],
			["Atributo N",         "Nombre del atributo ya en el template."],
			["Valor Atributo N",   "Valor del atributo. Si no existe se crea automaticamente."],
			["", ""],
			["NOTAS:", ""],
			["1", "El template debe existir con sus atributos configurados."],
			["2", "Cada fila puede referenciar un template distinto."],
			["3", "Funciona con templates configurados como 'No crear automaticamente'."],
		]:
			ws_info.append(row)

		for sheet in [ws, ws_info]:
			for col in sheet.columns:
				max_len = max((len(str(c.value)) for c in col if c.value), default=10)
				sheet.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

		output = io.BytesIO()
		wb.save(output)
		output.seek(0)
		attachment = self.env["ir.attachment"].create({
			"name":     "plantilla_importacion_variantes.xlsx",
			"type":     "binary",
			"datas":    base64.b64encode(output.read()),
			"mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		})
		return {
			"type":   "ir.actions.act_url",
			"url":    f"/web/content/{attachment.id}?download=true",
			"target": "self",
		}

	# ── Importación sincrónica ────────────────────────────────────────────────

	def action_import(self):
		self.ensure_one()
		if not self.import_file:
			raise UserError(_("Por favor cargue un archivo Excel."))

		job = self.env["primate.variant.import.job"].create({
			"import_file":      self.import_file,
			"import_file_name": self.import_file_name or "importacion.xlsx",
			"user_id":          self.env.user.id,
		})
		job.process()
		self.write({"state": "done", "job_id": job.id})

		return {
			"type":      "ir.actions.act_window",
			"res_model": self._name,
			"res_id":    self.id,
			"view_mode": "form",
			"target":    "new",
			"context":   self.env.context,
		}

	def action_view_job(self):
		return self.job_id.action_view_result()

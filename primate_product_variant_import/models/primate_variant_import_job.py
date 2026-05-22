# Copyright 2024 PrimateUY
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

COL_INTERNAL_REF = "Referencia Interna"
COL_PRODUCT_TMPL = "Product Template"
COL_ATTR_PREFIX  = "Atributo"
COL_VAL_PREFIX   = "Valor Atributo"


class PrimateVariantImportJob(models.Model):
	_name        = "primate.variant.import.job"
	_description = "Job de Importación de Variantes"
	_order       = "create_date desc"

	name             = fields.Char(string="Referencia", readonly=True,
	                               default=lambda self: _("Nuevo"))
	import_file      = fields.Binary(string="Archivo Excel", readonly=True)
	import_file_name = fields.Char(string="Nombre del archivo", readonly=True)
	state            = fields.Selection(
		[("pending", "Pendiente"), ("running", "Procesando"),
		 ("done", "Completado"), ("error", "Error")],
		default="pending", readonly=True, string="Estado",
	)
	result_info    = fields.Text(string="Resultado",       readonly=True)
	user_id        = fields.Many2one("res.users", string="Usuario",
	                                 default=lambda self: self.env.user, readonly=True)
	create_date    = fields.Datetime(string="Fecha",           readonly=True)
	line_count     = fields.Integer(string="Filas procesadas", readonly=True)
	created_count  = fields.Integer(string="Creadas",          readonly=True)
	updated_count  = fields.Integer(string="Actualizadas",     readonly=True)
	error_count    = fields.Integer(string="Errores",          readonly=True)

	@api.model_create_multi
	def create(self, vals_list):
		for vals in vals_list:
			if vals.get("name", _("Nuevo")) == _("Nuevo"):
				vals["name"] = (
					self.env["ir.sequence"].next_by_code("primate.variant.import.job")
					or _("Nuevo")
				)
		return super().create(vals_list)

	# ── Punto de entrada ─────────────────────────────────────────────────────

	def process(self):
		"""
		Procesa el job de forma sincrónica.

		Usa un savepoint para aislar errores de base de datos: si algo falla
		dentro del procesamiento, PostgreSQL hace rollback al savepoint sin
		abortar la transacción completa, lo que permite escribir el estado
		de error sin que la transacción quede en estado fallido.
		"""
		self.ensure_one()
		self.write({"state": "running"})

		try:
			with self.env.cr.savepoint():
				rows, headers = self._parse_excel()
				results       = self._import_all(rows, headers)
				self._write_result(results)
		except Exception as exc:
			_logger.exception("Error en job %s", self.name)
			self.write({
				"state":       "error",
				"result_info": _("Error al procesar: %s") % str(exc),
			})

	# ── Parseo del Excel ─────────────────────────────────────────────────────

	def _parse_excel(self):
		try:
			import openpyxl
		except ImportError as exc:
			raise UserError(_("openpyxl no está instalada.")) from exc

		data = base64.b64decode(self.import_file)
		try:
			wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
		except Exception as exc:
			raise UserError(_("No se pudo abrir el archivo .xlsx:\n%s") % exc) from exc

		ws          = wb.active
		rows_iter   = ws.iter_rows(values_only=True)
		raw_headers = next(rows_iter, None)
		if raw_headers is None:
			raise UserError(_("El archivo está vacío."))

		headers = [str(h).strip() if h is not None else "" for h in raw_headers]
		for col in (COL_INTERNAL_REF, COL_PRODUCT_TMPL):
			if col not in headers:
				raise UserError(_("No se encontró la columna '%s'.") % col)

		rows = list(rows_iter)
		wb.close()
		return rows, headers

	# ── Detección de columnas ────────────────────────────────────────────────

	@staticmethod
	def _detect_attr_pairs(headers):
		pairs = []
		for i, h in enumerate(headers):
			if h.startswith(COL_ATTR_PREFIX):
				suffix   = h[len(COL_ATTR_PREFIX):].strip()
				expected = f"{COL_VAL_PREFIX} {suffix}".strip()
				if expected in headers:
					pairs.append((i, headers.index(expected)))
		return pairs

	# ── Importación en dos fases con SQL batch ───────────────────────────────

	def _import_all(self, rows, headers):
		idx_ref    = headers.index(COL_INTERNAL_REF)
		idx_tmpl   = headers.index(COL_PRODUCT_TMPL)
		attr_pairs = self._detect_attr_pairs(headers)

		parsed  = []
		results = []

		for row_num, row in enumerate(rows, start=2):
			if all(c is None or str(c).strip() == "" for c in row):
				continue
			internal_ref = str(row[idx_ref]).strip()  if row[idx_ref]  is not None else ""
			tmpl_key     = str(row[idx_tmpl]).strip() if row[idx_tmpl] is not None else ""
			if not tmpl_key:
				results.append({"row": row_num, "status": "error",
				                "msg": _("Fila %d: falta Product Template.") % row_num})
				continue
			av_map = {}
			for ia, iv in attr_pairs:
				a = str(row[ia]).strip() if row[ia] is not None else ""
				v = str(row[iv]).strip() if row[iv] is not None else ""
				if a and v:
					av_map[a] = v
			parsed.append({"row_num": row_num, "internal_ref": internal_ref,
			               "tmpl_key": tmpl_key, "av_map": av_map})

		if not parsed:
			return results

		# SQL: resolver templates
		tmpl_map = self._sql_resolve_templates([p["tmpl_key"] for p in parsed])

		valid_parsed = []
		for p in parsed:
			tid = tmpl_map.get(p["tmpl_key"])
			if not tid:
				results.append({"row": p["row_num"], "status": "error",
				                "msg": _("Fila %d: template '%s' no encontrado.")
				                       % (p["row_num"], p["tmpl_key"])})
			else:
				p["tmpl_id"] = tid
				valid_parsed.append(p)

		if not valid_parsed:
			return results

		tmpl_ids = list({p["tmpl_id"] for p in valid_parsed})

		# SQL: cargar líneas, valores y PTAVs
		line_map     = self._sql_load_attr_lines(tmpl_ids)
		all_line_ids = [v[0] for v in line_map.values()]
		val_in_line  = self._sql_load_values_in_lines(all_line_ids)
		attr_ids     = list({v[1] for v in line_map.values()})
		val_global   = self._sql_load_global_values(attr_ids)

		# Determinar valores a crear y vincular
		new_links  = {}
		row_errors = {}
		to_create  = []

		for p in valid_parsed:
			tmpl_id = p["tmpl_id"]
			p["resolved"] = {}

			for attr_name, val_name in p["av_map"].items():
				key_line = (tmpl_id, attr_name.lower())
				if key_line not in line_map:
					row_errors[p["row_num"]] = (
						_("Fila %d: atributo '%s' no está en el template '%s'.")
						% (p["row_num"], attr_name, p["tmpl_key"])
					)
					break
				line_id, attr_id = line_map[key_line]
				key_vl = (line_id, val_name.lower())
				key_vg = (attr_id, val_name.lower())

				if key_vl in val_in_line:
					p["resolved"][line_id] = val_in_line[key_vl]
				elif key_vg in val_global:
					val_id = val_global[key_vg]
					new_links.setdefault(line_id, set()).add(val_id)
					val_in_line[key_vl] = val_id
					p["resolved"][line_id] = val_id
				else:
					to_create.append((attr_id, val_name.strip()))
					p["resolved"][line_id] = ("PENDING", attr_id, val_name.strip())

		# ORM: crear valores de atributo faltantes en batch
		if to_create:
			unique_new = list({(aid, vn) for aid, vn in to_create})
			created = self.env["product.attribute.value"].create([
				{"attribute_id": aid, "name": vn} for aid, vn in unique_new
			])
			for rec, (aid, vn) in zip(created, unique_new):
				val_global[(aid, vn.lower())] = rec.id

		# Resolver PLACEHOLDERs
		for p in valid_parsed:
			if p["row_num"] in row_errors:
				continue
			for line_id, v in list(p["resolved"].items()):
				if isinstance(v, tuple) and v[0] == "PENDING":
					_, attr_id, vn = v
					val_id = val_global.get((attr_id, vn.lower()))
					if val_id:
						p["resolved"][line_id] = val_id
						new_links.setdefault(line_id, set()).add(val_id)

		# ORM: vincular valores a líneas en bulk (1 write por línea)
		for line_id, val_ids in new_links.items():
			self.env["product.template.attribute.line"].browse(line_id).write(
				{"value_ids": [(4, vid) for vid in val_ids]}
			)

		# SQL + ORM: asegurar PTAVs (soporta no_create_variants)
		ptav_map = self._sql_load_ptavs(tmpl_ids)
		ptav_map = self._ensure_ptavs(valid_parsed, line_map, ptav_map)

		# SQL: cargar variantes existentes
		variant_map = self._sql_load_variants(tmpl_ids)

		# FASE 2: crear / actualizar variantes
		for p in valid_parsed:
			row_num = p["row_num"]
			if row_num in row_errors:
				results.append({"row": row_num, "status": "error",
				                "msg": row_errors[row_num]})
				continue
			if not p["resolved"] and self._tmpl_has_attrs(p["tmpl_id"]):
				results.append({"row": row_num, "status": "error",
				                "msg": _("Fila %d: template tiene atributos pero no "
				                         "se detectaron pares Atributo/Valor.") % row_num})
				continue
			try:
				results.append(self._create_or_update_variant(
					p, line_map, ptav_map, variant_map
				))
			except Exception as exc:
				results.append({"row": row_num, "status": "error",
				                "msg": _("Fila %d: %s") % (row_num, exc)})

		return results

	# ── SQL helpers ──────────────────────────────────────────────────────────

	def _sql_resolve_templates(self, tmpl_keys):
		cr, result  = self.env.cr, {}
		names, ext_ids = [], []
		for k in set(tmpl_keys):
			(ext_ids if "." in k else names).append(k)
		if names:
			cr.execute(
				"SELECT name, id FROM product_template WHERE name = ANY(%s) AND active = true",
				[names]
			)
			result.update(dict(cr.fetchall()))
		if ext_ids:
			pairs = [k.split(".", 1) for k in ext_ids]
			cr.execute("""
				SELECT imd.module || '.' || imd.name, imd.res_id
				FROM ir_model_data imd
				WHERE imd.model = 'product.template'
				  AND (imd.module, imd.name) IN %s
			""", [tuple((m, n) for m, n in pairs)])
			result.update(dict(cr.fetchall()))
		return result

	def _sql_load_attr_lines(self, tmpl_ids):
		if not tmpl_ids:
			return {}
		self.env.cr.execute("""
			SELECT pal.id, pal.product_tmpl_id, pa.id, LOWER(pa.name)
			FROM product_template_attribute_line pal
			JOIN product_attribute pa ON pa.id = pal.attribute_id
			WHERE pal.product_tmpl_id = ANY(%s)
		""", [tmpl_ids])
		return {(r[1], r[3]): (r[0], r[2]) for r in self.env.cr.fetchall()}

	def _sql_load_values_in_lines(self, line_ids):
		if not line_ids:
			return {}
		self.env.cr.execute("""
			SELECT rel.product_template_attribute_line_id, LOWER(pav.name), pav.id
			FROM product_attribute_value_product_template_attribute_line_rel rel
			JOIN product_attribute_value pav ON pav.id = rel.product_attribute_value_id
			WHERE rel.product_template_attribute_line_id = ANY(%s)
		""", [line_ids])
		return {(r[0], r[1]): r[2] for r in self.env.cr.fetchall()}

	def _sql_load_global_values(self, attr_ids):
		if not attr_ids:
			return {}
		self.env.cr.execute(
			"SELECT attribute_id, LOWER(name), id FROM product_attribute_value WHERE attribute_id = ANY(%s)",
			[attr_ids]
		)
		return {(r[0], r[1]): r[2] for r in self.env.cr.fetchall()}

	def _sql_load_ptavs(self, tmpl_ids):
		if not tmpl_ids:
			return {}
		self.env.cr.execute("""
			SELECT product_tmpl_id, attribute_id, product_attribute_value_id, id
			FROM product_template_attribute_value
			WHERE product_tmpl_id = ANY(%s)
		""", [tmpl_ids])
		return {(r[0], r[1], r[2]): r[3] for r in self.env.cr.fetchall()}

	def _sql_load_variants(self, tmpl_ids):
		if not tmpl_ids:
			return {}
		self.env.cr.execute("""
			SELECT pp.id, pp.product_tmpl_id, pp.default_code,
			       COALESCE(
			           array_agg(pvc.product_template_attribute_value_id)
			           FILTER (WHERE pvc.product_template_attribute_value_id IS NOT NULL),
			           ARRAY[]::int[]
			       )
			FROM product_product pp
			LEFT JOIN product_variant_combination pvc ON pvc.product_product_id = pp.id
			WHERE pp.product_tmpl_id = ANY(%s) AND pp.active = true
			GROUP BY pp.id, pp.product_tmpl_id, pp.default_code
		""", [tmpl_ids])
		return {
			(r[1], frozenset(r[3])): (r[0], r[2])
			for r in self.env.cr.fetchall()
		}

	def _tmpl_has_attrs(self, tmpl_id):
		self.env.cr.execute(
			"SELECT 1 FROM product_template_attribute_line WHERE product_tmpl_id = %s LIMIT 1",
			[tmpl_id]
		)
		return bool(self.env.cr.fetchone())

	# ── Asegurar PTAVs (soporta no_create_variants) ──────────────────────────

	def _ensure_ptavs(self, valid_parsed, line_map, ptav_map):
		"""
		Si el template tiene 'No crearlas automáticamente', Odoo no genera
		los PTAVs al agregar valores. Los creamos explícitamente.
		"""
		to_create = []
		for p in valid_parsed:
			tmpl_id = p["tmpl_id"]
			for line_id, val_id in p["resolved"].items():
				if not isinstance(val_id, int):
					continue
				attr_id = next(
					(v[1] for k, v in line_map.items()
					 if v[0] == line_id and k[0] == tmpl_id), None
				)
				if attr_id and (tmpl_id, attr_id, val_id) not in ptav_map:
					to_create.append((tmpl_id, attr_id, val_id))

		if to_create:
			unique = list(set(to_create))
			created = self.env["product.template.attribute.value"].create([
				{"product_tmpl_id": t, "attribute_id": a, "product_attribute_value_id": v}
				for t, a, v in unique
			])
			for rec, (t, a, v) in zip(created, unique):
				ptav_map[(t, a, v)] = rec.id
				_logger.info("PTAV creado explícitamente: tmpl=%d attr=%d val=%d → %d", t, a, v, rec.id)

		return ptav_map

	# ── Crear / actualizar variante ──────────────────────────────────────────

	def _create_or_update_variant(self, p, line_map, ptav_map, variant_map):
		row_num, internal_ref = p["row_num"], p["internal_ref"]
		tmpl_id,  tmpl_key   = p["tmpl_id"],  p["tmpl_key"]

		ptav_ids = []
		for line_id, val_id in p["resolved"].items():
			if not isinstance(val_id, int):
				raise UserError(_("Valor no resuelto en línea %d.") % line_id)
			attr_id = next(
				(v[1] for k, v in line_map.items() if v[0] == line_id and k[0] == tmpl_id), None
			)
			ptav_id = ptav_map.get((tmpl_id, attr_id, val_id)) if attr_id else None
			if not ptav_id:
				raise UserError(
					_("PTAV no encontrado (tmpl=%d attr=%d val=%d).") % (tmpl_id, attr_id or 0, val_id)
				)
			ptav_ids.append(ptav_id)

		key = (tmpl_id, frozenset(ptav_ids))

		if key in variant_map:
			pid, dcode = variant_map[key]
			if internal_ref and dcode != internal_ref:
				self.env["product.product"].browse(pid).write({"default_code": internal_ref})
				variant_map[key] = (pid, internal_ref)
				return {"row": row_num, "status": "updated",
				        "msg": _("Fila %d: actualizada (ref: %s) → %s [id:%d]")
				               % (row_num, internal_ref, tmpl_key, pid)}
			return {"row": row_num, "status": "skipped",
			        "msg": _("Fila %d: sin cambios → %s [id:%d]") % (row_num, tmpl_key, pid)}

		vals = {
			"product_tmpl_id": tmpl_id,
			"product_template_attribute_value_ids": [(6, 0, ptav_ids)],
		}
		if internal_ref:
			vals["default_code"] = internal_ref

		new = self.env["product.product"].create(vals)
		variant_map[key] = (new.id, internal_ref)
		return {"row": row_num, "status": "created",
		        "msg": _("Fila %d: creada (ref: %s) → %s [id:%d]")
		               % (row_num, internal_ref or "(sin ref)", tmpl_key, new.id)}

	# ── Resultado ────────────────────────────────────────────────────────────

	def _write_result(self, results):
		created = sum(1 for r in results if r["status"] == "created")
		updated = sum(1 for r in results if r["status"] == "updated")
		skipped = sum(1 for r in results if r["status"] == "skipped")
		errors  = sum(1 for r in results if r["status"] == "error")
		lines   = [
			_("=== RESULTADO ==="),
			_("✔ Creadas:      %d") % created,
			_("↺ Actualizadas: %d") % updated,
			_("— Sin cambios:  %d") % skipped,
			_("✘ Errores:      %d") % errors,
			"", _("DETALLE:"),
		] + [r["msg"] for r in results]
		self.write({
			"state":         "done" if not errors else "error",
			"result_info":   "\n".join(lines),
			"line_count":    len(results),
			"created_count": created,
			"updated_count": updated,
			"error_count":   errors,
		})

	def action_view_result(self):
		return {"type": "ir.actions.act_window", "res_model": self._name,
		        "res_id": self.id, "view_mode": "form", "target": "current"}

# Copyright 2024 PrimateUY
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import io
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Columnas fijas del Excel
COL_INTERNAL_REF = "Referencia Interna"
COL_PRODUCT_TMPL = "Product Template"

# Prefijos de las columnas dinámicas de atributos
COL_ATTR_PREFIX = "Atributo"
COL_VAL_PREFIX = "Valor Atributo"


class ProductVariantImportWizard(models.TransientModel):
    _name = "product.variant.import.wizard"
    _description = "Importar Variantes de Producto desde Excel"

    # ── Campos del wizard ──────────────────────────────────────────────────

    import_file = fields.Binary(
        string="Archivo Excel (.xlsx)",
        required=True,
        help="Suba el archivo Excel con las variantes a importar.",
    )
    import_file_name = fields.Char(string="Nombre del archivo")

    # Resultado
    result_info = fields.Text(string="Resultado", readonly=True)
    state = fields.Selection(
        [("draft", "Borrador"), ("done", "Hecho")],
        default="draft",
        readonly=True,
    )

    # ── Generación del Excel de plantilla ──────────────────────────────────

    def action_download_template(self):
        """Genera y descarga un Excel de ejemplo con la estructura requerida."""
        try:
            import openpyxl
        except ImportError as exc:
            raise UserError(
                _(
                    "La librería openpyxl no está instalada. "
                    "Ejecute: pip install openpyxl"
                )
            ) from exc

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Variantes"

        # Encabezados de ejemplo con 3 pares atributo/valor
        headers = [
            COL_INTERNAL_REF,
            COL_PRODUCT_TMPL,
            f"{COL_ATTR_PREFIX} 1",
            f"{COL_VAL_PREFIX} 1",
            f"{COL_ATTR_PREFIX} 2",
            f"{COL_VAL_PREFIX} 2",
            f"{COL_ATTR_PREFIX} 3",
            f"{COL_VAL_PREFIX} 3",
        ]
        ws.append(headers)

        # Fila de ejemplo
        ws.append(
            [
                "PROD-VAR-001",
                "product.template_1",  # ID externo o nombre
                "Color",
                "Rojo",
                "Talla",
                "M",
                "",
                "",
            ]
        )

        # Hoja de instrucciones
        ws_info = wb.create_sheet("Instrucciones")
        instrucciones = [
            ["Campo", "Descripción"],
            [
                COL_INTERNAL_REF,
                "Referencia interna (default_code) de la variante. "
                "Puede estar vacía.",
            ],
            [
                COL_PRODUCT_TMPL,
                "ID externo (ej: product.template_1) o nombre exacto "
                "del Product Template ya creado en Odoo.",
            ],
            [
                f"{COL_ATTR_PREFIX} N",
                "Nombre del atributo ya configurado en el template "
                "(ej: Color, Talla).",
            ],
            [
                f"{COL_VAL_PREFIX} N",
                "Nombre del valor del atributo "
                "(ej: Rojo, Azul). Debe existir en el template.",
            ],
            ["", ""],
            ["NOTAS:", ""],
            [
                "1",
                "El Product Template debe existir y tener los atributos "
                "ya agregados.",
            ],
            [
                "2",
                "Los valores de atributo deben estar en las líneas de "
                "atributos del template.",
            ],
            [
                "3",
                "Puede agregar tantos pares Atributo N / Valor Atributo N "
                "como necesite.",
            ],
            [
                "4",
                "Si la variante ya existe (misma combinación de valores), "
                "se actualizará su referencia interna.",
            ],
            [
                "5",
                "Deje en blanco las columnas de atributo/valor que no apliquen.",
            ],
        ]
        for row in instrucciones:
            ws_info.append(row)

        # Auto-ajustar anchos
        for sheet in [ws, ws_info]:
            for col in sheet.columns:
                max_len = 0
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        if cell.value:
                            max_len = max(max_len, len(str(cell.value)))
                    except Exception:
                        pass
                sheet.column_dimensions[col_letter].width = min(max_len + 4, 60)

        # Guardar en memoria y devolver como descarga
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        file_data = base64.b64encode(output.read())

        attachment = self.env["ir.attachment"].create(
            {
                "name": "plantilla_importacion_variantes.xlsx",
                "type": "binary",
                "datas": file_data,
                "mimetype": "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet",
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    # ── Lógica principal de importación ────────────────────────────────────

    def action_import(self):
        """Lee el Excel y crea/actualiza las variantes de producto."""
        self.ensure_one()
        if not self.import_file:
            raise UserError(_("Por favor cargue un archivo Excel."))

        rows, headers = self._parse_excel()
        results = self._process_rows(rows, headers)
        self._write_result(results)

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": self.env.context,
        }

    def _parse_excel(self):
        """Descodifica el binario y devuelve (rows, headers)."""
        try:
            import openpyxl
        except ImportError as exc:
            raise UserError(
                _(
                    "La librería openpyxl no está instalada. "
                    "Ejecute: pip install openpyxl"
                )
            ) from exc

        file_data = base64.b64decode(self.import_file)
        try:
            wb = openpyxl.load_workbook(
                io.BytesIO(file_data), read_only=True, data_only=True
            )
        except Exception as exc:
            raise UserError(
                _("No se pudo abrir el archivo. ¿Es un archivo .xlsx válido?\n%s")
                % str(exc)
            ) from exc

        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)

        # Primera fila = encabezados
        try:
            raw_headers = next(rows_iter)
        except StopIteration as exc:
            raise UserError(_("El archivo está vacío.")) from exc

        headers = [str(h).strip() if h is not None else "" for h in raw_headers]

        # Validar columnas obligatorias
        if COL_INTERNAL_REF not in headers:
            raise UserError(
                _(
                    "No se encontró la columna '%s'. "
                    "Verifique la plantilla."
                )
                % COL_INTERNAL_REF
            )
        if COL_PRODUCT_TMPL not in headers:
            raise UserError(
                _(
                    "No se encontró la columna '%s'. "
                    "Verifique la plantilla."
                )
                % COL_PRODUCT_TMPL
            )

        rows = list(rows_iter)
        wb.close()
        return rows, headers

    def _process_rows(self, rows, headers):
        """Procesa cada fila del Excel y devuelve una lista de dicts con resultados."""
        results = []
        idx_ref = headers.index(COL_INTERNAL_REF)
        idx_tmpl = headers.index(COL_PRODUCT_TMPL)

        # Detectar pares de atributo/valor dinámicamente
        attr_pairs = self._detect_attribute_pairs(headers)

        for row_num, row in enumerate(rows, start=2):
            # Saltear filas completamente vacías
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue

            internal_ref = (
                str(row[idx_ref]).strip()
                if row[idx_ref] is not None
                else ""
            )
            tmpl_key = (
                str(row[idx_tmpl]).strip()
                if row[idx_tmpl] is not None
                else ""
            )

            if not tmpl_key:
                results.append(
                    {
                        "row": row_num,
                        "status": "error",
                        "msg": _("Fila %d: falta el Product Template.") % row_num,
                    }
                )
                continue

            try:
                result = self._import_variant_row(
                    row_num=row_num,
                    internal_ref=internal_ref,
                    tmpl_key=tmpl_key,
                    attr_pairs=attr_pairs,
                    row=row,
                    headers=headers,
                )
                results.append(result)
            except (UserError, ValidationError) as exc:
                results.append(
                    {
                        "row": row_num,
                        "status": "error",
                        "msg": _("Fila %d: %s") % (row_num, exc.args[0]),
                    }
                )
            except Exception as exc:
                _logger.exception("Error inesperado en fila %d", row_num)
                results.append(
                    {
                        "row": row_num,
                        "status": "error",
                        "msg": _("Fila %d: error inesperado: %s")
                        % (row_num, str(exc)),
                    }
                )

        return results

    def _detect_attribute_pairs(self, headers):
        """
        Detecta los pares (índice_atributo, índice_valor) a partir de los encabezados.
        Soporta columnas tipo:
          'Atributo 1' / 'Valor Atributo 1'
          'Atributo 2' / 'Valor Atributo 2'  …etc.
        """
        pairs = []
        for i, h in enumerate(headers):
            if h.startswith(COL_ATTR_PREFIX) and h != COL_INTERNAL_REF:
                # Extraer sufijo numérico: "Atributo 1" → "1"
                suffix = h[len(COL_ATTR_PREFIX):].strip()
                expected_val_col = f"{COL_VAL_PREFIX} {suffix}".strip()
                if expected_val_col in headers:
                    j = headers.index(expected_val_col)
                    pairs.append((i, j))
        return pairs

    def _find_product_template(self, tmpl_key):
        """
        Busca el product template por:
          1. ID externo (ej: 'product.template_1' o '__export__.product_template_42')
          2. Nombre exacto
        """
        ProductTemplate = self.env["product.template"]

        # Intentar como ID externo
        if "." in tmpl_key:
            try:
                record = self.env.ref(tmpl_key, raise_if_not_found=False)
                if record and record._name == "product.template":
                    return record
            except Exception:
                pass

        # Intentar por nombre exacto
        template = ProductTemplate.search([("name", "=", tmpl_key)], limit=1)
        if template:
            return template

        raise UserError(
            _(
                "No se encontró el Product Template '%s'. "
                "Verifique el ID externo o el nombre."
            )
            % tmpl_key
        )

    def _import_variant_row(
        self, row_num, internal_ref, tmpl_key, attr_pairs, row, headers
    ):
        """Procesa una fila individual y crea/actualiza la variante."""
        template = self._find_product_template(tmpl_key)

        # Recopilar pares atributo/valor de la fila (ignorar vacíos)
        attr_value_map = {}  # {nombre_atributo: nombre_valor}
        for idx_attr, idx_val in attr_pairs:
            attr_name = (
                str(row[idx_attr]).strip()
                if row[idx_attr] is not None
                else ""
            )
            val_name = (
                str(row[idx_val]).strip()
                if row[idx_val] is not None
                else ""
            )
            if attr_name and val_name:
                attr_value_map[attr_name] = val_name

        # Resolver los product.template.attribute.value (ptav)
        ptav_ids = self._resolve_ptav_ids(template, attr_value_map, row_num)


        # Si el template tiene atributos pero no se detectó ninguno en la fila, error
        if not ptav_ids and template.attribute_line_ids:
            raise UserError(
                _(
                    "El template '%s' tiene atributos configurados pero no se "
                    "encontraron pares Atributo/Valor en la fila. "
                    "Verifique que las columnas se llamen exactamente "
                    "'Atributo 1', 'Valor Atributo 1', etc., "
                    "y que las celdas no estén vacías."
                )
                % template.name
            )
        # Buscar variante existente con exactamente esos valores
        existing = self._find_existing_variant(template, ptav_ids)

        if existing:
            # Actualizar referencia interna si se proporcionó
            if internal_ref and existing.default_code != internal_ref:
                existing.write({"default_code": internal_ref})
                return {
                    "row": row_num,
                    "status": "updated",
                    "msg": _(
                        "Fila %d: variante actualizada (ref: %s) → %s [%s]"
                    )
                    % (row_num, internal_ref, template.name, existing.id),
                }
            return {
                "row": row_num,
                "status": "skipped",
                "msg": _(
                    "Fila %d: variante ya existe, sin cambios → %s [%s]"
                )
                % (row_num, template.name, existing.id),
            }

        # Crear nueva variante
        vals = {
            "product_tmpl_id": template.id,
            "product_template_attribute_value_ids": [(6, 0, ptav_ids)],
        }
        if internal_ref:
            vals["default_code"] = internal_ref

        new_variant = self.env["product.product"].create(vals)
        return {
            "row": row_num,
            "status": "created",
            "msg": _(
                "Fila %d: variante creada (ref: %s) → %s [%s]"
            )
            % (row_num, internal_ref or "(sin ref)", template.name, new_variant.id),
        }

    def _resolve_ptav_ids(self, template, attr_value_map, row_num):
        """
        Dado un dict {nombre_atributo: nombre_valor}, devuelve la lista de IDs
        de product.template.attribute.value correspondientes al template.

        Requisitos:
         - El atributo DEBE existir en las líneas del template (error si no).
         - El valor puede NO existir: se crea y se agrega a la línea del template.
           Odoo genera el PTAV automáticamente al agregar el valor.
        """
        if not attr_value_map:
            return []

        AttributeValue = self.env["product.attribute.value"]
        ptav_obj = self.env["product.template.attribute.value"]
        ptav_ids = []

        for attr_name, val_name in attr_value_map.items():

            # 1. Buscar línea de atributo en el template
            attr_line = template.attribute_line_ids.filtered(
                lambda l, a=attr_name: l.attribute_id.name.strip().lower()
                == a.strip().lower()
            )
            if not attr_line:
                raise UserError(
                    _(
                        "El atributo '%s' no está configurado en el template '%s'. "
                        "Agréguelo al template antes de importar."
                    )
                    % (attr_name, template.name)
                )
            if len(attr_line) > 1:
                attr_line = attr_line[0]

            attribute = attr_line.attribute_id

            # 2. Buscar el valor en la línea; si no existe, crearlo
            attr_value = attr_line.value_ids.filtered(
                lambda v, vn=val_name: v.name.strip().lower() == vn.strip().lower()
            )

            if not attr_value:
                # Buscar si el valor ya existe globalmente para este atributo
                attr_value = AttributeValue.search(
                    [
                        ("attribute_id", "=", attribute.id),
                        ("name", "=ilike", val_name.strip()),
                    ],
                    limit=1,
                )
                if not attr_value:
                    attr_value = AttributeValue.create(
                        {
                            "attribute_id": attribute.id,
                            "name": val_name.strip(),
                        }
                    )
                    _logger.info(
                        "Creado nuevo valor de atributo: %s = %s",
                        attr_name,
                        val_name,
                    )

                # Agregar el valor a la línea de atributo del template
                attr_line.write({"value_ids": [(4, attr_value.id)]})
                _logger.info(
                    "Valor '%s' agregado a la línea '%s' del template '%s'",
                    val_name, attr_name, template.name,
                )

            if len(attr_value) > 1:
                attr_value = attr_value[0]

            # 3. Buscar el PTAV (Odoo lo genera al agregar el valor)
            ptav = ptav_obj.search(
                [
                    ("product_tmpl_id", "=", template.id),
                    ("attribute_id", "=", attribute.id),
                    ("product_attribute_value_id", "=", attr_value.id),
                ],
                limit=1,
            )
            if not ptav:
                raise UserError(
                    _(
                        "No se pudo obtener el vínculo interno (PTAV) para "
                        "'%s' = '%s' en '%s' luego de agregar el valor. "
                        "Contacte al administrador."
                    )
                    % (attr_name, val_name, template.name)
                )

            ptav_ids.append(ptav.id)

        return ptav_ids

    def _find_existing_variant(self, template, ptav_ids):
        """
        Busca una variante del template que tenga exactamente los ptav_ids indicados.
        """
        if not ptav_ids:
            # Sin atributos: buscar variante sin valores de atributo
            variants = template.product_variant_ids
            for v in variants:
                if not v.product_template_attribute_value_ids:
                    return v
            return False

        domain = [("product_tmpl_id", "=", template.id)]
        for ptav_id in ptav_ids:
            domain.append(("product_template_attribute_value_ids", "=", ptav_id))

        candidates = self.env["product.product"].search(domain)
        for variant in candidates:
            if len(variant.product_template_attribute_value_ids) == len(ptav_ids):
                return variant
        return False

    def _write_result(self, results):
        """Formatea y escribe el resumen de la importación."""
        if not results:
            summary = _("No se procesaron filas.")
        else:
            created = sum(1 for r in results if r["status"] == "created")
            updated = sum(1 for r in results if r["status"] == "updated")
            skipped = sum(1 for r in results if r["status"] == "skipped")
            errors = sum(1 for r in results if r["status"] == "error")

            lines = [
                _("=== RESULTADO DE IMPORTACIÓN ==="),
                _("✔ Creadas:     %d") % created,
                _("↺ Actualizadas: %d") % updated,
                _("— Sin cambios: %d") % skipped,
                _("✘ Errores:     %d") % errors,
                "",
                _("DETALLE:"),
            ]
            for r in results:
                lines.append(r["msg"])

            summary = "\n".join(lines)

        self.write({"result_info": summary, "state": "done"})

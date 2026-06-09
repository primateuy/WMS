# Backfill de wis.webhook.log.codigo_unico para los logs históricos (creados antes de que
# existiera el campo). Parsea el request/respuesta ya guardado y extrae el/los código(s) WMS.
# Idempotente: solo toca registros con codigo_unico vacío.
import json
import re
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Mismas claves/regex que _extraer_codigo_wis del controller (case-insensitive).
_CLAVES = {'pedido', 'nropedido', 'codigounico', 'referencia', 'numeroreferencia', 'serializado'}
_PATRON = re.compile(
    r'"(?:pedido|nroPedido|codigoUnico|referencia|numeroReferencia|serializado)"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)


def _extraer_codigo_wis(*fuentes):
    encontrados = []

    def add(val):
        val = str(val).strip()
        if val and val not in encontrados:
            encontrados.append(val)

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if (isinstance(k, str) and k.lower() in _CLAVES
                        and isinstance(v, (str, int)) and str(v).strip()):
                    add(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for fuente in fuentes:
        if isinstance(fuente, str):
            try:
                walk(json.loads(fuente))
            except Exception:
                for m in _PATRON.findall(fuente):
                    add(m)
        else:
            walk(fuente)
    return ', '.join(encontrados)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    logs = env['wis.webhook.log'].search(
        ['|', ('codigo_unico', '=', False), ('codigo_unico', '=', '')]
    )
    actualizados = 0
    for log in logs:
        codigo = _extraer_codigo_wis(log.request or '', log.respuesta or '')
        if codigo:
            log.codigo_unico = codigo
            actualizados += 1
    _logger.info(
        "[WIS] backfill wis.webhook.log.codigo_unico: %s/%s logs actualizados.",
        actualizados, len(logs),
    )

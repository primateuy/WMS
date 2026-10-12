# Cola de operaciones: motivo de la falla, LogID de WIS y espera tras un timeout.
#
# Hasta 0.9.1 una operación que daba timeout se reenviaba en la corrida siguiente, segundos
# después; en support (12-10-2026) una recepción de 1000 líneas llegó 16 veces a WIS. Las
# que quedaron pendientes por timeout pasan a «sin confirmar» y esperan antes de reenviarse:
# WIS probablemente ya las procesó. Las que tienen error guardan el LogID para pasárselo a WIS.
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE wis_picking_cola
           SET tipo_error = CASE
                   WHEN ultimo_error ILIKE '%%timed out%%' THEN 'sin_confirmar'
                   WHEN ultimo_error ILIKE '%%Esperando la integración%%' THEN 'espera'
                   ELSE 'datos' END,
               log_id = substring(ultimo_error from '"LogI[Dd]"\\s*:\\s*"([^"]+)"')
         WHERE estado IN ('pendiente', 'error') AND ultimo_error IS NOT NULL
    """)
    _logger.info("integracion_wis 0.9.2: %s entradas de la cola de operaciones con motivo.",
                 cr.rowcount)
    cr.execute("""
        UPDATE wis_picking_cola
           SET proximo_intento = now() at time zone 'UTC' + interval '30 minutes'
         WHERE estado = 'pendiente' AND tipo_error = 'sin_confirmar'
    """)
    _logger.info("integracion_wis 0.9.2: %s operaciones sin confirmar esperan 30 min "
                 "antes de reenviarse.", cr.rowcount)

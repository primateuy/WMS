# Cola de productos: el estado «Error» pasa a ser Pendiente con subestado «Con error».
#
# Antes una variante rechazada por WIS terminaba en «Error» —y la cola no la volvía a
# tomar— o directamente en «Integrado». Ahora queda Pendiente con error, en rojo, y la
# cola la reintenta antes que lo que todavía no se mandó.
#
# Las «Integradas» con un error guardado se dejan como están: las que se encontraron en
# support (12-10-2026) se habían vuelto a mandar bien después, con el botón.
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE wis_sync_queue
           SET estado = 'pendiente', subestado = 'error', intentos = 0,
               proximo_intento = NULL
         WHERE estado = 'error'
    """)
    _logger.info("[WIS] Cola de productos: %s entradas en Error pasan a Pendiente con error.",
                 cr.rowcount)
    cr.execute("UPDATE wis_sync_queue SET subestado = 'sin_enviar' WHERE subestado IS NULL")

    # El tope de reintentos automáticos pasa de 3 a 5; se respeta un valor cambiado a mano.
    cr.execute("""
        UPDATE integracion_wis_integracion_wis SET cola_max_intentos = 5
         WHERE cola_max_intentos IS NULL OR cola_max_intentos = 3
    """)

# -*- coding: utf-8 -*-
"""Desmarca el «valor propio» del múltiplo que puso el bug de propagación.

Hasta la 0.3, la plantilla escribía el múltiplo variante por variante y cada escritura
marcaba la variante como «valor propio» (`mutiplos_distribucion_override`). Desde el
segundo cambio, la plantilla dejaba de propagarles. Se desmarcan SOLO las que tienen el
mismo valor que su plantilla: ésas no tienen un valor propio, tienen el de la plantilla.
Una variante con un múltiplo distinto se respeta.

Por SQL: es un booleano sin dependientes y en la base hay miles de filas.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE product_product pp
           SET mutiplos_distribucion_override = FALSE
          FROM product_template pt
         WHERE pt.id = pp.product_tmpl_id
           AND pp.mutiplos_distribucion_override
           AND pp.mutiplos_distribucion = pt.mutiplos_distribucion
    """)
    _logger.info("Múltiplos: %s variante(s) vuelven a tomar el valor de su plantilla.",
                 cr.rowcount)

# Importación de Operaciones de Inventario desde Excel (`primate_stock_import`)

Importa operaciones de inventario (`stock.picking`) desde un archivo Excel y las
genera en estado **borrador**, con validación previa, log por sesión y una acción
masiva para comprobar disponibilidad.

## Flujo

```
Excel (.xlsx / .xls)
  → Asistente: Validar        (parseo + resolución de ids + errores/advertencias por fila)
  → Asistente: Importar       (crea stock.picking + stock.move en borrador)
  → Lista de operaciones → acción "Comprobar disponibilidad (importadas)"
```

Menú: **Inventario › Operaciones › Importar operaciones desde Excel** y
**Inventario › Operaciones › Sesiones de importación**.

Grupo de acceso: *Importación de operaciones desde Excel* (implica Usuario de
inventario; los Responsables de inventario lo tienen por defecto).

## Formato del archivo

Primera hoja, encabezados en la fila 1 (sin distinguir mayúsculas). Una fila por
línea de movimiento; las filas con la misma `operation_ref` forman una operación.
La primera fila de cada grupo define el cabezal y el resto debe repetirlo.

| Columna | Oblig. | Descripción |
|---|---|---|
| `operation_ref` | ✔ | Identificador libre de la operación (agrupa filas). |
| `picking_type` | ✔ | Nombre exacto, `Almacén: Nombre` o código de secuencia (`IN`, `OUT`, `INT`). |
| `warehouse` | | Almacén, para desambiguar tipos con el mismo nombre. |
| `partner` | | Contacto por RUT o nombre exacto. |
| `location_origin` | | Ubicación por nombre completo (`WH/Stock`), nombre corto o código de barras. Default: la del tipo de operación → la del contacto → proveedores/clientes genéricas (misma cascada que `stock.picking`). |
| `location_dest` | | Ídem para destino. |
| `scheduled_date` | | `AAAA-MM-DD`, `AAAA-MM-DD HH:MM` o `DD/MM/AAAA`, en la zona horaria del usuario. |
| `origin` | | Documento origen. Default: `operation_ref`. |
| `product` | ✔ | Referencia interna o código de barras de la variante. |
| `lot` | | Lote/serie. Ver *Limitaciones*. |
| `quantity` | ✔ | Cantidad > 0. Acepta coma o punto decimal. |
| `uom` | | UdM de la misma categoría que la del producto. Default: la del producto. |

La plantilla (con hoja de instrucciones y filas de ejemplo) se descarga desde el
asistente.

## Reglas de validación

- **Errores (bloquean la importación):** columna obligatoria vacía, producto /
  tipo de operación / ubicación / contacto / UdM inexistente o ambiguo, cantidad
  no numérica o ≤ 0, fecha inválida, UdM de otra categoría, origen igual a
  destino, cabezal inconsistente dentro de un grupo, cantidad ≠ 1 en productos
  con número de serie.
- **Advertencias (no bloquean):** producto con seguimiento sin lote, lote que no
  existe, lote informado en producto sin seguimiento.

Con errores no se crea nada; la sesión queda en estado *Con errores* con el log.

## Sesiones de importación

Cada validación crea una sesión (`primate_stock_import.session`) con el archivo
original, el resultado de la validación (ya resuelto a ids), el log por fila y
las operaciones creadas. La importación no vuelve a leer el archivo: usa el
resultado guardado.

## Acción masiva

En la lista de operaciones, *Acción › Comprobar disponibilidad (importadas)*
ejecuta `action_assign()` (que confirma los borradores) picking por picking dentro
de un savepoint: los que fallan quedan anotados en su chatter y no bloquean al
resto. Las operaciones hechas o canceladas se omiten.

## Limitaciones (v1)

- **Lotes:** la columna `lot` se valida y se anota en la descripción del
  movimiento, pero no se asigna. Crear `stock.move.line` con lote sobre un
  borrador fuerza la reserva y saltea la comprobación de disponibilidad, por lo
  que se dejó para una versión posterior.
- Solo se lee la primera hoja del archivo.
- Archivos muy grandes (>5000 filas) se procesan en la misma petición.

## Dependencias

Solo `stock`. La lectura usa `xlrd` (1.2.x) y la plantilla `xlsxwriter`, ambos
requisitos estándar de Odoo 17.

## Tests

```
odoo-bin -c forum.conf -d <db> -i primate_stock_import --test-enable --test-tags /primate_stock_import --stop-after-init
```

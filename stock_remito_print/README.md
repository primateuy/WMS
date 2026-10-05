# Impresión masiva de remitos (`stock_remito_print`)

Imprime o descarga remitos de a muchos desde cualquier lista de operaciones
de inventario, procesando los registros **por lotes** para no sobrecargar el
servidor.

Para cada operación elige el PDF del CFE emitido por UCFE cuando corresponde,
y el reporte estándar de Odoo cuando no.

> **No depende de la facturación electrónica.** El módulo instala y funciona
> en una base sin ningún `l10n_uy_einvoice_*`: ahí siempre imprime el reporte
> estándar. La integración con UCFE se detecta en tiempo de ejecución.

---

## Cómo se elige el PDF de cada operación

En este orden:

| # | Situación | Origen |
|---|---|---|
| 1 | CFE aceptado y el PDF ya está adjunto | `ucfe` |
| 2 | CFE aceptado y el PDF no está adjunto → se le pide a UCFE y se adjunta | `ucfe` |
| 3 | CFE aceptado y falla la obtención | `error`, o `standard_fallback` si está habilitado el fallback |
| 4 | El tipo de operación no emite CFE, o no hay CFE | `standard` |

**CFE aceptado** quiere decir `cfe_emitido = True` y `ucfe_state` en `00`
(recibido por DGI) u `11` (procesado por UCFE, esperando DGI). Para las
operaciones viejas sin `ucfe_state`, y para las bases donde solo está
instalado `l10n_uy_einvoice_base`, se usa `cfe_state` en `7` o `5`, que son
los equivalentes.

El PDF que llega de UCFE se adjunta como `eRemito_<serie>.pdf` y no se vuelve
a pedir. También se reconoce el adjunto que deja
`l10n_uy_einvoice_uruware`, que lo guarda con el nombre de la operación.

### Reporte estándar por tipo de operación

En **Inventario → Configuración → Tipos de operación** hay un campo
**Reporte de remito**. Si queda vacío se usa el albarán de entrega estándar
(`stock.action_report_delivery`).

---

## Uso

### Desde cualquier lista de operaciones

Seleccionar las operaciones → **Acciones → Imprimir remitos**.

En el asistente se elige:

- **Modo**: *Imprimir* (abre el diálogo de impresión por cada lote) o
  *Descargar* (baja un PDF por lote).
- **Marcar como impresas**: marca cada operación a medida que su PDF entra
  en el lote. Activado por defecto.
- **Tamaño de lote**: cuántas operaciones se procesan por pedido al
  servidor.

Al confirmar se abre una pantalla de progreso que procesa los lotes **de a
uno**: no pide el siguiente hasta terminar el anterior. Se puede cancelar en
cualquier momento; el lote en curso termina y los siguientes no se procesan.

Al final muestra el resumen: cuántas salieron con PDF de UCFE, cuántas con
reporte estándar y cuáles fallaron, con el motivo de cada una.

### Desde el menú Remitos

El menú raíz **Remitos** trae una lista filtrada por defecto en
*Hecho + No impresas*, con el botón **Imprimir remitos** en la cabecera
(aparece al seleccionar registros).

### Marcado manual

Dos acciones de servidor sobre las operaciones: **Marcar como impresa** y
**Marcar como no impresa**. Las dos dejan constancia en el chatter.

Desmarcar conserva la cantidad de impresiones y el origen del último PDF,
que son el historial de lo que realmente se imprimió.

---

## Restringir tipos de operación por usuario

En el formulario del usuario, pestaña **Inventario / Remitos** (visible para
Administración de ajustes o Administrador de inventario):

- **Restringir tipos de operación**
- **Tipos de operación permitidos**

Con la restricción activada el usuario solo ve las operaciones, los tipos de
operación y los movimientos de los tipos que tenga asignados. Si la
restricción está activada la lista no puede quedar vacía.

Se aplica con cuatro reglas de registro **globales**, que no recortan nada
mientras el usuario no tenga la restricción activada:

| Modelo | Permisos de la regla |
|---|---|
| `stock.picking` | lectura y escritura |
| `stock.picking.type` | lectura |
| `stock.move` | lectura |
| `stock.move.line` | lectura |

En movimientos la regla deja pasar los que **no** tienen operación asociada:
valorización, producción y ajustes de inventario trabajan con movimientos
sueltos y cortarlos rompería esos procesos.

Los procesos internos que corren como superusuario o con `sudo()` (crons,
POS, integraciones) no se ven afectados: las reglas de registro no aplican a
`sudo`.

> Al cambiar estos campos se invalida la caché de reglas
> (`registry.clear_cache()`), así que el cambio tiene efecto inmediato sin
> reiniciar el servidor.

---

## Perfil "Impresión de remitos (solo)"

Grupo `group_remito_print_only`, pensado para operadores logísticos
externos. Hereda **Usuario interno** pero **no** Usuario de inventario, así
que no ve la app Inventario.

Puede: listar las operaciones de sus tipos, imprimir sus remitos y marcarlos
como impresos.

No puede: crear, escribir, validar ni borrar operaciones.

Para los usuarios de este grupo la restricción de tipos de operación es
**obligatoria**: sin ella verían todas las operaciones de la empresa.

> **Limitación conocida.** El grupo hereda `base.group_user`, que en Odoo
> habilita los menús que ve cualquier usuario interno (Conversaciones,
> Contactos, Calendario, Aplicaciones, y los de los demás módulos
> instalados). Esos menús no se pueden ocultar para un solo grupo sin
> modificar los menús del core, que son compartidos. Si hace falta cerrar
> más el perfil, hay que planificarlo aparte.

---

## Parámetros de sistema

| Parámetro | Por defecto | Qué hace |
|---|---|---|
| `stock_remito_print.batch_size` | `20` | Operaciones por lote |
| `stock_remito_print.max_records` | `500` | Tope de operaciones por ejecución |
| `stock_remito_print.fallback_on_ucfe_error` | `False` | Si es `True`, ante una falla de UCFE imprime el reporte estándar y lo registra en el chatter. En `False` la operación se informa como error y **no** se marca como impresa, para poder reintentarla |
| `stock_remito_print.ucfe_timeout` | `20` | Segundos de espera para la llamada a UCFE |

---

## Impresión directa sin diálogo

En modo *Imprimir* el navegador abre su diálogo de impresión una vez por
lote. En las PC del depósito, donde se imprime siempre a la misma impresora,
conviene arrancar Chrome con `--kiosk-printing`: con esa opción el diálogo
no aparece y cada lote sale directo a la impresora predeterminada.

**Windows** — en las propiedades del acceso directo de Chrome, campo
*Destino*:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --kiosk-printing
```

**Linux**

```sh
google-chrome --kiosk-printing
```

**macOS**

```sh
open -a "Google Chrome" --args --kiosk-printing
```

Conviene además fijar la impresora predeterminada del sistema y, en el
diálogo de impresión de Chrome, dejar configurados márgenes y escala una
primera vez: Chrome los recuerda.

---

## Detalles técnicos

### Procesamiento por lotes

La acción cliente OWL `stock_remito_print.batch` llama al controlador
`POST /stock_remito_print/batch` una vez por lote y espera la
respuesta antes de pedir el siguiente. Nunca hay más de un lote en vuelo por
usuario, y solo se leen los binarios del lote en curso.

El controlador:

- Valida el acceso de lectura con el usuario actual (las reglas de registro
  aplican): lo que el usuario no puede ver vuelve como error, no en el PDF.
- Resuelve el PDF de cada operación capturando los errores **por operación**,
  sin abortar el lote.
- Une los PDF con `odoo.tools.pdf.merge_pdf` respetando el orden de
  selección.
- Marca como impresas, con `sudo()` y solo los campos de marcado, únicamente
  las operaciones que entraron en el PDF que devuelve.

La respuesta es el PDF del lote más la cabecera **`X-Remito-Result`** con el
detalle por operación en JSON **codificado en base64** (las cabeceras HTTP
son latin-1 y los mensajes van en español). La cabecera
`X-Remito-Result-Encoding` lo indica. Si el lote no produjo ningún PDF, la
respuesta es JSON con los errores.

### Campos agregados a `stock.picking`

| Campo | Para qué |
|---|---|
| `remito_printed` | Marca de impresa (indexado, con seguimiento en el chatter) |
| `remito_printed_date`, `remito_printed_by` | Cuándo y quién |
| `remito_print_count` | Cuántas veces se imprimió |
| `remito_last_source` | `ucfe` / `standard` / `standard_fallback` |
| `remito_warehouse_id` | Almacén (related almacenado a `picking_type_id.warehouse_id`; `stock.picking` no tiene almacén propio) |
| `remito_usa_cfe`, `remito_cfe_ref` | Espejos de la facturación electrónica, para que las vistas no referencien campos que pueden no existir |
| `ucfe_pdf_available` | Calculado con método de búsqueda: si hay un PDF de UCFE adjunto |

`remito_warehouse_id` es un related almacenado: al instalar el módulo se
completa de una pasada sobre todas las operaciones existentes.

---

## Tests

```sh
odoo-bin -c <config> -d <base> -u stock_remito_print \
    --test-enable --test-tags /stock_remito_print --stop-after-init
```

Cubren la selección del PDF en los cuatro casos (con UCFE simulada, en éxito
y en error, con el fallback encendido y apagado), el marcado manual, las
reglas de registro, el perfil de solo impresión y el controlador por lotes
con mezcla de casos ok y error.

Los tests que necesitan UCFE se saltean solos si
`l10n_uy_einvoice_uruware` no está instalado.

En modo test Odoo no llama a wkhtmltopdf: `_render_qweb_pdf` devuelve HTML.
Por eso los tests que necesitan un PDF real lo simulan.

## Renderizar el reporte estándar fuera de los tests

Dos requisitos, los dos verificados en macOS arm64:

1. **wkhtmltopdf tiene que poder ejecutarse.** El binario que trae el venv es
   x86_64 y no existe build nativo arm64 (el proyecto está archivado y Qt
   WebKit nunca tuvo soporte macOS ARM). En Apple Silicon hace falta Rosetta:

   ```sh
   softwareupdate --install-rosetta --agree-to-license
   ```

2. **Tiene que haber un servidor HTTP escuchando en el puerto de
   `web.base.url`.** El HTML del reporte referencia los assets por URL
   absoluta. Si no hay nadie escuchando, wkhtmltopdf **no falla: se cuelga
   indefinidamente** esperando el evento de fin de carga (el hilo principal
   queda en `Converter::convert()` → `select()`).

   Esto es lo que pasa al renderizar desde `odoo shell` con el servidor
   apagado. Si `web.base.url` apunta a `http://localhost:8069`, hay que
   levantar el servidor en ese puerto, no en otro:

   ```sh
   odoo-bin -c <config> -d <base> --http-port=8069
   ```

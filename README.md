# Módulo de Integración WIS para Odoo

Integración entre Odoo y el Sistema de Gestión de Almacenes **WIS** (Warehouse Integration System).

---

## Índice

- [Documentación Funcional](#documentación-funcional)
  - [1. Propósito del módulo](#1-propósito-del-módulo)
  - [2. Conceptos clave](#2-conceptos-clave)
  - [3. Configuración inicial](#3-configuración-inicial)
  - [4. Gestión de entidades maestras](#4-gestión-de-entidades-maestras)
  - [5. Operaciones de inventario](#5-operaciones-de-inventario)
  - [6. Procesos de conciliación](#6-procesos-de-conciliación)
  - [7. Webhooks — notificaciones de WIS a Odoo](#7-webhooks--notificaciones-de-wis-a-odoo)
  - [8. Logs y trazabilidad](#8-logs-y-trazabilidad)
  - [9. Guía operativa — Preguntas frecuentes](#9-guía-operativa--preguntas-frecuentes)
- [Documentación Técnica](#documentación-técnica)
  - [1. Estructura del módulo](#1-estructura-del-módulo)
  - [2. Clase IntegracionWIS](#2-clase-integracionwis)
  - [3. Modelos de Odoo extendidos](#3-modelos-de-odoo-extendidos)
  - [4. Controlador de Webhooks](#4-controlador-de-webhooks)
  - [5. Procesos de conciliación (técnico)](#5-procesos-de-conciliación-técnico)
  - [6. Procesador de eventos WMS](#6-procesador-de-eventos-wms)
  - [7. Patrones y convenciones de código](#7-patrones-y-convenciones-de-código)
  - [8. Funcionalidades pendientes y código comentado](#8-funcionalidades-pendientes-y-código-comentado)
  - [9. Guía de diagnóstico y troubleshooting](#9-guía-de-diagnóstico-y-troubleshooting)
  - [10. Seguridad y control de acceso](#10-seguridad-y-control-de-acceso)
  - [11. Próximos pasos sugeridos](#11-próximos-pasos-sugeridos)

---

## Documentación Funcional

### 1. Propósito del módulo

El módulo **`integracion_wis`** conecta Odoo con el sistema externo de gestión de almacenes WIS. Su objetivo es eliminar la operación manual doble: los datos maestros, los movimientos de stock y los pedidos fluyen automáticamente entre ambos sistemas, con trazabilidad completa en ambas puntas.

El módulo fue desarrollado específicamente para este proyecto siguiendo la definición de procesos acordada con el proveedor WIS y la documentación de su API (versión 10.2).

---

### 2. Conceptos clave

| Sistema | Rol |
|---------|-----|
| **Odoo** | ERP: registra productos, socios, órdenes de compra/venta y movimientos de inventario. Es la **fuente de verdad para datos maestros** (productos, socios, precios). |
| **WIS** | WMS: controla dónde está físicamente cada unidad, organiza el picking, el empaquetado y los despachos. Es la **fuente de verdad para el stock físico**. |

Cuando hay discrepancias de stock, el sistema reconcilia tomando el valor de WIS como referencia.

La sincronización es **bidireccional**: además del flujo principal Odoo → WIS, el módulo también actualiza datos en Odoo a partir de información recibida desde WIS. Ver sección 2.3.

#### 2.3 Sincronización bidireccional

El módulo implementa flujos de datos en ambas direcciones:

- **Odoo → WIS:** productos, socios, pedidos y recepciones se envían a WIS desde Odoo como fuente de datos maestros.
- **WIS → Odoo:** WIS puede actualizar datos en Odoo a través de dos mecanismos:
  - **Webhooks** (en tiempo real): confirmaciones de despacho, recepción, preparación y anulación de pedidos.
  - **Conciliación** (por cron): los procesos de conciliación pueden sincronizar en sentido inverso campos como nombre, teléfono y dirección de socios (`sincronizarClientesDesdeWIS()`), y descripción, peso, precio y estado activo de productos (`sincronizarProductosDesdeWIS()`). En ambos casos se usa el contexto `skip_wis_sync=True` / `_avoid_wms=True` para evitar que la escritura en Odoo vuelva a disparar un envío a WIS.

#### 2.1 Identificadores únicos

Cada entidad sincronizada tiene un par de identificadores:

- **Código Único WIS** (`codigo_unico`): identificador que WIS asigna (o el sistema genera) para cada producto, socio o pedido. Es la "llave" que permite a ambos sistemas referirse al mismo objeto.
- **Número de Interfaz WMS** (`codigo_interfaz_wms` / `idPedidoWMS`): número de seguimiento de operación que devuelve WIS al confirmar una inserción.

#### 2.2 Switch de comunicación

Existe un campo global **"Comunicación con WIS habilitada"** en la configuración del módulo. Cuando está desactivado, todo el módulo entra en **modo silencioso**: ningún cron, ningún trigger automático y ningún botón manual envía datos a WIS. Permite hacer pruebas o mantenimiento sin afectar la operación del almacén.

---

### 3. Configuración inicial

La configuración se realiza desde **WIS > Configuración**. Existe exactamente un registro de configuración por compañía de Odoo.

#### 3.1 Configuración de Webhooks

Además de la configuración principal, existe un registro separado de Webhook que guarda la clave secreta usada para verificar la autenticidad de las llamadas entrantes de WIS.

- **Menú:** WIS > Configuración > Webhooks
- **Campo:** Clave Secreta — proporcionada por WIS
- El parámetro de sistema `wis.webhook_secret` debe coincidir con esta clave

---

### 4. Gestión de entidades maestras

#### 4.1 Productos

Para que un producto sea gestionado por el módulo, se debe activar el flag **"Integración con WMS"** en su ficha. Esto aplica tanto al template (producto genérico) como a cada variante.

Cuando se marca la integración en un template, el módulo automáticamente activa todas sus variantes y las envía a WIS. Si se agregan nuevas variantes a un template ya integrado, esas variantes también se envían automáticamente.

**Campos sincronizados Odoo → WIS:** nombre, categoría, precio, peso, estado activo, código de barras.

> **Importante:** el campo `manejoIdentificador` solo se puede establecer cuando el producto se crea por primera vez en WIS. Si el producto ya tiene movimientos en el almacén, WIS rechaza cualquier cambio en este campo. El módulo detecta este error y lo trata como una advertencia, no como un fallo.

**Actualización automática:** cada vez que se modifica alguno de los campos relevantes en Odoo, el módulo detecta el cambio y reenvía el producto a WIS de forma automática, sin necesidad de acción manual.

**Consulta de stock manual:** desde la ficha de una variante existe el botón **"Consultar Stock en WMS"**. Al pulsarlo, el módulo consulta la API de WIS y muestra en un popup la cantidad disponible según WIS.

#### 4.2 Socios (Clientes y Proveedores)

Los socios se sincronizan con WIS como "agentes". Un mismo socio puede tener dos códigos en WIS: uno como cliente (`CLI`) y otro como proveedor (`PRO`), dependiendo de sus roles en Odoo.

Para activar la sincronización, se activa el flag **"Integración con WIS"** en la ficha del socio. El módulo enviará el socio a WIS automáticamente al guardar.

> **Restricción importante:** un socio que ya fue sincronizado con WIS (tiene código WMS y código único) **no puede eliminarse** de Odoo.

Identificadores del socio:

- `codigo_unico_cliente` — se usa en operaciones de venta y despacho
- `codigo_unico_proveedor` — se usa en recepciones y compras

> **Nota:** existe también un campo `codigo_unico` genérico en el modelo de socio, pero está en desuso. Los campos operativos son exclusivamente `codigo_unico_cliente` y `codigo_unico_proveedor`.

---

### 5. Operaciones de inventario

#### 5.1 Tipos de operación configurables

Cada tipo de operación de Odoo (Recepciones, Entregas, Traslados internos, etc.) se puede configurar de forma independiente para la integración con WIS.

#### 5.2 Flujo de una entrega (venta)

1. Se confirma una orden de venta. El sistema crea automáticamente un picking de tipo Entrega.
2. Cuando el picking alcanza el **Estado para Disparo WIS** configurado, el módulo lo envía a WIS como un Pedido (`/Pedido/Create`).
3. WIS procesa el pedido: organiza el picking físico y puede enviarlo a distintas zonas del almacén.
4. WIS notifica a Odoo vía webhook que la mercadería está preparada (el picking pasa a estado "Preparado por WMS").
5. Cuando WIS confirma el despacho vía webhook (`confirmacionPedido`), Odoo valida automáticamente el picking, registra la fecha, transportadora, remito, número de seguimiento y crea los paquetes (bultos) correspondientes.

#### 5.3 Flujo de una recepción (compra)

1. Se confirma una orden de compra. Odoo crea el picking de recepción.
2. El módulo envía el picking a WIS como una Referencia de Recepción (`/ReferenciaRecepcion/Create`), indicando los productos esperados y sus fechas de vencimiento si aplica.
3. El operario en el almacén físico recibe la mercadería y WIS registra lo recibido.
4. WIS notifica a Odoo vía webhook (`confirmacionRecepcion`) con las cantidades realmente recibidas.
5. El módulo actualiza las cantidades del picking en Odoo y lo valida. Si la recepción fue parcial, crea automáticamente un **backorder** con el remanente.

#### 5.4 Devoluciones

Las devoluciones de clientes se envían a WIS como Referencias de Recepción con tipo `OD` (*Order Delivery Return*), usando el endpoint `/ReferenciaRecepcion/Create`. El flujo es similar al de recepciones pero desde el lado del cliente que devuelve mercadería.

#### 5.5 Estados WMS de un picking

El módulo agrega a cada picking un campo **"Estado WMS"** que refleja en qué punto del proceso de WIS se encuentra:

| Estado | Descripción |
|--------|-------------|
| `Sin enviar a WMS` | El picking aún no fue enviado a WIS |
| `Enviado a WMS` | El picking fue enviado exitosamente, WIS lo tiene registrado |
| `Preparado por WMS` | WIS confirmó que la mercadería fue preparada físicamente |
| `Despachado por WMS` | WIS confirmó que la mercadería fue despachada (picking validado) |
| `Anulado por WMS` | WIS informó que el pedido fue cancelado |

---

### 6. Procesos de conciliación

El módulo incluye procesos automáticos (y manuales) para asegurar que los datos de Odoo y WIS permanezcan sincronizados.

#### 6.1 Conciliación de Maestros

Verifica y sincroniza los datos maestros: productos, clientes y códigos de barras. Acceso desde **WIS > Conciliación > Maestros**.

- **Productos:** envía a WIS todos los productos con integración activa modificados desde la última sincronización exitosa (sincronización incremental). Los productos se envían en lotes de 200.
- **Clientes:** reenvía a WIS todos los clientes con integración activa, actualizando sus datos si hay diferencias.
- **Códigos de Barras:** verifica qué códigos de barras existen en WIS y agrega los que faltan.

#### 6.2 Conciliación de Stock

Compara el stock físico en WIS con el stock registrado en Odoo para cada producto con integración activa. Acceso desde **WIS > Conciliación > Stock**.

El proceso:

1. Consulta el stock de todas las variantes con código WIS en un solo bloque.
2. Compara la cantidad en WIS con la suma de los quants en las ubicaciones configuradas.
3. Si la diferencia supera la **Diferencia Mínima** configurada, actualiza el stock en la ubicación de reposición para igualar WIS.
4. Registra el resultado (éxito, advertencia o error) en el log de conciliación.

#### 6.3 Conciliaciones automáticas (Crons)

El módulo incluye crons que ejecutan las conciliaciones en forma programada. Todos los crons verifican primero si la comunicación está habilitada; si el switch global está apagado, se omiten sin error.

#### 6.4 Estados de conciliación

| Estado | Descripción |
|--------|-------------|
| Borrador | Recién creado, no procesado |
| En Proceso | Ejecutándose |
| Completado | Terminó sin errores |
| Completado con Errores | Terminó pero hubo errores parciales (los exitosos se guardaron) |
| Error Fatal | Error que impidió completar el proceso |

---

### 7. Webhooks — notificaciones de WIS a Odoo

WIS puede notificar a Odoo de forma asíncrona sobre eventos que ocurren en el almacén. Las notificaciones llegan al endpoint:

```
POST /webhook/wis/callback
```

> **Estado actual:** la verificación de firma HMAC está **temporalmente desactivada** en el código. El endpoint acepta cualquier llamada entrante sin validar su autenticidad. Reactivarla es parte de los próximos pasos pendientes.

Cuando esté activa, cada llamada entrante será verificada mediante una firma HMAC usando la clave secreta configurada. Si la firma no coincide, la llamada se rechazará con código `401`.

#### 7.1 Log de webhooks

Cada llamada entrante queda registrada en **WIS > Logs > Webhooks**. El log guarda:

- Fecha y hora
- Tipo de evento
- Número de interfaz de ejecución (asignado por WIS)
- Cuerpo completo de la solicitud recibida
- Respuesta enviada a WIS
- Estado: Éxito o Error

Este log es fundamental para diagnosticar problemas cuando WIS dice que envió una notificación pero Odoo no reaccionó como se esperaba.

---

### 8. Logs y trazabilidad

El módulo genera varios tipos de log para auditar y diagnosticar cualquier situación. Adicionalmente, escribe en el log del servidor de Odoo (`odoo.log`) con el prefijo `[WIS]` para facilitar el seguimiento en consola.

---

### 9. Guía operativa — Preguntas frecuentes

**¿Por qué un picking no se envió a WIS?**
- Verificar que el tipo de operación tenga "Integración con WMS" activada
- Verificar que el estado del picking coincida con el "Estado para Disparo WIS" configurado
- Verificar que el picking tenga partner asignado con el código WIS correspondiente (CLI o PRO)
- Verificar que "Comunicación con WIS habilitada" esté activa
- Revisar los logs del picking para ver si hubo un intento fallido con mensaje de error

**¿Por qué un producto no se envió a WIS?**
- Verificar que el producto (variante) tenga "Integración con WMS" activada
- Verificar que el tipo de producto sea "Almacenable" (no servicio ni consumible)
- Revisar los logs WMS en la ficha del producto

**¿Cómo forzar el reenvío de un producto ya sincronizado?**  
Modificar cualquier campo relevante (nombre, categoría, precio, peso) y guardar. El módulo detectará el cambio y reenviará automáticamente.

**¿Cómo sé si WIS recibió bien un pedido?**  
El picking tendrá el campo "ID Pedido WMS" y "Código WMS identificatorio" completos, y el estado WMS será "Enviado". También se verá en el log del picking.

**¿Qué pasa si la conciliación de stock encuentra diferencias?**  
El módulo ajusta el stock en la "Ubicación para Reponer Stock" configurada, escribiendo la cantidad según WIS. Esto se registra en el log de conciliación.

**¿Se puede desactivar la integración temporalmente?**  
Sí. Ir a **WIS > Configuración**, desmarcar "Comunicación con WIS habilitada" y guardar. El módulo entra en modo silencioso total.

---

## Documentación Técnica

### 1. Estructura del módulo

El módulo se llama **`integracion_wis`** y se instala como un addon de Odoo estándar.

**Dependencias:** `base`, `contacts`, `stock`, `account`, `sale_management`, `purchase`, `product`, `stock_barcode`

---

### 2. Clase IntegracionWIS

Definida en `models/models.py`. Es el núcleo del módulo — todas las comunicaciones con la API WIS pasan por esta clase. Existe exactamente un registro por compañía (constraint SQL `unique` en `company_id`).

#### 2.1 Autenticación OAuth 2.0

WIS usa OAuth 2.0 con grant type `client_credentials`. El método `renovarToken()` obtiene un `access_token` almacenado en el campo `token` junto con la fecha de expiración (`expiracionToken`).

El método `consultarAPI()` verifica antes de cada llamada si el token está vigente. Si expiró o no existe, llama a `renovarToken()` automáticamente.

#### 2.2 Método `consultarAPI()`

Método HTTP centralizado. Acepta:

| Parámetro | Descripción |
|-----------|-------------|
| `link` | Path del endpoint (ej.: `/Producto/CreateOrUpdate`) |
| `params` | Query parameters (GET) |
| `body` | Cuerpo JSON (POST) |
| `method` | `GET` o `POST` (defecto `GET`) |

Siempre usa `timeout=30` segundos. En caso de `status != 200` lanza `ValidationError` con el texto de la respuesta.

#### 2.3 Switch de comunicación

```python
if not self.env['integracion_wis.integracion_wis']._comunicacion_habilitada():
    return  # o raise ValidationError
```

El método de clase `_comunicacion_habilitada()` es el **punto de entrada único** para verificar si el módulo debe actuar. Siempre debe consultarse antes de cualquier operación que llame a la API.

---

### 3. Modelos de Odoo extendidos

#### 3.1 `product.product` y `product.template` (`Product.py`)

Lógica de `create`/`write` en `product.product`:

- **`create()`:** si `integracion_wms=True` y tipo `product` y el template padre no tiene integración WMS, llama a `enviarWS()`
- **`write()`:** si algún campo de `CAMPOS_WIS` cambió y comunicación está activa, llama a `enviarWS()` para cada variante afectada
- **`_avoid_wms` (context):** si está en `True`, omite el envío a WIS (evita recursión infinita en los `write` internos del módulo)

```python
CAMPOS_WIS = {
    'name', 'default_code', 'description', 'active', 'integracion_wms',
    'barcode', 'list_price', 'standard_price', 'taxes_id', 'uom_id', 'uom_po_id'
}
```

Lógica en `product.template`:

- Si se activa `integracion_wms` en un template, se propaga a todas sus variantes y se envían a WIS
- `_create_variant_ids()` se sobrescribe para detectar nuevas variantes de templates ya integrados y enviarlas a WIS

#### 3.2 `res.partner` (`res_partner.py`)

Lógica `create`/`write`: si `integracion_wms=True` y la comunicación está activa, llama a `enviarWS()`. El flag `skip_wis_sync` en contexto evita recursión.

El método `enviarWS()` decide si enviar como `CLI`, `PRO` o ambos según `customer_rank` y `supplier_rank` del socio.

**Restricción de borrado:** si el socio tiene `codigo_wms` y `codigo_unico`, no puede eliminarse (`ValidationError`).

**Campo `codigo_unico` (deprecado):** el modelo tiene un campo `codigo_unico` genérico que ya no se usa. Los campos operativos son `codigo_unico_cliente` y `codigo_unico_proveedor`. El campo deprecado se mantiene en el modelo por compatibilidad pero no debe referenciarse en código nuevo.

#### 3.3 `stock.picking` (`stock_picking.py`)

Hooks de disparo (cuándo se envía el picking a WIS):

| Hook | Condición |
|------|-----------|
| `create()` | Si el picking ya tiene el estado de disparo y todos los requisitos |
| `action_confirm()` | Hook de confirmación |
| `action_assign()` | Hook de asignación de stock |
| `_action_done()` | Cuando se valida y el estado de disparo es `done` |
| `write()` | Si cambia el `state` |

El método `_enviar_wis_si_corresponde()` centraliza la lógica de verificación:

- Comunicación activa
- Tipo de operación con integración WMS
- Tiene `move_ids` y `partner`
- `wms_estado == 'sin_enviar'` (no se reenvía si ya fue enviado)
- El `state` actual coincide con `estado_disparo_wis` del tipo de operación

> **Nota sobre `waiting`:** si `estado_disparo_wis = 'waiting'`, el módulo también acepta el estado `confirmed` porque en Odoo ambos estados son variantes de "en espera".

**Actualización automática al modificar un picking ya enviado:**

- Si cambia `scheduled_date` → llama a `actualizarReferenciaRecepcion()`
- Si cambia `product_uom_qty` en un `stock.move` → llama a `actualizarReferenciaRecepcion()`

#### 3.4 `stock.picking.type` (`stock_picking_type.py`)

Campos de configuración que el administrador puede definir por cada tipo de operación.

Entre ellos, el campo **`integra_parciales`** (Boolean, defecto `False`) controla el comportamiento de los backorders generados cuando una recepción es parcial:

- `integra_parciales = False`: el backorder **hereda el `codigo_unico` del picking original**. WIS ya conoce ese código, por lo que el backorder no se envía como una operación nueva sino que se asocia a la referencia existente.
- `integra_parciales = True`: el backorder recibe un código propio y se trata como una operación independiente en WIS.

---

### 4. Controlador de Webhooks

**Archivo:** `controllers/controllers.py`

#### 4.1 Endpoint

```
POST /webhook/wis/callback
```

- Auth: `public` (no requiere sesión Odoo)
- CSRF: deshabilitado
- Tipo: JSON

#### 4.2 Verificación de firma

> **Temporalmente desactivada.** La llamada a `_verify_signature()` está comentada en el código. El endpoint acepta cualquier payload sin verificar su origen.

El método `_verify_signature()` compara la cabecera `X-Hub-Signature` con la clave almacenada en el parámetro de sistema `wis.webhook_secret` usando `hmac.compare_digest()`. Para reactivarla basta con descomentar la guarda al inicio del handler principal.

#### 4.3 Normalización de claves

WIS puede enviar claves PascalCase (`ConfirmacionPedido`) o camelCase (`confirmacionPedido`). El método `_normalize_keys()` convierte recursivamente todo a camelCase antes de procesar.

#### 4.4 Handlers de eventos

| Handler | Descripción |
|---------|-------------|
| `_handle_confirmacion_recepcion()` | Procesa recepciones confirmadas por WIS, actualiza cantidades y valida el picking |
| `_handle_confirmacion_pedido()` | Procesa despachos confirmados por WIS, crea paquetes y valida el picking |
| `_handle_pedidos_anulados()` | Cancela el picking en Odoo cuando WIS anula el pedido |

**`_handle_confirmacion_recepcion()` — lógica detallada:**

1. Itera sobre el array `referencias` del payload
2. Para cada referencia, busca el picking por `codigo_unico`
3. Construye mapas de cantidades por `move.id` y por `codigo_unico` del producto
4. Asigna `qty_done` en cada `move_line` según lo recibido por WIS
5. Si la cantidad es menor a lo pedido, marca `es_parcial=True`
6. Llama a `button_validate()` con `skip_wms_integration=True`
7. Si es parcial, confirma el backorder wizard; si es completo, lo cancela

**`_handle_confirmacion_pedido()` — lógica detallada:**

1. Itera sobre el array `pedidos` del payload
2. Busca el picking por `name`, luego por `codigo_unico`, luego por `idPedidoWMS`
3. Verifica que el picking no esté ya `done`, `cancelled`, `despachado` o `anulado`
4. Actualiza campos de despacho: fecha, transportadora, remito, bultos, peso
5. Crea paquetes (`stock.quant.package`) para cada contenedor del payload
6. Llama a `button_validate()` y cancela el backorder wizard (sin backorder en despachos)

---

### 5. Procesos de conciliación (técnico)

#### 5.1 `ConciliacionMaestros` (`conciliacionMaestros.py`)

- **`conciliarProductos()`:** usa sincronización incremental. Lee `ultima_sync_productos` del registro de configuración. Solo procesa variantes con `write_date > ultima_sync`. Si termina sin errores, actualiza el campo. Delega a `insertarProductosMasivo()` para envíos en lote.
- **`insertarProductosMasivo()`:** agrupa variantes en chunks de 200, llama a `/Producto/CreateOrUpdate` por lote. Si un lote falla, reintenta uno por uno. Detecta y omite el error `'No se permite modificar ManejoIdentificador'` tratándolo como advertencia.
- **`conciliarClientes()`:** itera clientes con integración activa y `codigo_unico`, llama a `enviarWS()` por cada uno.
- **`conciliarCodigosBarra()`:** itera variantes con `barcode`, verifica existencia en WIS y agrega si falta.

#### 5.2 `ConciliacionStock` (`ConciliacionStock.py`)

- Recolecta todas las variantes con `integracion_wms=True` y `codigo_unico`
- Llama a `consultaStockBulk()` para obtener el stock de todas en una pasada (renueva token una sola vez)
- Para cada variante calcula `cantidad_odoo` sumando `stock.quant` en las ubicaciones configuradas
- Si `abs(cantidad_odoo - cantidad_wis) >= diferenciaMinima`, busca o crea un quant en `ubicacionReponerStock` y setea `inventory_quantity = cantidad_wis`

---

### 6. Procesador de eventos WMS

**Archivo:** `wms_event_processor.py`

`WMSEventProcessor` es un `TransientModel` que procesa los payloads de los webhooks que requieren lógica compleja. Los handlers del controller lo instancian con:

```python
self.env['wms.event.processor'].create({})
```

Funcionalidades implementadas:

- `confirmacionMercaderiaPreparada()`: marca el picking como `preparado_wms`
- `confirmacionPedido()`: procesa empaquetado si hay contenedores, valida el picking
- `pedidosAnulados()`: cancela el picking en Odoo
- `confirmacionRecepcion()`: actualiza cantidades y valida la recepción

**Empaquetado** (`_procesar_empaquetado_completo`): crea paquetes `stock.quant.package` por contenedor, asigna líneas de movimiento a cada paquete.

**Desempaquetado** (`_procesar_desempaquetado_completo`): disociación o eliminación de paquetes existentes. El comportamiento se controla por el parámetro de sistema `wms.metodo_apertura_paquete` (`'disociar'` por defecto).

---

### 7. Patrones y convenciones de código

#### 7.1 Prevención de recursión (`_avoid_wms` / `skip_wis_sync`)

Cuando el módulo hace un `write()` interno para actualizar campos como `codigo_unico`, `wms_last_sync`, etc., usa el contexto `_avoid_wms=True` (productos) o `skip_wis_sync=True` (socios) para evitar que ese `write` vuelva a disparar la sincronización con WIS.

```python
record.with_context(_avoid_wms=True).write({'codigo_unico': valor})
```

#### 7.2 Generación de código único para nuevos objetos

Cuando se crea un producto o socio sin `codigo_unico`, el módulo genera uno temporal aleatorio:

| Entidad | Formato |
|---------|---------|
| Productos | `PRD-XXXXXX` (6 dígitos aleatorios) |
| Clientes | `CLI-XXXXXX` |
| Proveedores | `PRO-XXXXXX` |

#### 7.3 Manejo del error `ManejoIdentificador`

WIS no permite modificar `manejoIdentificador` en productos que ya tienen movimientos. El módulo detecta la cadena `'ManejoIdentificador'` + `'No se permite modificar'` en el mensaje de error y lo trata como éxito (el producto ya existe en WIS con el valor correcto).

#### 7.4 Detección de tipo de operación en `enviarWS()`

| Tipo | Acción |
|------|--------|
| `ODM`, `ODT`, `ODW`, `ODFT` | `insertarDevolucion()` |
| `OCI` o picking con `sale_id` + `incoming` | `insertarReferenciaRecepcion()` |
| `EC` | Crea/confirma factura y luego inserta pedido |
| Cualquier otro | `insertarPedidos()` |

#### 7.5 Timezone Uruguay

El modelo `ConciliacionMaestros` usa `get_uruguay_datetime()` que convierte UTC a `America/Montevideo` para los campos de fecha de los logs, porque el proyecto opera en Uruguay.

#### 7.6 Truncado de nombres de productos

WIS tiene un límite de **65 caracteres** para el campo `descripcion` de un producto. El módulo trunca automáticamente antes de enviar.

#### 7.7 Código de pedido (hash MD5)

Cuando se crea un pedido sin `codigo_unico`, el módulo genera un código de 8 caracteres a partir del MD5 del nombre del picking: `P` + primeros 8 chars del hex en mayúsculas. Ejemplo: `PABCD1234`.

---

### 8. Funcionalidades pendientes y código comentado

#### 8.1 Devoluciones desde wizard (`StockReturnPicking.py`)

El método `_create_returns()` en `ReturnPicking` está completamente comentado. Estaba destinado a enviar automáticamente la devolución a WIS al crearla desde el wizard de Odoo. Actualmente las devoluciones se envían por el flujo normal del picking.

#### 8.2 Creación de picking desde orden de compra (`purchase_order.py`)

El método `_create_picking()` en `PurchaseOrder` también está comentado. La lógica de integración para recepciones por OC quedó en `sale_order.py` y en el hook directo del `stock.picking`.

#### 8.3 Webhook handlers no implementados

Los siguientes handlers están definidos pero contienen solo `pass`:

- `_handle_ajustes()`: ajustes de stock enviados por WIS
- `_handle_consulta_stock()`: respuesta a consultas de stock
- `_handle_almacenamiento()`: eventos de almacenamiento
- `_handle_confirmacion_produccion()`: confirmaciones de producción
- `_handle_test()`: handler de prueba

#### 8.4 Bug en `transferirStock()`

El método `transferirStock()` en `IntegracionWIS` llama al endpoint `/Stock/Transferir` pero tiene un bug: usa `response.status` en lugar de `req.status_code` (la respuesta de `consultarAPI()` ya es el JSON, no el objeto `requests.Response`). No está siendo llamado desde ningún lugar del código actualmente.

#### 8.5 Campos `res_config_settings` no conectados

El modelo `ResConfigSettings` tiene campos (`wis_auto_sync_products`, `wis_auto_sync_partners`, etc.) que no están conectados a la lógica real del módulo. El módulo usa directamente el modelo `integracion_wis.integracion_wis` para su configuración operativa.

---

### 9. Guía de diagnóstico y troubleshooting

#### Un picking no se envía a WIS

Secuencia de verificación:

1. `picking_type_id.integracion_wms == True`
2. `picking.wms_estado == 'sin_enviar'`
3. `picking.partner_id` tiene el código WIS correcto (`CLI` o `PRO` según tipo)
4. `picking.state` está en el `estado_disparo_wis` del tipo
5. Todos los `move_ids` tienen productos con `codigo_unico`
6. La comunicación está activa
7. Revisar `log_ids` del picking para ver el error exacto

#### WIS recibe el pedido pero Odoo no se actualiza cuando WIS responde

- Verificar que el webhook llegue: revisar `wis.webhook.log`
- Verificar que el `idPedidoWMS` o `codigo_unico` del picking coincida con el valor enviado en el webhook
- Revisar el log del webhook para ver el error en la respuesta
- Nota: la verificación de firma HMAC está temporalmente desactivada, por lo que no puede ser la causa del rechazo en el estado actual del código

#### Error "El producto no tiene código WMS" al enviar picking

El producto no tiene el campo `codigo_unico`. Solución: ir a la ficha del producto, activar "Integración con WMS" y guardar.

#### Error de token expirado o inválido

El módulo debería renovarlo automáticamente. Si el error persiste, verificar `client_id`, `client_secret` y `url_access_token` en la configuración. Para testear manualmente desde la consola Python de Odoo:

```python
config = env['integracion_wis.integracion_wis'].search([], limit=1)
config.renovarToken()
```

#### Conciliación de productos omite productos recién modificados

Revisar el campo `ultima_sync_productos` en el registro de configuración. Si tiene una fecha incorrectamente en el futuro, resetearla a `False` para que la próxima conciliación procese todos los productos.




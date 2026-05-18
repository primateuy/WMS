# Postman / curl — webhook WIS (ajustes y almacenamiento)

## Colección unificada para apuntar al servidor

`WIS_Server.postman_collection.json` — **importable directo a Postman**. 20 requests organizados en 5 folders:

| Folder | Requests | Qué hacen |
|---|---|---|
| 00 Sanity | 1 | Ping al handler `test`, 200 sin tocar nada |
| 01 Casos reales | 2 | Ajuste +5 PRD-428421 y Almacenamiento REC-240715 (12 productos). Replicados en local 18-may-26, crearon movimientos de stock reales |
| 02 Seguros | 2 | Producto/picking inexistente → 500 controlado, sin tocar stock |
| 03 Destructivos | 1 | Almacenamiento REC-910005 — mueve stock si el IMP del server destino sigue pendiente |
| 04 Históricos | 14 | Los 2 ajustes + 12 almacenamientos originales que recibió `o17_forum_support` |

### Variables (editar a nivel colección al importar)

| Variable | Default | Para qué |
|---|---|---|
| `base_url` | (vacío) | Dominio del Odoo destino, sin barra final. Ej: `https://primate.uy` |
| `use_hmac` | `false` | Poner `true` si en el server destino `_verify_signature` está activo |
| `wis_secret` | (vacío) | Solo si `use_hmac=true`. El valor está en `ir.config_parameter.wis.webhook_secret` |

El pre-request script de la colección agrega `X-Hub-Signature` (HMAC-SHA512 + Base64) cuando `use_hmac=true`. Si está en `false`, el header no se manda.

### Pre-requisitos del server destino

- Módulo `integracion_wis` instalado y schema actualizado (con columnas `picking_type_ajuste_wis_id` y `tipo_ajuste_recuento`).
- Para que los ajustes con movimiento creen picking: `integracion_wis.integracion_wis.picking_type_ajuste_wis_id` configurado en la BD.
- Para que el almacenamiento real funcione sin backfill manual: aplicar el fix de `controllers.py:1622-1670` (search por `group_id`, no `purchase_id`).

---

## Archivos individuales (legacy)

Payloads reales tomados de `wis.webhook.log` en `o17_forum_support`
(usando `psql -d o17_forum_support -c "SELECT request FROM wis_webhook_log WHERE tipo IN ('ajustes','almacenamiento') ..."`).

Endpoint: `POST {{base_url}}/webhook/wis/callback`
- `type='json'`, `auth='public'`, `csrf=False` (`controllers.py:14`).
- Hoy la verificación HMAC `X-Hub-Signature` está **deshabilitada** (`controllers.py:40-41` comentado). Se ignora el header si lo mandás.
- El handler hace `request.update_env(user=SUPERUSER_ID)` así que NO necesita autenticación de usuario.

## Archivos

| Archivo | Tipo | Idempotencia | Resultado esperado |
|---|---|---|---|
| `00_test_ping.json` | `test` | seguro | `200 {"status":200}` |
| `01_ajustes_movimiento_PRD-550854.json` | `ajustes` | seguro (producto no existe) | `500 {"status":500,"detail":"..."}` |
| `02_ajustes_movimiento_PRD-490654_DESTRUCTIVO.json` | `ajustes` | **CREA picking real de -50** | `200 {"status":200}` y un picking nuevo |
| `03_almacenamiento_basico_REC-932515.json` | `almacenamiento` | seguro (picking no existe) | `500 {"status":500,"detail":"..."}` |
| `04_almacenamiento_multiple_REC-910005.json` | `almacenamiento` | **DESTRUCTIVO** si `REC-910005` sigue pendiente (lo está: `state=assigned`) | si todavía no se ejecutó, valida el picking interno |

## curl rápido

```sh
curl -X POST http://localhost:8088/webhook/wis/callback \
  -H 'Content-Type: application/json' \
  --data @00_test_ping.json
```

Para los destructivos, primero confirmar con el equipo qué se quiere reproducir.
La respuesta viene wrappeada en JSON-RPC: `{"jsonrpc":"2.0","id":null,"result":{"status":200}}`.

## Postman

Importar `WIS_Webhooks.postman_collection.json`. Las variables `base_url` y
`wis_secret` se editan a nivel colección.

## HMAC (si en el futuro re-habilitan `_verify_signature`)

WIS firma con HMAC-SHA512 + Base64 (`controllers.py:116-117`):

```sh
SECRET=$(psql -At -d o17_forum_support -c "SELECT value FROM ir_config_parameter WHERE key='wis.webhook_secret'")
BODY=$(cat 00_test_ping.json)
SIG=$(printf '%s' "$BODY" | openssl dgst -sha512 -hmac "$SECRET" -binary | base64)
curl -X POST http://localhost:8088/webhook/wis/callback \
  -H 'Content-Type: application/json' \
  -H "X-Hub-Signature: $SIG" \
  --data "$BODY"
```

En Postman: pre-request script que compute `pm.variables.get('wis_secret')` →
`CryptoJS.HmacSHA512(body, secret).toString(CryptoJS.enc.Base64)` y setee el
header `X-Hub-Signature`.

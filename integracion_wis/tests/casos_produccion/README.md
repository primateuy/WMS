# Casos de producción de WIS replicados

Son los 12 casos de la auditoría del 08 al 22-10-2026, armados con el flujo real de Odoo sobre una copia de la base. A cada caso se le mandan notificaciones con la misma forma que las reales de WIS. Cada caso verifica el resultado **correcto**:

- con integracion_wis 0.9.7 da en rojo, igual que en producción (30 de 42 controles en rojo);
- con 0.10.0 da 42 de 42.

| Caso | Qué reproduce |
|---|---|
| C1 | Reenvío de la misma preparación (e-Remito duplicado) |
| C1T | Tiempo de respuesta con 15 pedidos (corte por tiempo) |
| C2 | Entrega POLO/OUT que junta varios pedidos de una tienda |
| C3 | Caja de WIS compartida entre pedidos de crossdock |
| C4 | Despacho sin preparación previa |
| C5 | Mayorista: el asistente de SMS frena la validación |
| C6 | Pedido que sólo figura en los contenedores |
| C7 | Sucursal sin «Es receptor» |
| C7B | Línea suelta en 0 (stock repartido en dos paquetes) |
| C8 | Notificaciones simultáneas |
| C9 | Recepción cancelada desde la compra |
| C10 | Fecha de entrega vencida |
| C11 | Ajuste fuera de escala |
| C12 | Dos preparaciones despachadas en dos camiones |

## Variantes (`sh/c20.py`, `sh/c21.py`)

Son las situaciones que pueden romper la integración de punta a cabo. Además de su control puntual, cada una pasa por `integridad()` (`sh/integridad.py`), que revisa:

- stock sin negativos nuevos;
- cajas en una sola ubicación;
- e-Remitos, uno por operación y sin líneas en 0;
- lo despachado entregado entero y lo no despachado sin entregar;
- la recepción validada en la tienda.

| Variante | Qué prueba |
|---|---|
| V01 | Reenvío con otro número de interfaz y las mismas cajas |
| V02 | Dos copias de la misma notificación al mismo tiempo |
| V03 | El despacho llega antes que la preparación |
| V04 | El mismo despacho dos veces |
| V05 | Tres tandas de preparación y un solo camión |
| V06 | WIS prepara más de lo pedido |
| V07 | Producto que el pedido no tiene |
| V08 | Anulación parcial y preparación del resto |
| V09 | Pedido que Odoo no conoce |
| V10 | Un pedido falla (receptor), se corrige y se reprocesa |
| V11 | Entrega unificada despachada en desorden |
| V12 | Caja compartida, misma entrega |
| V13 | Caja compartida y la lista nombra un solo pedido |
| V14 | Crossdock con caja compartida y parcial |
| V15 | Mayoristas con y sin teléfono |
| V16 | WIS rechaza la anulación de la recepción |
| V17 | Ajuste grande retenido y aprobado; ajuste chico aplicado |
| V18 | Notificación de 30 pedidos que necesita varias corridas del cron |
| V19 | El servidor muere a mitad y retoma |
| V20 | Uruware caído y reproceso |
| V21 | Despacho de un pedido cancelado en Odoo |
| V22 | La entrega se validó a mano antes del despacho |
| V23 | WIS prepara lo que Odoo no tiene en stock |

V18 y V19 se corren con una conf de límites bajos (`limit_time_real = limit_time_real_cron = 90`) y con las variables `CASOS_CONF`, `CASOS_PYTHON` y `CASOS_ODOO`, porque V19 mata y vuelve a levantar el servidor.

**Supuesto de V19:** si el proceso muere mientras Uruware firma, el reproceso reenvía el e-Remito con el mismo Uuid. El simulador devuelve el mismo CFE, que es para lo que UCFE usa el Uuid. Hay que confirmarlo con Uruware.

`grabar_video.py <base> <salida.mp4> log/video_*.json` graba en la interfaz de Odoo cómo quedó cada caso (Playwright + ffmpeg).

## Cómo se arma

1. **Copia de la base.** Hacerla con `createdb -O odoo -T <base> <copia>`. Si la base no es del usuario de Odoo, Odoo no la lista: `/web/login` manda al selector de bases y el webhook da 404.
2. **Neutralizar la copia:** `odoo-bin neutralize -d <copia>`. Apagar los crons y dejar activo sólo el de «WIS: Notificaciones recibidas».
3. **Simuladores**, que no salen a la red:
   - `python3 stub_wis.py 8765`: registra en `log/wis_requests.jsonl` (sin credenciales). Devuelve 400 si la fecha de entrega está vencida, como WIS.
   - `python3 stub_ucfe.py 8766`: aprueba y numera. Sin bloque `<Receptor>` responde 31. La latencia por emisión se fija en `log/ucfe_demora` (segundos).
4. **Apuntar la copia a los simuladores:**
   - `integracion_wis_integracion_wis`: `apiLink` = `http://127.0.0.1:8765/api/`, `url_access_token` = `http://127.0.0.1:8765/connect/token` y `comunicacion_activa` = true.
   - `res_company`: `url_testing` y `url_produccion` = `http://127.0.0.1:8766`, con `einvoice_mode` = `testing`.
5. **Conf propia:** `workers = 2`, `max_cron_threads = 1` y `dbfilter = ^<copia>$`.
   - En macOS el servidor arranca con `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES no_proxy="*"`. Si no, el worker que llama a `requests` se muere sin dejar traza.
6. **Correr:** `CONF=<conf> ./correr.sh c1 c2 c3 ...`. El resultado queda en `log/resultado_*.json`.

`reset.sh` es el script con el que se armó en la Mac de desarrollo: crea una copia nueva desde una foto y nunca borra bases. Hay que adaptarle las rutas.

## Contra support

`CASOS_WIS_URL=https://<support>/webhook/wis/callback`. Los casos crean operaciones reales en la base: usar sólo una base de pruebas con WIS y UCFE apuntando a simuladores o a entornos de prueba.

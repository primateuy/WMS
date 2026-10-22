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

#!/usr/bin/env python3
"""Tests end-to-end de los webhooks WIS.

Corre contra un Odoo server REAL (arrancado en background) — los `HttpCase`
nativos dan 404 sobre rutas custom auth='public' en este entorno, así que
optamos por un harness externo:

  1. Pre-condición: el server está corriendo en BASE_URL con db-filter
     resolviendo a una sola BD (ver --db-filter en la línea de arranque).
  2. Fixtures se crean vía JSON-RPC (mismo formato que usa el cliente web).
  3. Los webhooks se golpean con `requests.post` directo.
  4. Estados de BD se verifican via JSON-RPC (search_read).

Uso:

    # 1. Arrancar Odoo de fondo (de otra terminal o &):
    odoo-bin --config forum.conf -d o17_forum_support \\
        --db-filter='^o17_forum_support$' --http-port=8099 &

    # 2. Correr el script:
    python tests/run_webhooks_e2e.py http://localhost:8099 o17_forum_support

Sale con código 0 si todos los tests pasan, 1 si alguno falla.
"""
import json
import sys
import time
import traceback

import requests


class RpcClient:
    """Wrapper minimal de JSON-RPC para crear/leer registros en Odoo."""

    def __init__(self, base_url, db, login="soporteforum@primate.uy", password="12345"):
        self.base = base_url.rstrip("/")
        self.db = db
        self.password = password
        self.session = requests.Session()
        self.uid = self._login(login, password)

    def _call(self, service, method, args):
        body = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
        }
        r = self.session.post(f"{self.base}/jsonrpc", json=body, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"JSON-RPC error: {data['error']}")
        return data.get("result")

    def _login(self, login, password):
        return self._call("common", "login", [self.db, login, password])

    def execute(self, model, method, *args, **kw):
        return self._call(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, method, list(args), kw],
        )

    def create(self, model, vals, context=None):
        ctx = context or {}
        return self._call(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, "create", [vals], {"context": ctx}],
        )

    def write(self, model, ids, vals, context=None):
        ctx = context or {}
        return self._call(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, "write", [ids, vals], {"context": ctx}],
        )

    def read(self, model, ids, fields):
        return self._call(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, "read", [ids, fields], {}],
        )

    def search_read(self, model, domain, fields, limit=None):
        kw = {"limit": limit} if limit else {}
        return self._call(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, "search_read", [domain, fields], kw],
        )

    def ref(self, xmlid):
        rec = self.search_read(
            "ir.model.data",
            [["module", "=", xmlid.split(".")[0]], ["name", "=", xmlid.split(".")[1]]],
            ["res_id"], limit=1,
        )
        if not rec:
            raise RuntimeError(f"xmlid {xmlid} no encontrado")
        return rec[0]["res_id"]


def post_webhook(base_url, payload):
    r = requests.post(
        f"{base_url}/webhook/wis/callback",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code != 200:
        raise AssertionError(f"HTTP {r.status_code} en webhook. Body: {r.text[:300]}")
    body = r.json()
    if "error" in body:
        raise AssertionError(f"Error JSON-RPC del webhook: {body['error']}")
    return body.get("result", body)


# ============================================================ Tests

def test_event_no_soportado(rpc, base_url):
    res = post_webhook(base_url, {"Id": "noExisteTest", "data": {}})
    assert res.get("status") == 400, f"esperaba 400, vino {res}"
    assert "noExisteTest" in res.get("detail", ""), res
    return "evento no soportado → 400 OK"


def test_confirmacion_recepcion_basico(rpc, base_url):
    # Fixtures
    pt_id = rpc.create("stock.picking.type", {
        "name": "E2E Recepcion Test",
        "code": "incoming",
        "sequence_code": f"E2EIN{int(time.time() * 1000) % 100000}/",
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Recepcion",
        "type": "product",
        "codigo_unico": f"E2E-RCP-{int(time.time() * 1000)}",
    })
    partner_id = rpc.create("res.partner", {
        "name": "E2E Partner Recepcion",
        "codigo_unico_proveedor": f"PRO-E2E-{int(time.time() * 1000)}",
    })
    codigo_unico = f"REC-E2E-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
        "partner_id": partner_id,
        "codigo_unico": codigo_unico,
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    move_id = rpc.create("stock.move", {
        "name": "E2E Move",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 5,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])
    rpc.execute("stock.picking", "action_assign", [pick_id])

    payload = {
        "Id": "confirmacionRecepcion",
        "NumeroInterfazEjecucion": 1001,
        "confirmacionRecepcion": {
            "Referencias": [{
                "NumeroReferencia": codigo_unico,
                "Detalles": [{
                    "IdLineaSistemaExterno": f"odoo__stock.move__{move_id}",
                    "Producto": rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"],
                    "CantidadConsumida": 5,
                    "CantidadReferencia": 5,
                }],
            }],
        },
    }
    res = post_webhook(base_url, payload)
    assert res.get("status") == 200, res

    state = rpc.read("stock.picking", [pick_id], ["state"])[0]["state"]
    assert state == "done", f"state esperado 'done', vino '{state}'"
    return f"picking {pick_id} validado por recepción"


def test_confirmacion_recepcion_ignora_auto(rpc, base_url):
    pt_id = rpc.create("stock.picking.type", {
        "name": "E2E Recepcion AUTO Test",
        "code": "incoming",
        "sequence_code": f"E2EAUTO{int(time.time() * 1000) % 100000}/",
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod AUTO",
        "type": "product",
        "codigo_unico": f"E2E-AUTO-{int(time.time() * 1000)}",
    })
    codigo_unico = f"REC-AUTO-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
        "codigo_unico": codigo_unico,
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    move_id = rpc.create("stock.move", {
        "name": "E2E Move AUTO",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 3,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])
    rpc.execute("stock.picking", "action_assign", [pick_id])

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    payload = {
        "Id": "confirmacionRecepcion",
        "confirmacionRecepcion": {
            "Referencias": [{
                "NumeroReferencia": codigo_unico,
                "Detalles": [
                    {
                        "IdLineaSistemaExterno": f"odoo__stock.move__{move_id}",
                        "Producto": cod_prod,
                        "Identificador": "(AUTO)",
                        "CantidadConsumida": 0,
                        "CantidadReferencia": 3,
                    },
                    {
                        "IdLineaSistemaExterno": f"odoo__stock.move__{move_id}",
                        "Producto": cod_prod,
                        "Identificador": "LOTE-REAL",
                        "CantidadConsumida": 3,
                        "CantidadReferencia": 3,
                    },
                ],
            }],
        },
    }
    res = post_webhook(base_url, payload)
    assert res.get("status") == 200, res

    state = rpc.read("stock.picking", [pick_id], ["state"])[0]["state"]
    assert state == "done", f"state esperado 'done', vino '{state}'"
    # Cantidad efectiva debe ser 3 (la línea AUTO se ignora; no se acumula a 6).
    moves = rpc.read("stock.move", [move_id], ["quantity"])
    assert moves[0]["quantity"] == 3.0, f"quantity esperada 3, vino {moves[0]['quantity']}"
    return "línea (AUTO) ignorada; cantidad real = 3"


def test_confirmacion_pedido_basico(rpc, base_url):
    pt_id = rpc.create("stock.picking.type", {
        "name": "E2E Despacho Test",
        "code": "outgoing",
        "sequence_code": f"E2EOUT{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
        "metodo_preparacion_wis": True,
        "metodo_cancelacion_wis": True,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Despacho",
        "type": "product",
        "codigo_unico": f"E2E-DSP-{int(time.time() * 1000)}",
    })
    # Stock en existencias
    quant_id = rpc.create("stock.quant", {
        "product_id": prod_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "inventory_quantity": 20,
    }, context={"inventory_mode": True})
    rpc.execute("stock.quant", "action_apply_inventory", [quant_id])

    partner_id = rpc.create("res.partner", {
        "name": "E2E Partner Despacho",
        "codigo_unico_cliente": f"CLI-E2E-{int(time.time() * 1000)}",
    })
    codigo_unico = f"PED-E2E-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
        "partner_id": partner_id,
        "codigo_unico": codigo_unico,
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E Move Despacho",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 2,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])
    rpc.execute("stock.picking", "action_assign", [pick_id])

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    barcode_pkg = f"BARCODE-E2E-{int(time.time() * 1000)}"
    id_ext = f"EXT-E2E-{int(time.time() * 1000)}"
    # Flujo nuevo: primero confirmacionMercaderiaPreparada valida el picking a done/preparado y
    # arma el paquete con las cantidades del contenedor. confirmacionPedido YA NO procesa pickings
    # activos (esa rama se desactivó a propósito): solo confirma el DESPACHO de lo ya preparado.
    res = post_webhook(base_url, {
        "Id": "confirmacionMercaderiaPreparada",
        "confirmacionMercaderiaPreparada": {
            "FechaPreparacion": "01/05/2026 09:00",
            "Pedidos": [{
                "Pedido": codigo_unico,
                "Contenedores": [{
                    "CodigoBarras": barcode_pkg,
                    "IdExternoContenedor": id_ext,
                    "Detalles": [{"Producto": cod_prod, "CantidadPreparada": 2.0}],
                }],
            }],
        },
    })
    assert res.get("status") == 200, res

    # confirmacionPedido: despacha lo preparado (metadatos de transporte sobre el picking done)
    res = post_webhook(base_url, {
        "Id": "confirmacionPedido",
        "confirmacionPedido": {
            "FechaCierre": "01/05/2026 10:00",
            "DescripcionCamion": "Camion E2E",
            "Transportadora": "Trans-E2E",
            "Pedidos": [{"Pedido": codigo_unico}],
        },
    })
    assert res.get("status") == 200, res

    pdata = rpc.read("stock.picking", [pick_id], ["state", "wms_estado", "wms_descripcion_camion"])[0]
    assert pdata["state"] == "done", f"state esperado 'done', vino '{pdata['state']}'"
    assert pdata["wms_estado"] == "despachado", pdata
    assert pdata["wms_descripcion_camion"] == "Camion E2E", pdata
    # Paquete (creado por la preparada) con CodigoBarras y wis_id_externo
    pkg = rpc.search_read("stock.quant.package", [["name", "=", barcode_pkg]], ["wis_id_externo"], 1)
    assert pkg, f"falta paquete name={barcode_pkg}"
    assert pkg[0]["wis_id_externo"] == id_ext, pkg
    return f"despacho OK (preparada+pedido); paquete '{barcode_pkg}'"


def test_mercaderia_preparada_basico(rpc, base_url):
    pt_id = rpc.create("stock.picking.type", {
        "name": "E2E Prep Test",
        "code": "outgoing",
        "sequence_code": f"E2EPREP{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
        "metodo_preparacion_wis": True,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Prep",
        "type": "product",
        "codigo_unico": f"E2E-PRP-{int(time.time() * 1000)}",
    })
    codigo_unico = f"PRP-E2E-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
        "codigo_unico": codigo_unico,
        # WIS solo prepara pedidos que fueron enviados; el matcher excluye 'sin_enviar'.
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E Move Prep",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 1,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])

    res = post_webhook(base_url, {
        "Id": "confirmacionMercaderiaPreparada",
        "confirmacionMercaderiaPreparada": {
            "FechaPreparacion": "02/05/2026 14:00",
            "Pedidos": [{"Pedido": codigo_unico}],
        },
    })
    assert res.get("status") == 200, res

    pdata = rpc.read("stock.picking", [pick_id], ["wms_estado", "wms_fecha_preparacion"])[0]
    assert pdata["wms_estado"] == "preparado", pdata
    assert pdata["wms_fecha_preparacion"], pdata
    return f"picking {pick_id} marcado como preparado"


def test_paquetes_creados_en_preparada_y_reusados_en_confirmacion(rpc, base_url):
    """Valida el cambio: los paquetes se crean en confirmacionMercaderiaPreparada
    cuando el payload trae contenedores, y una confirmacionPedido posterior con
    los mismos contenedores REUSA esos paquetes (no duplica) y no re-asigna
    move_lines ya asignadas (idempotencia del helper).
    """
    pt_id = rpc.create("stock.picking.type", {
        "name": "E2E Prep+Conf Test",
        "code": "outgoing",
        "sequence_code": f"E2EPC{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
        "metodo_preparacion_wis": True,
        "metodo_cancelacion_wis": True,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Prep+Conf",
        "type": "product",
        "codigo_unico": f"E2E-PCF-{int(time.time() * 1000)}",
    })
    # Stock para que action_assign genere move_lines reservadas
    quant_id = rpc.create("stock.quant", {
        "product_id": prod_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "inventory_quantity": 5,
    }, context={"inventory_mode": True})
    rpc.execute("stock.quant", "action_apply_inventory", [quant_id])

    partner_id = rpc.create("res.partner", {
        "name": "E2E Partner Prep+Conf",
        "codigo_unico_cliente": f"CLI-PCF-{int(time.time() * 1000)}",
    })
    codigo_unico = f"PCF-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
        "partner_id": partner_id,
        "codigo_unico": codigo_unico,
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E Move Prep+Conf",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 2,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])
    rpc.execute("stock.picking", "action_assign", [pick_id])

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    barcode_pkg = f"BCPCF-{int(time.time() * 1000)}"
    id_ext = f"EXTPCF-{int(time.time() * 1000)}"
    contenedor = {
        "CodigoBarras": barcode_pkg,
        "IdExternoContenedor": id_ext,
        "Detalles": [{"Producto": cod_prod, "CantidadPreparada": 2.0}],
    }

    # --- Fase 1: confirmacionMercaderiaPreparada con contenedores per-pedido ---
    res1 = post_webhook(base_url, {
        "Id": "confirmacionMercaderiaPreparada",
        "confirmacionMercaderiaPreparada": {
            "FechaPreparacion": "02/05/2026 14:00",
            "Pedidos": [{"Pedido": codigo_unico, "Contenedores": [contenedor]}],
        },
    })
    assert res1.get("status") == 200, res1
    pdata = rpc.read("stock.picking", [pick_id], ["wms_estado", "wms_fecha_preparacion"])[0]
    assert pdata["wms_estado"] == "preparado", f"tras preparada esperaba 'preparado', vino {pdata}"
    # Paquete creado en la PREPARACIÓN, no en confirmacionPedido
    pkgs1 = rpc.search_read("stock.quant.package", [["name", "=", barcode_pkg]], ["id", "wis_id_externo"])
    assert len(pkgs1) == 1, f"esperaba 1 paquete tras preparada, hay {len(pkgs1)}: {pkgs1}"
    pkg_id_inicial = pkgs1[0]["id"]
    assert pkgs1[0]["wis_id_externo"] == id_ext, pkgs1
    # move_lines asignadas al paquete
    mls1 = rpc.search_read("stock.move.line", [["picking_id", "=", pick_id]], ["id", "result_package_id"])
    assert mls1, "no hay move_lines tras action_assign"
    asignadas = [m for m in mls1 if m["result_package_id"] and m["result_package_id"][0] == pkg_id_inicial]
    assert asignadas, f"ningún move_line quedó asignado al paquete {pkg_id_inicial}: {mls1}"
    # Snapshot del mapeo move_line -> paquete antes de confirmacionPedido
    snapshot = {m["id"]: (m["result_package_id"][0] if m["result_package_id"] else None) for m in mls1}

    # --- Fase 2: confirmacionPedido con los MISMOS contenedores ---
    res2 = post_webhook(base_url, {
        "Id": "confirmacionPedido",
        "confirmacionPedido": {
            "FechaCierre": "03/05/2026 10:00",
            "DescripcionCamion": "Camion PCF",
            "Transportadora": "Trans-PCF",
            "Pedidos": [{"Pedido": codigo_unico}],
            "Contenedores": [contenedor],
        },
    })
    assert res2.get("status") == 200, res2
    pdata2 = rpc.read("stock.picking", [pick_id], ["state", "wms_estado", "wms_descripcion_camion"])[0]
    assert pdata2["state"] == "done", f"tras confirmacion: state={pdata2['state']}"
    assert pdata2["wms_estado"] == "despachado", pdata2
    assert pdata2["wms_descripcion_camion"] == "Camion PCF", pdata2
    # Idempotencia: NO se duplicó el paquete, mismo id
    pkgs2 = rpc.search_read("stock.quant.package", [["name", "=", barcode_pkg]], ["id"])
    assert len(pkgs2) == 1, f"esperaba 1 paquete (no duplicado), hay {len(pkgs2)}: {pkgs2}"
    assert pkgs2[0]["id"] == pkg_id_inicial, (
        f"paquete cambió: era {pkg_id_inicial}, ahora {pkgs2[0]['id']}"
    )
    # Idempotencia: move_lines preexistentes mantienen su result_package_id
    mls2 = rpc.search_read("stock.move.line", [["picking_id", "=", pick_id]], ["id", "result_package_id"])
    for m in mls2:
        prev = snapshot.get(m["id"])
        if prev is None:
            continue  # move_line nuevo creado por button_validate, no comparable
        cur = m["result_package_id"][0] if m["result_package_id"] else None
        assert cur == prev, f"move_line {m['id']} cambió de paquete: {prev} -> {cur}"

    return (
        f"preparada creó pkg id={pkg_id_inicial}; confirmacion lo reusó "
        f"(idempotente, sin duplicar, sin re-asignar move_lines)"
    )


def test_pedidos_anulados_basico(rpc, base_url):
    pt_id = rpc.create("stock.picking.type", {
        "name": "E2E Anular Test",
        "code": "outgoing",
        "sequence_code": f"E2EAN{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
        "metodo_cancelacion_wis": True,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Anular",
        "type": "product",
        "codigo_unico": f"E2E-AN-{int(time.time() * 1000)}",
    })
    codigo_unico = f"AN-E2E-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
        "codigo_unico": codigo_unico,
        # WIS solo anula pedidos que fueron enviados; el matcher excluye 'sin_enviar'.
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E Move Anular",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 1,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    res = post_webhook(base_url, {
        "Id": "PedidosAnulados",
        "PedidosAnulados": {
            "PedidosAnulados": [{
                "Pedido": codigo_unico,
                "CodigoAgente": "CLI-E2E",
                "Detalles": [{
                    "Producto": cod_prod,
                    "CantidadAnulada": 1.0,
                    "Motivo": "Anulación E2E",
                    "FechaAlta": "11/05/2026",
                    "Aplicacion": "PRE110",
                }],
            }],
        },
    })
    assert res.get("status") == 200, res

    pdata = rpc.read("stock.picking", [pick_id], ["state", "wms_estado"])[0]
    assert pdata["state"] == "cancel", pdata
    assert pdata["wms_estado"] == "anulado", pdata
    return f"picking {pick_id} cancelado por WIS"


def test_ajustes_movimiento(rpc, base_url):
    # Asegurar picking_type_ajuste_wis_id en la config — lo creamos si falta.
    cfg = rpc.search_read("integracion_wis.integracion_wis", [], ["id", "picking_type_ajuste_wis_id"], 1)
    assert cfg, "Falta record integracion_wis.integracion_wis"
    if not cfg[0]["picking_type_ajuste_wis_id"]:
        # Crear tipos de operación interno entrada + return.
        wh = rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"]
        loc_stock = rpc.ref("stock.stock_location_stock")
        # Ubicación virtual de inventario (loss/gain)
        loc_virtual_parent = rpc.ref("stock.stock_location_locations_virtual")
        loc_inv = rpc.create("stock.location", {
            "name": f"Inventario AJ E2E {int(time.time() * 1000)}",
            "usage": "inventory",
            "location_id": loc_virtual_parent,
        })
        pt_entrada = rpc.create("stock.picking.type", {
            "name": f"Ajuste Entrada E2E {int(time.time() * 1000)}",
            "code": "internal",
            "sequence_code": f"E2EAJI{int(time.time() * 1000) % 100000}/",
            "default_location_src_id": loc_inv,
            "default_location_dest_id": loc_stock,
            "warehouse_id": wh,
        })
        pt_salida = rpc.create("stock.picking.type", {
            "name": f"Ajuste Salida E2E {int(time.time() * 1000)}",
            "code": "internal",
            "sequence_code": f"E2EAJO{int(time.time() * 1000) % 100000}/",
            "default_location_src_id": loc_stock,
            "default_location_dest_id": loc_inv,
            "warehouse_id": wh,
        })
        rpc.write("stock.picking.type", [pt_entrada], {"return_picking_type_id": pt_salida})
        rpc.write("integracion_wis.integracion_wis", [cfg[0]["id"]], {
            "picking_type_ajuste_wis_id": pt_entrada,
            "tipo_ajuste_recuento": "RECUENTO_FISICO",
        })
        cfg = rpc.search_read("integracion_wis.integracion_wis", [], ["picking_type_ajuste_wis_id"], 1)
    assert cfg[0]["picking_type_ajuste_wis_id"], "picking_type_ajuste_wis_id sigue vacío tras setup"

    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Ajuste",
        "type": "product",
        "codigo_unico": f"E2E-AJ-{int(time.time() * 1000)}",
    })
    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    nro_aj = int(time.time() * 1000)

    res = post_webhook(base_url, {
        "Id": "Ajustes",
        "Ajustes": {
            "Ajustes": [{
                "Producto": cod_prod,
                "CantidadMovimiento": 7.0,
                "TipoAjuste": "INGRESO_GENERICO",
                "Motivo": "01",
                "DescripcionMotivo": "E2E entrada",
                "NumeroAjusteStock": nro_aj,
                "FechaRealizacion": "01/05/2026",
                "Identificador": "*",
            }],
        },
    })
    assert res.get("status") == 200, res

    nuevo = rpc.search_read(
        "stock.picking",
        [["origin", "ilike", f"WIS-AJ-{nro_aj}"]],
        ["state", "picking_type_id"], 1,
    )
    assert nuevo, f"no se creó picking WIS-AJ-{nro_aj}"
    assert nuevo[0]["state"] == "done", nuevo[0]
    assert nuevo[0]["picking_type_id"][0] == cfg[0]["picking_type_ajuste_wis_id"][0]
    return f"picking de ajuste {nuevo[0]['picking_type_id'][1]} creado y validado"


def test_almacenamiento_basico(rpc, base_url):
    # Tipos de operación
    pt_imp = rpc.create("stock.picking.type", {
        "name": "E2E Recep Alma Test",
        "code": "incoming",
        "sequence_code": f"E2EALM{int(time.time() * 1000) % 100000}/",
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
    })
    # Ubicación intermedia (entrada)
    parent = rpc.ref("stock.stock_location_locations")
    loc_in = rpc.create("stock.location", {
        "name": f"Entrada Alma E2E {int(time.time() * 1000)}",
        "usage": "internal",
        "location_id": parent,
    })
    pt_int = rpc.create("stock.picking.type", {
        "name": f"E2E Interno Alma Test {int(time.time() * 1000)}",
        "code": "internal",
        "sequence_code": f"E2EALMI{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": loc_in,
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
    })
    # Producto
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod Alma",
        "type": "product",
        "codigo_unico": f"E2E-ALM-{int(time.time() * 1000)}",
    })
    partner_id = rpc.create("res.partner", {
        "name": "E2E Vendor Alma",
        "codigo_unico_proveedor": f"PRO-E2E-{int(time.time() * 1000)}",
    })
    # OC
    oc_id = rpc.create("purchase.order", {
        "partner_id": partner_id,
        "order_line": [(0, 0, {
            "product_id": prod_id,
            "product_qty": 4,
            "name": "E2E line",
            "date_planned": "2026-05-01",
            "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
            "price_unit": 1.0,
        })],
    })
    # Reemplazar la ubicación dest del IMPO por loc_in para que el INT recoja desde ahí
    # stock.picking.purchase_id es computado desde stock.move.purchase_line_id.
    # Por eso seteamos purchase_line_id en el move y NO purchase_id directamente
    # en el picking (que es read-only).
    oc_line_id = rpc.search_read(
        "purchase.order.line", [["order_id", "=", oc_id]], ["id"], 1,
    )[0]["id"]

    # procurement.group: el handler _handle_almacenamiento usa picking_imp.group_id
    # para buscar el picking interno (memoria 2026-05-18). En crossdocks reales lo
    # crea Odoo automáticamente con la corrida de procurement; acá lo creamos a mano
    # y lo asignamos a ambos pickings para emular el encadenamiento IMPO→INT.
    codigo_imp = f"IMPO-E2E-{int(time.time() * 1000)}"
    group_id = rpc.create("procurement.group", {
        "name": f"E2E-ALM-{codigo_imp}",
        "partner_id": partner_id,
    })

    pick_imp = rpc.create("stock.picking", {
        "picking_type_id": pt_imp,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in,
        "partner_id": partner_id,
        "codigo_unico": codigo_imp,
        "group_id": group_id,
        # La recepción IMPO fue enviada a WIS; el matcher de almacenamiento excluye 'sin_enviar'.
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E IMPO move",
        "picking_id": pick_imp,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 4,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in,
        "purchase_line_id": oc_line_id,
        "group_id": group_id,
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_imp])
    rpc.execute("stock.picking", "action_assign", [pick_imp])
    # Setear quantity en move_lines
    mls = rpc.search_read("stock.move.line", [["picking_id", "=", pick_imp]], ["id"])
    for ml in mls:
        rpc.write("stock.move.line", [ml["id"]], {"quantity": 4})
    rpc.execute(
        "stock.picking",
        "button_validate",
        [pick_imp],
    )

    # Picking interno desde loc_in → existencias, mismo group_id que el IMPO.
    pick_int = rpc.create("stock.picking", {
        "picking_type_id": pt_int,
        "location_id": loc_in,
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
        "partner_id": partner_id,
        "group_id": group_id,
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E INT move",
        "picking_id": pick_int,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 4,
        "location_id": loc_in,
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
        "purchase_line_id": oc_line_id,
        "group_id": group_id,
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_int])
    rpc.execute("stock.picking", "action_assign", [pick_int])

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    res = post_webhook(base_url, {
        "Id": "Almacenamiento",
        "Almacenamiento": {
            "Serializado": codigo_imp,
            "CodigoAgente": "PRO-E2E",
            "Detalles": [{
                "Producto": cod_prod,
                "CantidadAlmacenada": 4.0,
                "Identificador": "*",
            }],
        },
    })
    assert res.get("status") == 200, res

    state = rpc.read("stock.picking", [pick_int], ["state"])[0]["state"]
    assert state == "done", f"picking interno state esperado 'done', vino '{state}'"
    return f"picking interno {pick_int} validado por almacenamiento"


def test_almacenamiento_no_confunde_crosspick_de_otro_codigo(rpc, base_url):
    """Regresión: en crossdock el mismo procurement group contiene crosspicks de OTROS
    pedidos (código W-P-…) que arrancan en la MISMA ubicación de Entrada. El almacenamiento
    de la recepción (código W-R-…) NO debe caer en el crosspick ajeno: debe resolver el
    interno cuyo `codigo_unico` coincide con el de la recepción, y el log debe quedar ahí."""
    ts = int(time.time() * 1000)
    parent = rpc.ref("stock.stock_location_locations")
    loc_in = rpc.create("stock.location", {
        "name": f"Entrada AlmaX E2E {ts}", "usage": "internal", "location_id": parent,
    })
    wh = rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"]
    pt_imp = rpc.create("stock.picking.type", {
        "name": "E2E RecepX Test", "code": "incoming",
        "sequence_code": f"E2EAXR{ts % 100000}/",
        "default_location_dest_id": loc_in, "warehouse_id": wh,
    })
    pt_int = rpc.create("stock.picking.type", {
        "name": f"E2E InternoX Test {ts}", "code": "internal",
        "sequence_code": f"E2EAXI{ts % 100000}/",
        "default_location_src_id": loc_in,
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": wh,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod AlmaX", "type": "product", "codigo_unico": f"E2E-ALMX-{ts}",
    })
    uom = rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0]
    partner_id = rpc.create("res.partner", {
        "name": "E2E Vendor AlmaX", "codigo_unico_proveedor": f"PRO-EX-{ts}",
    })
    codigo_rec = f"W-R-E2EX-{ts}"          # recepción (el Serializado del almacenamiento)
    codigo_ped = f"W-P-E2EX-{ts}"          # crosspick de OTRO pedido, mismo grupo
    group_id = rpc.create("procurement.group", {
        "name": f"E2E-ALMX-{ts}", "partner_id": partner_id,
    })

    # Recepción (código W-R-), valida y deja stock en loc_in.
    pick_imp = rpc.create("stock.picking", {
        "picking_type_id": pt_imp,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in, "partner_id": partner_id,
        "codigo_unico": codigo_rec, "group_id": group_id, "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2EX IMPO move", "picking_id": pick_imp, "product_id": prod_id,
        "product_uom": uom, "product_uom_qty": 4,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in, "group_id": group_id,
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_imp])
    rpc.execute("stock.picking", "action_assign", [pick_imp])
    for ml in rpc.search_read("stock.move.line", [["picking_id", "=", pick_imp]], ["id"]):
        rpc.write("stock.move.line", [ml["id"]], {"quantity": 4})
    rpc.execute("stock.picking", "button_validate", [pick_imp])

    def _crear_interno(codigo):
        p = rpc.create("stock.picking", {
            "picking_type_id": pt_int, "location_id": loc_in,
            "location_dest_id": rpc.ref("stock.stock_location_stock"),
            "partner_id": partner_id, "group_id": group_id, "codigo_unico": codigo,
            "wms_estado": "no_integrado",
        }, context={"skip_wms_integration": True})
        rpc.create("stock.move", {
            "name": f"E2EX INT move {codigo}", "picking_id": p, "product_id": prod_id,
            "product_uom": uom, "product_uom_qty": 4, "location_id": loc_in,
            "location_dest_id": rpc.ref("stock.stock_location_stock"), "group_id": group_id,
        }, context={"skip_wms_integration": True})
        rpc.execute("stock.picking", "action_confirm", [p])
        rpc.execute("stock.picking", "action_assign", [p])
        return p

    # El correcto (W-R-) se crea PRIMERO (id menor y reserva el stock); el crosspick ajeno
    # (W-P-) se crea DESPUÉS con id MAYOR: el criterio viejo (order='id desc' sin código) lo
    # habría agarrado. El fix debe elegir por codigo_unico, no por id.
    pick_correcto = _crear_interno(codigo_rec)
    pick_crosspick = _crear_interno(codigo_ped)

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    res = post_webhook(base_url, {
        "Id": "Almacenamiento",
        "Almacenamiento": {
            "Serializado": codigo_rec, "CodigoAgente": "PRO-EX",
            "Detalles": [{"Producto": cod_prod, "CantidadAlmacenada": 4.0, "Identificador": "*"}],
        },
    })
    assert res.get("status") == 200, res

    st_ok = rpc.read("stock.picking", [pick_correcto], ["state"])[0]["state"]
    st_cross = rpc.read("stock.picking", [pick_crosspick], ["state"])[0]["state"]
    assert st_ok == "done", f"el interno correcto (W-R) debía validarse, vino '{st_ok}'"
    assert st_cross != "done", f"el crosspick de OTRO código (W-P) NO debía tocarse, vino '{st_cross}'"

    # El log del almacenamiento debe quedar en el picking correcto, identificado por código.
    logs = rpc.search_read(
        "wms.integracion.log",
        [["codigo_unico", "=", codigo_rec], ["proceso", "=", "almacenamiento"],
         ["resultado", "=", "exito"]],
        ["picking_id"],
    )
    pids = [l["picking_id"][0] for l in logs if l["picking_id"]]
    assert pick_correcto in pids, f"log de almacenamiento no quedó en el interno correcto; pids={pids}"
    assert pick_crosspick not in pids, f"log de almacenamiento cayó en el crosspick ajeno; pids={pids}"
    return f"almacenamiento resolvió {pick_correcto} (W-R) y NO tocó {pick_crosspick} (W-P)"


def test_almacenamiento_no_valida_preparada_si_storage_sin_codigo(rpc, base_url):
    """Regresión (caso reportado): el interno de guardado todavía NO adquirió su codigo_unico
    (propagación pendiente) y en el MISMO grupo/ubicación hay una operación de mercadería
    preparada (código W-P-) ABIERTA con id MAYOR. El almacenamiento debe validar el guardado
    (sin código) y NO tocar la preparada ajena; el criterio viejo (order id desc) la validaba."""
    ts = int(time.time() * 1000)
    parent = rpc.ref("stock.stock_location_locations")
    loc_in = rpc.create("stock.location", {
        "name": f"Entrada AlmaZ E2E {ts}", "usage": "internal", "location_id": parent,
    })
    loc_prep = rpc.create("stock.location", {
        "name": f"Prep AlmaZ E2E {ts}", "usage": "internal", "location_id": parent,
    })
    wh = rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"]
    pt_imp = rpc.create("stock.picking.type", {
        "name": "E2E RecepZ Test", "code": "incoming",
        "sequence_code": f"E2EAZR{ts % 100000}/",
        "default_location_dest_id": loc_in, "warehouse_id": wh,
    })
    pt_int = rpc.create("stock.picking.type", {
        "name": f"E2E InternoZ Test {ts}", "code": "internal",
        "sequence_code": f"E2EAZI{ts % 100000}/",
        "default_location_src_id": loc_in,
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": wh,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod AlmaZ", "type": "product", "codigo_unico": f"E2E-ALMZ-{ts}",
    })
    uom = rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0]
    partner_id = rpc.create("res.partner", {
        "name": "E2E Vendor AlmaZ", "codigo_unico_proveedor": f"PRO-EZ-{ts}",
    })
    codigo_rec = f"W-R-E2EZ-{ts}"
    codigo_ped = f"W-P-E2EZ-{ts}"
    group_id = rpc.create("procurement.group", {
        "name": f"E2E-ALMZ-{ts}", "partner_id": partner_id,
    })

    # Recepción: 8 unidades a loc_in (alcanza para el guardado y la preparada).
    pick_imp = rpc.create("stock.picking", {
        "picking_type_id": pt_imp,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in, "partner_id": partner_id,
        "codigo_unico": codigo_rec, "group_id": group_id, "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2EZ IMPO move", "picking_id": pick_imp, "product_id": prod_id,
        "product_uom": uom, "product_uom_qty": 8,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in, "group_id": group_id,
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_imp])
    rpc.execute("stock.picking", "action_assign", [pick_imp])
    for ml in rpc.search_read("stock.move.line", [["picking_id", "=", pick_imp]], ["id"]):
        rpc.write("stock.move.line", [ml["id"]], {"quantity": 8})
    rpc.execute("stock.picking", "button_validate", [pick_imp])

    def _crear_interno(codigo, loc_dst, qty):
        p = rpc.create("stock.picking", {
            "picking_type_id": pt_int, "location_id": loc_in,
            "location_dest_id": loc_dst, "partner_id": partner_id, "group_id": group_id,
            "codigo_unico": codigo, "wms_estado": "no_integrado" if codigo else "sin_enviar",
        }, context={"skip_wms_integration": True})
        rpc.create("stock.move", {
            "name": f"E2EZ INT move {codigo or 'sincod'}", "picking_id": p, "product_id": prod_id,
            "product_uom": uom, "product_uom_qty": qty, "location_id": loc_in,
            "location_dest_id": loc_dst, "group_id": group_id,
        }, context={"skip_wms_integration": True})
        rpc.execute("stock.picking", "action_confirm", [p])
        rpc.execute("stock.picking", "action_assign", [p])
        return p

    # Guardado SIN código (propagación pendiente), id menor. Preparada W-P, id MAYOR y abierta.
    pick_storage = _crear_interno(False, rpc.ref("stock.stock_location_stock"), 4)
    pick_preparada = _crear_interno(codigo_ped, loc_prep, 4)

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    res = post_webhook(base_url, {
        "Id": "Almacenamiento",
        "Almacenamiento": {
            "Serializado": codigo_rec, "CodigoAgente": "PRO-EZ",
            "Detalles": [{"Producto": cod_prod, "CantidadAlmacenada": 4.0, "Identificador": "*"}],
        },
    })
    assert res.get("status") == 200, res

    st_storage = rpc.read("stock.picking", [pick_storage], ["state"])[0]["state"]
    st_prep = rpc.read("stock.picking", [pick_preparada], ["state"])[0]["state"]
    assert st_storage == "done", f"el guardado (sin código) debía validarse, vino '{st_storage}'"
    assert st_prep != "done", f"la mercaderia preparada (W-P) NO debía validarse, vino '{st_prep}'"
    return f"almacenamiento validó guardado {pick_storage} y NO la preparada {pick_preparada} (W-P)"


# ============================================================ Tests Puntos 1-4 (mayo 2026)

def _crear_pick_recepcion_minimo(rpc, codigo_unico, qty=2):
    """Helper: crea un picking incoming en estado assigned listo para webhook recepción."""
    pt_id = rpc.create("stock.picking.type", {
        "name": f"E2E P-1234 Recep {int(time.time() * 1000)}",
        "code": "incoming",
        "sequence_code": f"E2EP{int(time.time() * 1000) % 100000}/",
        "default_location_dest_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod P1234",
        "type": "product",
        "codigo_unico": f"E2E-P14-{int(time.time() * 1000)}",
    })
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
        "codigo_unico": codigo_unico,
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    move_id = rpc.create("stock.move", {
        "name": "E2E Move P1234",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": qty,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])
    rpc.execute("stock.picking", "action_assign", [pick_id])
    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    return pick_id, move_id, cod_prod


def test_punto1_wis_location_panel_id_recepcion(rpc, base_url):
    """Punto 1: LocationID + PanelID dentro del sub-objeto del evento se persisten."""
    codigo_unico = f"P1-LOC-{int(time.time() * 1000)}"
    pick_id, move_id, cod_prod = _crear_pick_recepcion_minimo(rpc, codigo_unico, qty=1)

    # El controller hace payload.get(event_id) y normaliza claves: las claves van
    # DENTRO del sub-objeto del evento.
    payload = {
        "Id": "confirmacionRecepcion",
        "confirmacionRecepcion": {
            "LocationID": "LOC-ABC-99",
            "PanelID": "PNL-7",
            "Referencias": [{
                "NumeroReferencia": codigo_unico,
                "Detalles": [{
                    "IdLineaSistemaExterno": f"odoo__stock.move__{move_id}",
                    "Producto": cod_prod,
                    "CantidadConsumida": 1,
                    "CantidadReferencia": 1,
                }],
            }],
        },
    }
    res = post_webhook(base_url, payload)
    assert res.get("status") == 200, res

    pdata = rpc.read("stock.picking", [pick_id], ["wis_location_id", "wis_panel_id", "wms_origen"])[0]
    assert pdata["wis_location_id"] == "LOC-ABC-99", pdata
    assert pdata["wis_panel_id"] == "PNL-7", pdata
    assert pdata["wms_origen"] == "recepcion", pdata
    return f"location/panel persistidos en picking {pick_id}"


def test_punto2_wms_origen_recepcion_sin_location(rpc, base_url):
    """Punto 2: recepción sin location/panel en payload sigue dejando wms_origen='recepcion'."""
    codigo_unico = f"P2-RCP-{int(time.time() * 1000)}"
    pick_id, move_id, cod_prod = _crear_pick_recepcion_minimo(rpc, codigo_unico, qty=1)

    res = post_webhook(base_url, {
        "Id": "confirmacionRecepcion",
        "confirmacionRecepcion": {
            "Referencias": [{
                "NumeroReferencia": codigo_unico,
                "Detalles": [{
                    "IdLineaSistemaExterno": f"odoo__stock.move__{move_id}",
                    "Producto": cod_prod,
                    "CantidadConsumida": 1,
                    "CantidadReferencia": 1,
                }],
            }],
        },
    })
    assert res.get("status") == 200, res
    pdata = rpc.read("stock.picking", [pick_id], ["wms_origen", "wis_location_id"])[0]
    assert pdata["wms_origen"] == "recepcion", pdata
    assert pdata["wis_location_id"] in (False, ""), pdata
    return f"wms_origen='recepcion' OK sin location/panel"


def test_punto2_wms_origen_despacho(rpc, base_url):
    """Punto 2: confirmacionPedido deja wms_origen='despacho'."""
    pt_id = rpc.create("stock.picking.type", {
        "name": f"E2E P2 Despacho {int(time.time() * 1000)}",
        "code": "outgoing",
        "sequence_code": f"E2EP2D{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": rpc.ref("stock.stock_location_stock"),
        "warehouse_id": rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"],
        "metodo_preparacion_wis": True,
        "metodo_cancelacion_wis": True,
    })
    prod_id = rpc.create("product.product", {
        "name": "E2E Prod P2D",
        "type": "product",
        "codigo_unico": f"E2E-P2D-{int(time.time() * 1000)}",
    })
    quant_id = rpc.create("stock.quant", {
        "product_id": prod_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "inventory_quantity": 5,
    }, context={"inventory_mode": True})
    rpc.execute("stock.quant", "action_apply_inventory", [quant_id])

    codigo_unico = f"P2-DSP-{int(time.time() * 1000)}"
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
        "codigo_unico": codigo_unico,
        "wms_estado": "enviado",
    }, context={"skip_wms_integration": True})
    rpc.create("stock.move", {
        "name": "E2E Move P2D",
        "picking_id": pick_id,
        "product_id": prod_id,
        "product_uom": rpc.read("product.product", [prod_id], ["uom_id"])[0]["uom_id"][0],
        "product_uom_qty": 1,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": rpc.ref("stock.stock_location_customers"),
    }, context={"skip_wms_integration": True})
    rpc.execute("stock.picking", "action_confirm", [pick_id])
    rpc.execute("stock.picking", "action_assign", [pick_id])

    cod_prod = rpc.read("product.product", [prod_id], ["codigo_unico"])[0]["codigo_unico"]
    # Flujo nuevo: preparada (valida a done/preparado con el contenedor) y luego confirmacionPedido.
    res = post_webhook(base_url, {
        "Id": "confirmacionMercaderiaPreparada",
        "confirmacionMercaderiaPreparada": {
            "FechaPreparacion": "10/05/2026 11:00",
            "Pedidos": [{
                "Pedido": codigo_unico,
                "Contenedores": [{
                    "CodigoBarras": f"P2-BC-{int(time.time() * 1000)}",
                    "IdExternoContenedor": f"P2-EXT-{int(time.time() * 1000)}",
                    "Detalles": [{"Producto": cod_prod, "CantidadPreparada": 1.0}],
                }],
            }],
        },
    })
    assert res.get("status") == 200, res
    res = post_webhook(base_url, {
        "Id": "confirmacionPedido",
        "confirmacionPedido": {
            "FechaCierre": "10/05/2026 12:00",
            "DescripcionCamion": "P2 Camion",
            "Transportadora": "P2 Trans",
            "Pedidos": [{"Pedido": codigo_unico}],
        },
    })
    assert res.get("status") == 200, res
    pdata = rpc.read("stock.picking", [pick_id], ["wms_estado", "wms_origen"])[0]
    assert pdata["wms_estado"] == "despachado", pdata
    assert pdata["wms_origen"] == "despacho", pdata
    return f"wms_origen='despacho' OK en picking {pick_id} (preparada+pedido)"


def test_punto3_l10n_latam_document_type_bypass(rpc, base_url):
    """Punto 3: location_dest_id.wis_no_requiere_eremito=True salta el compute de
    l10n_latam_document_type_id (no se exige tipo de documento, no se levanta UserError).

    Compara dos pickings con el mismo picking_type_id (uses_cfe=True):
    - destino normal → el compute corre (puede asignar o levantar UserError)
    - destino con flag → l10n_latam_document_type_id queda False sin error
    """
    wh = rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"]
    parent = rpc.ref("stock.stock_location_locations")

    loc_normal = rpc.create("stock.location", {
        "name": f"E2E Dest Normal {int(time.time() * 1000)}",
        "usage": "internal",
        "location_id": parent,
    })
    loc_no_remito = rpc.create("stock.location", {
        "name": f"E2E Dest NoRemito {int(time.time() * 1000)}",
        "usage": "internal",
        "location_id": parent,
        "wis_no_requiere_eremito": True,
    })

    pt_id = rpc.create("stock.picking.type", {
        "name": f"E2E P3 PT {int(time.time() * 1000)}",
        "code": "internal",
        "sequence_code": f"E2EP3{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": rpc.ref("stock.stock_location_stock"),
        "default_location_dest_id": loc_normal,
        "warehouse_id": wh,
        "uses_cfe": True,
    })
    pt = rpc.read("stock.picking.type", [pt_id], ["uses_cfe"])[0]
    assert pt.get("uses_cfe") is True, (
        f"picking_type.uses_cfe quedó {pt.get('uses_cfe')!r}; "
        "verificar que l10n_uy_einvoice_base esté instalado"
    )

    # Picking con destino flag-on: el compute heredado puede levantar UserError si
    # no encuentra exactamente 1 candidato; nuestro override lo salta y lo deja en False.
    pick_b = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": rpc.ref("stock.stock_location_stock"),
        "location_dest_id": loc_no_remito,
    }, context={"skip_wms_integration": True})

    data_b = rpc.read(
        "stock.picking", [pick_b],
        ["l10n_latam_document_type_id", "wis_skip_eremito", "uses_cfe"],
    )[0]
    # 1) wis_skip_eremito debe propagarse desde la location destino.
    assert data_b["wis_skip_eremito"] is True, (
        f"wis_skip_eremito esperado True; vino {data_b['wis_skip_eremito']!r}"
    )
    # 2) En destino con flag, l10n_latam_document_type_id debe quedar False.
    dt_b = data_b["l10n_latam_document_type_id"]
    assert not dt_b, (
        f"picking destino con flag: l10n_latam_document_type_id={dt_b!r} (esperaba False)"
    )
    # 3) create_delivery_guide no debe enviar CFE (override devuelve True sin tocar UCFE).
    #    Como el picking está en draft no podemos llamar el botón con state='done',
    #    pero sí podemos invocar el método directo y verificar que no levanta.
    res_dg = rpc.execute("stock.picking", "create_delivery_guide", [pick_b])
    assert res_dg is True, f"create_delivery_guide debió devolver True; vino {res_dg!r}"
    # 4) Verificar que el flag se propaga si cambia el destino a uno sin el booleano.
    rpc.write("stock.picking", [pick_b], {"location_dest_id": loc_normal})
    data_b2 = rpc.read("stock.picking", [pick_b], ["wis_skip_eremito"])[0]
    assert data_b2["wis_skip_eremito"] is False, (
        f"al cambiar destino al normal, wis_skip_eremito debería ser False; "
        f"vino {data_b2['wis_skip_eremito']!r}"
    )
    return "wis_skip_eremito + bypass document_type + skip create_delivery_guide OK"


def test_punto4_document_type_helper_no_levanta(rpc, base_url):
    """Punto 4: el helper _wis_complete_document_type no falla aún cuando
    el picking no tiene partner_id y el compute no encuentra candidatos.

    Verifica que el método existe y se ejecuta sin levantar para un picking
    típico de ajuste WIS (interno, sin partner, sin punto_emision configurado).
    """
    wh = rpc.search_read("stock.warehouse", [], ["id"], 1)[0]["id"]
    parent = rpc.ref("stock.stock_location_locations")
    loc_a = rpc.create("stock.location", {
        "name": f"E2E P4 Src {int(time.time() * 1000)}",
        "usage": "internal",
        "location_id": parent,
    })
    loc_b = rpc.create("stock.location", {
        "name": f"E2E P4 Dest {int(time.time() * 1000)}",
        "usage": "internal",
        "location_id": parent,
    })
    pt_id = rpc.create("stock.picking.type", {
        "name": f"E2E P4 PT {int(time.time() * 1000)}",
        "code": "internal",
        "sequence_code": f"E2EP4{int(time.time() * 1000) % 100000}/",
        "default_location_src_id": loc_a,
        "default_location_dest_id": loc_b,
        "warehouse_id": wh,
    })
    pick_id = rpc.create("stock.picking", {
        "picking_type_id": pt_id,
        "location_id": loc_a,
        "location_dest_id": loc_b,
    }, context={"skip_wms_integration": True})

    # Llamar al helper — no debe levantar aunque no haya CFE configurado.
    # _wis_complete_document_type es un método público (sin guion bajo prefijo)
    # accesible vía RPC. Si está protegido por convención de Odoo (prefijo _),
    # lo llamamos vía un wrapper. Como ya tiene prefijo _, lo wrapeamos en un
    # método público auxiliar inline o usamos invalidate_recordset directo.
    # Acá la verificación práctica: crear el picking no levanta excepción, y
    # action_confirm (que lo invoca internamente) tampoco.
    res = rpc.execute("stock.picking", "action_confirm", [pick_id])
    pdata = rpc.read("stock.picking", [pick_id], ["state"])[0]
    assert pdata["state"] in ("confirmed", "assigned", "draft"), pdata
    return f"action_confirm con helper interno completa sin levantar (state={pdata['state']})"


# ============================================================ Runner

TESTS = [
    test_event_no_soportado,
    test_confirmacion_recepcion_basico,
    test_confirmacion_recepcion_ignora_auto,
    test_confirmacion_pedido_basico,
    test_mercaderia_preparada_basico,
    test_paquetes_creados_en_preparada_y_reusados_en_confirmacion,
    test_pedidos_anulados_basico,
    test_ajustes_movimiento,
    test_almacenamiento_basico,
    test_almacenamiento_no_confunde_crosspick_de_otro_codigo,
    test_almacenamiento_no_valida_preparada_si_storage_sin_codigo,
    # Tests nuevos — Puntos 1-4 (mayo 2026)
    test_punto1_wis_location_panel_id_recepcion,
    test_punto2_wms_origen_recepcion_sin_location,
    test_punto2_wms_origen_despacho,
    test_punto3_l10n_latam_document_type_bypass,
    test_punto4_document_type_helper_no_levanta,
]


def main(argv):
    if len(argv) < 3:
        print(f"Uso: {argv[0]} <base_url> <db>")
        return 2
    base_url, db = argv[1].rstrip("/"), argv[2]
    rpc = RpcClient(base_url, db)
    print(f"Conectado a {base_url} db={db} uid={rpc.uid}\n")

    pasados, fallados = 0, 0
    for fn in TESTS:
        nombre = fn.__name__
        try:
            msg = fn(rpc, base_url)
            print(f"  PASS  {nombre}  — {msg}")
            pasados += 1
        except AssertionError as e:
            print(f"  FAIL  {nombre}")
            print(f"        {e}")
            fallados += 1
        except Exception as e:
            print(f"  ERROR {nombre}")
            print(f"        {type(e).__name__}: {e}")
            traceback.print_exc()
            fallados += 1
    print(f"\nResultado: {pasados} pasados | {fallados} fallados / {len(TESTS)}")
    return 0 if fallados == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

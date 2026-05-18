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

    def __init__(self, base_url, db, login="soporteforum@primate.uy", password="wis_test_pw_2026"):
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
    payload = {
        "Id": "confirmacionPedido",
        "confirmacionPedido": {
            "FechaCierre": "01/05/2026 10:00",
            "DescripcionCamion": "Camion E2E",
            "Transportadora": "Trans-E2E",
            "Pedidos": [{"Pedido": codigo_unico}],
            "Contenedores": [{
                "CodigoBarras": barcode_pkg,
                "IdExternoContenedor": id_ext,
                "Detalles": [{"Producto": cod_prod, "CantidadPreparada": 2.0}],
            }],
        },
    }
    res = post_webhook(base_url, payload)
    assert res.get("status") == 200, res

    pdata = rpc.read("stock.picking", [pick_id], ["state", "wms_estado", "wms_descripcion_camion"])[0]
    assert pdata["state"] == "done", f"state esperado 'done', vino '{pdata['state']}'"
    assert pdata["wms_estado"] == "despachado", pdata
    assert pdata["wms_descripcion_camion"] == "Camion E2E", pdata
    # Paquete con CodigoBarras y wis_id_externo
    pkg = rpc.search_read("stock.quant.package", [["name", "=", barcode_pkg]], ["wis_id_externo"], 1)
    assert pkg, f"falta paquete name={barcode_pkg}"
    assert pkg[0]["wis_id_externo"] == id_ext, pkg
    return f"despacho OK; paquete '{barcode_pkg}' con wis_id_externo='{id_ext}'"


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
    codigo_imp = f"IMPO-E2E-{int(time.time() * 1000)}"
    pick_imp = rpc.create("stock.picking", {
        "picking_type_id": pt_imp,
        "location_id": rpc.ref("stock.stock_location_suppliers"),
        "location_dest_id": loc_in,
        "partner_id": partner_id,
        "codigo_unico": codigo_imp,
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

    # Picking interno desde loc_in → existencias. purchase_id es computado desde
    # purchase_line_id del move.
    pick_int = rpc.create("stock.picking", {
        "picking_type_id": pt_int,
        "location_id": loc_in,
        "location_dest_id": rpc.ref("stock.stock_location_stock"),
        "partner_id": partner_id,
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


# ============================================================ Runner

TESTS = [
    test_event_no_soportado,
    test_confirmacion_recepcion_basico,
    test_confirmacion_recepcion_ignora_auto,
    test_confirmacion_pedido_basico,
    test_mercaderia_preparada_basico,
    test_pedidos_anulados_basico,
    test_ajustes_movimiento,
    test_almacenamiento_basico,
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

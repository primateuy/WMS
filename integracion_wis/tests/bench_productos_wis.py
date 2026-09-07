#!/usr/bin/env python3
"""Benchmark de la integración de productos con WIS — SIN Odoo de por medio.

Objetivo (acordado en la reunión del requerimiento "Integración de Productos
con WIS"): separar las tres componentes del tiempo total de una integración
masiva de variantes:

  1. Tiempo de respuesta propio de WIS  -> lo mide ESTE script.
  2. Tiempo de procesamiento de Odoo    -> total observado en Odoo menos (1).
  3. Tiempo de la lógica actual         -> se deduce comparando el modo
     "una llamada por producto" (lo que hace hoy `Product.enviarWS`) contra
     el modo "lote" (lo que hace `insertarProductosMasivo`).

El script NO escribe nada en Odoo. Golpea la API de WIS directamente con
productos de prueba, en modo simulación por defecto (`--dry-run`), que arma
los payloads y mide todo MENOS el POST final.

Uso:

    # Medición real contra el ambiente de pruebas de WIS
    python tests/bench_productos_wis.py \\
        --api-link https://<host-wis>/api \\
        --token-url https://<host-wis>/connect/token \\
        --client-id <id> --client-secret <secret> --empresa 1 \\
        --n 232 --chunks 1,50,100,200 --ejecutar

    # Las credenciales también se pueden pasar por variables de entorno:
    #   WIS_API_LINK, WIS_TOKEN_URL, WIS_CLIENT_ID, WIS_CLIENT_SECRET, WIS_EMPRESA

Los productos de prueba usan el prefijo BENCH- para que sean identificables
y descartables en WIS. Con `--ejecutar` se dan de alta de verdad: usar
únicamente contra un ambiente de PRUEBAS.
"""
import argparse
import os
import statistics
import sys
import time

import requests

TIMEOUT = 60
PREFIJO_BENCH = "BENCH"


# --------------------------------------------------------------------------
# Utilidades de medición
# --------------------------------------------------------------------------
class Medicion:
    """Acumula tiempos de una serie de llamadas y calcula percentiles."""

    def __init__(self, etiqueta):
        self.etiqueta = etiqueta
        self.tiempos = []
        self.errores = []

    def registrar(self, segundos):
        self.tiempos.append(segundos)

    def registrar_error(self, msg):
        self.errores.append(msg)

    @property
    def total(self):
        return sum(self.tiempos)

    def percentil(self, p):
        if not self.tiempos:
            return 0.0
        datos = sorted(self.tiempos)
        k = (len(datos) - 1) * (p / 100.0)
        f, c = int(k), min(int(k) + 1, len(datos) - 1)
        return datos[f] + (datos[c] - datos[f]) * (k - f)

    def resumen(self):
        if not self.tiempos:
            return f"{self.etiqueta}: sin datos ({len(self.errores)} errores)"
        return (
            f"{self.etiqueta}: n={len(self.tiempos)} "
            f"total={self.total:.2f}s "
            f"media={statistics.mean(self.tiempos):.3f}s "
            f"p50={self.percentil(50):.3f}s "
            f"p95={self.percentil(95):.3f}s "
            f"min={min(self.tiempos):.3f}s max={max(self.tiempos):.3f}s "
            f"errores={len(self.errores)}"
        )


class ClienteWIS:
    """Cliente mínimo de la API de WIS: token + POST con reintento de token."""

    def __init__(self, api_link, token_url, client_id, client_secret, empresa, dry_run=True):
        self.api_link = self._limpiar_url(api_link)
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.empresa = empresa
        self.dry_run = dry_run
        self.token = None
        self.session = requests.Session()
        self.medicion_token = Medicion("Token (client_credentials)")

    @staticmethod
    def _limpiar_url(url):
        url = (url or "").strip()
        while url.endswith("/index.html") or url.endswith("/"):
            url = url[:-11] if url.endswith("/index.html") else url[:-1]
        return url

    def renovar_token(self):
        """Mide el costo de obtener el token. Es un costo fijo por sesión."""
        t0 = time.perf_counter()
        req = self.session.post(
            self.token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "api",
                "grant_type": "client_credentials",
            },
            timeout=TIMEOUT,
        )
        self.medicion_token.registrar(time.perf_counter() - t0)
        if req.status_code != 200:
            raise RuntimeError(f"No se pudo obtener el token: {req.status_code} - {req.text}")
        self.token = req.json().get("access_token")
        return self.token

    def post(self, ruta, body, medicion):
        """POST cronometrado. En dry-run no golpea la red."""
        if self.dry_run:
            medicion.registrar(0.0)
            return {"dryRun": True}

        if not self.token:
            self.renovar_token()

        t0 = time.perf_counter()
        try:
            req = self.session.post(
                url=self.api_link + ruta,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "accept-language": "es",
                    "Authorization": f"Bearer {self.token}",
                },
                timeout=TIMEOUT,
            )
            transcurrido = time.perf_counter() - t0
        except Exception as e:
            medicion.registrar_error(str(e))
            return None

        medicion.registrar(transcurrido)
        if req.status_code != 200:
            medicion.registrar_error(f"{req.status_code}: {req.text[:300]}")
            return None
        try:
            return req.json()
        except ValueError:
            medicion.registrar_error(f"respuesta no JSON: {req.text[:300]}")
            return None

    def get(self, ruta, params, medicion):
        """GET cronometrado (se usa para GetCodigoBarras)."""
        if self.dry_run:
            medicion.registrar(0.0)
            return {"dryRun": True}

        if not self.token:
            self.renovar_token()

        t0 = time.perf_counter()
        try:
            req = self.session.get(
                url=self.api_link + ruta,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=TIMEOUT,
            )
            transcurrido = time.perf_counter() - t0
        except Exception as e:
            medicion.registrar_error(str(e))
            return None

        medicion.registrar(transcurrido)
        return req


# --------------------------------------------------------------------------
# Armado de payloads (espejo exacto de models.py)
# --------------------------------------------------------------------------
def producto_bench(indice, corrida):
    """Un producto de prueba con la misma forma que `_build_producto_payload`."""
    codigo = f"{PREFIJO_BENCH}-{corrida}-{indice:05d}"
    return {
        "codigoProducto": codigo,
        "codigo": codigo,
        "descripcion": f"Producto de benchmark {indice} - corrida {corrida}",
        "familia": 1,
        "unidadMedida": "UND",
        "clase": 1,
        "ramo": 1,
        "pesoNeto": 0.5,
        "precioVenta": 100.0,
        "categoria1": "BENCHMARK",
        "unidadBulto": 1,
        "activo": True,
        "tipoManejoFecha": "D",
        "manejoIdentificador": "P",
    }


def barcode_bench(producto):
    return {
        "codigo": f"779{producto['codigo'].replace('-', '')[:10]}",
        "producto": producto["codigo"],
        "tipoCodigo": 13,
        "prioridadUso": 1,
        "cantidadEmbalaje": 1,
        "tipoOperacion": "A",
    }


# --------------------------------------------------------------------------
# Escenarios
# --------------------------------------------------------------------------
def escenario_por_producto(cliente, productos, con_barcode):
    """Modo ACTUAL de Odoo: 1 request por producto (+2 más si tiene barcode).

    Es exactamente lo que hace hoy `Product.enviarWS()` -> `insertarProducto()`
    y, si el producto tiene barcode, `saveBarcode()` -> `existeBarcode()` +
    `insertarBarcode()`.
    """
    m_prod = Medicion("  /Producto/CreateOrUpdate (1 producto por request)")
    m_get_bc = Medicion("  /CodigoBarras/GetCodigoBarras (1 por request)")
    m_post_bc = Medicion("  /CodigoBarras/CreateUpdateOrDelete (1 por request)")

    t0 = time.perf_counter()
    for prod in productos:
        cliente.post(
            "/Producto/CreateOrUpdate",
            {
                "empresa": cliente.empresa,
                "dsReferencia": f"PRODUCTO: {prod['descripcion']} desde Odoo",
                "productos": [prod],
            },
            m_prod,
        )
        if con_barcode:
            bc = barcode_bench(prod)
            cliente.get("/CodigoBarras/GetCodigoBarras",
                        {"empresa": cliente.empresa, "codigo": bc["codigo"]},
                        m_get_bc)
            cliente.post(
                "/CodigoBarras/CreateUpdateOrDelete",
                {
                    "empresa": cliente.empresa,
                    "dsReferencia": "CODIGO DE BARRA agregado desde Odoo",
                    "archivo": "Archivo",
                    "codigosDeBarras": [bc],
                },
                m_post_bc,
            )
    pared = time.perf_counter() - t0

    mediciones = [m_prod] + ([m_get_bc, m_post_bc] if con_barcode else [])
    return pared, mediciones


def escenario_por_lote(cliente, productos, chunk_size, con_barcode):
    """Modo PROPUESTO: N productos por request (`insertarProductosMasivo`)."""
    m_prod = Medicion(f"  /Producto/CreateOrUpdate (lotes de {chunk_size})")
    m_bc = Medicion(f"  /CodigoBarras/CreateUpdateOrDelete (lotes de {chunk_size})")

    t0 = time.perf_counter()
    for i in range(0, len(productos), chunk_size):
        chunk = productos[i:i + chunk_size]
        cliente.post(
            "/Producto/CreateOrUpdate",
            {
                "empresa": cliente.empresa,
                "dsReferencia": f"Sincronización masiva desde Odoo - lote {i // chunk_size + 1}",
                "productos": chunk,
            },
            m_prod,
        )
        if con_barcode:
            cliente.post(
                "/CodigoBarras/CreateUpdateOrDelete",
                {
                    "empresa": cliente.empresa,
                    "dsReferencia": "Códigos de barras masivos desde Odoo",
                    "archivo": "Archivo",
                    "codigosDeBarras": [barcode_bench(p) for p in chunk],
                },
                m_bc,
            )
    pared = time.perf_counter() - t0

    return pared, [m_prod] + ([m_bc] if con_barcode else [])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Benchmark de integración de productos con WIS (fuera de Odoo)."
    )
    parser.add_argument("--api-link", default=os.environ.get("WIS_API_LINK"))
    parser.add_argument("--token-url", default=os.environ.get("WIS_TOKEN_URL"))
    parser.add_argument("--client-id", default=os.environ.get("WIS_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("WIS_CLIENT_SECRET"))
    parser.add_argument("--empresa", type=int,
                        default=int(os.environ.get("WIS_EMPRESA", 1)))
    parser.add_argument("--n", type=int, default=232,
                        help="Cantidad de variantes a simular (default: 232, el caso reportado).")
    parser.add_argument("--chunks", default="50,100,200",
                        help="Tamaños de lote a comparar, separados por coma.")
    parser.add_argument("--sin-barcode", action="store_true",
                        help="No medir el circuito de códigos de barras.")
    parser.add_argument("--saltear-individual", action="store_true",
                        help="No correr el escenario 1-request-por-producto (es el lento).")
    parser.add_argument("--ejecutar", action="store_true",
                        help="Golpea WIS de verdad. Sin este flag corre en dry-run. "
                             "USAR SOLO CONTRA AMBIENTE DE PRUEBAS.")
    args = parser.parse_args()

    dry_run = not args.ejecutar
    con_barcode = not args.sin_barcode

    if not dry_run and not all([args.api_link, args.token_url,
                                args.client_id, args.client_secret]):
        parser.error("Con --ejecutar hacen falta --api-link, --token-url, "
                     "--client-id y --client-secret (o sus variables de entorno).")

    cliente = ClienteWIS(args.api_link or "", args.token_url or "",
                         args.client_id or "", args.client_secret or "",
                         args.empresa, dry_run=dry_run)

    corrida = time.strftime("%Y%m%d%H%M%S")
    chunk_sizes = [int(c.strip()) for c in args.chunks.split(",") if c.strip()]

    print("=" * 78)
    print("BENCHMARK — Integración de productos con WIS")
    print("=" * 78)
    print(f"Modo         : {'DRY-RUN (no se golpea la red)' if dry_run else 'EJECUCIÓN REAL'}")
    print(f"Variantes    : {args.n}")
    print(f"Lotes        : {chunk_sizes}")
    print(f"Barcodes     : {'sí' if con_barcode else 'no'}")
    print(f"API          : {cliente.api_link or '(no configurada)'}")
    print()

    if dry_run:
        print("Corriendo en dry-run: los tiempos van a dar 0. Sirve para validar los")
        print("payloads y el conteo de requests. Agregá --ejecutar para medir de verdad.")
        print()

    productos = [producto_bench(i, corrida) for i in range(args.n)]

    # Costo del token (una vez por sesión)
    if not dry_run:
        cliente.renovar_token()
        print(cliente.medicion_token.resumen())
        print()

    resultados = []

    # Escenario 1: el comportamiento actual
    if not args.saltear_individual:
        print("-" * 78)
        print("ESCENARIO A — comportamiento ACTUAL (1 request por producto)")
        print("-" * 78)
        pared, mediciones = escenario_por_producto(cliente, productos, con_barcode)
        requests_totales = sum(len(m.tiempos) for m in mediciones)
        for m in mediciones:
            print(m.resumen())
        print(f"  >> requests: {requests_totales} | tiempo de pared: {pared:.2f}s")
        print()
        resultados.append(("actual (1 x producto)", requests_totales, pared))

    # Escenario 2: por lotes, para cada tamaño de chunk
    for chunk_size in chunk_sizes:
        print("-" * 78)
        print(f"ESCENARIO B — por LOTES de {chunk_size}")
        print("-" * 78)
        pared, mediciones = escenario_por_lote(cliente, productos, chunk_size, con_barcode)
        requests_totales = sum(len(m.tiempos) for m in mediciones)
        for m in mediciones:
            print(m.resumen())
        print(f"  >> requests: {requests_totales} | tiempo de pared: {pared:.2f}s")
        print()
        resultados.append((f"lotes de {chunk_size}", requests_totales, pared))

    # Comparativa final
    print("=" * 78)
    print("COMPARATIVA")
    print("=" * 78)
    print(f"{'Escenario':<28}{'Requests':>10}{'Segundos':>12}{'s/variante':>14}")
    print("-" * 78)
    base = resultados[0][2] if resultados else 0
    for etiqueta, reqs, pared in resultados:
        print(f"{etiqueta:<28}{reqs:>10}{pared:>12.2f}{pared / max(args.n, 1):>14.4f}")
    if len(resultados) > 1 and base:
        mejor = min(resultados[1:], key=lambda r: r[2])
        print("-" * 78)
        print(f"Mejora del mejor lote ({mejor[0]}) sobre el modo actual: "
              f"{base / mejor[2]:.1f}x menos tiempo, "
              f"{resultados[0][1] / max(mejor[1], 1):.1f}x menos requests.")
    print()
    print("Para obtener la componente 'tiempo de Odoo': correr la misma cantidad de")
    print("variantes desde Odoo, medir el total, y restarle el tiempo de WIS de arriba.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

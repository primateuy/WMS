#!/usr/bin/env python3
"""WIS falso para replicar en local los casos de producción. No sale a la red.

- POST /connect/token            -> token fijo.
- POST/GET /api/<Recurso>/<Op>   -> 200 con la forma que devuelve WIS; cada pedido
  recibido se registra en log/wis_requests.jsonl.
- /Pedido/Create con fechaEntrega anterior a hoy -> 400 con el mismo mensaje que WIS
  devolvió en producción el 22-10 (caso C10).
- Con el archivo log/wis_rechaza_anulacion, la anulación de una recepción responde 400.

Uso: python3 stub_wis.py [puerto]   (default 8765)
"""
import datetime
import itertools
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log', 'wis_requests.jsonl')
_interfaz = itertools.count(900001)


def _fecha_vencida(body):
    hoy = datetime.date.today()
    for ped in (body or {}).get('pedidos') or []:
        raw = ped.get('fechaEntrega') or ''
        for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%Y-%m-%dT%H:%M:%S'):
            try:
                if datetime.datetime.strptime(raw[:19], fmt).date() < hoy:
                    return raw
                break
            except ValueError:
                continue
    return None


class Handler(BaseHTTPRequestHandler):
    def _leer(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw or b'null')
        except ValueError:
            return raw.decode('utf-8', 'replace')

    def _responder(self, status, data):
        out = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _registrar(self, metodo, body, status, resp):
        with open(LOG, 'a') as f:
            f.write(json.dumps({'ts': datetime.datetime.now().isoformat(), 'metodo': metodo,
                                'path': self.path, 'body': body, 'status': status,
                                'resp': resp}, ensure_ascii=False) + '\n')

    def _procesar(self, metodo):
        body = self._leer()
        if self.path.startswith('/connect/token'):
            body = '(credenciales omitidas)'
            status, resp = 200, {'access_token': 'token-local', 'expires_in': 86400}
        else:
            nro = next(_interfaz)
            status, resp = 200, {'codigoEmpresa': 6005, 'codigoInterfaz': 503, 'numeroInterfaz': nro}
            if '/Pedido/Create' in self.path:
                vencida = _fecha_vencida(body if isinstance(body, dict) else {})
                if vencida:
                    status = 400
                    resp = {'type': 'https://tools.ietf.org/html/rfc7231#section-6.5.1',
                            'title': f"Error Interfaz Nro. '{nro}'", 'status': 400,
                            'detail': json.dumps([{'ItemId': 1, 'Messages': [
                                f"'FechaEntrega': La fecha ingresada '{vencida}' no puede ser "
                                f"menor a la fecha 'Emitida'."]}]),
                            'instance': '/Pedido/Create'}
            elif ('AnulacionReferenciaRecepcion' in self.path and os.path.exists(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log', 'wis_rechaza_anulacion'))):
                status = 400
                resp = {'title': f"Error Interfaz Nro. '{nro}'", 'status': 400,
                        'detail': json.dumps([{'ItemId': 1, 'Messages': ['La referencia tiene recepciones en curso.']}])}
            elif '/GetProducto' in self.path or '/GetAgente' in self.path:
                resp = {'codigoEmpresa': 6005, 'existe': True, 'cantidadGenerica': 0}
            elif '/ConsultaDeStock/GetData' in self.path:
                resp = {'registros': [], 'totalRegistros': 0}
        self._registrar(metodo, body, status, resp)
        self._responder(status, resp)

    def do_POST(self):
        self._procesar('POST')

    def do_GET(self):
        self._procesar('GET')

    def do_PUT(self):
        self._procesar('PUT')

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(('127.0.0.1', puerto), Handler).serve_forever()

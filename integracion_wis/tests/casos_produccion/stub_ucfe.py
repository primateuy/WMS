#!/usr/bin/env python3
"""UCFE (Uruware) falso para replicar en local. Ningún CFE sale a DGI.

- .../rest/Invoke con TipoMensaje 310 (firma): aprueba y numera (serie A, desde 970001).
  Si el XML del CFE no trae el bloque <Receptor>, responde 31 con el mismo
  mensaje que Uruware dio en producción a Portones (caso C7).
- Otros TipoMensaje (consulta de estado): 00.
- ObtenerPdf*: 404 (Odoo lo registra y sigue, igual que si UCFE no devolviera PDF).

UCFE_DEMORA (segundos, env) o el archivo log/ucfe_demora simula la latencia por emisión, para reproducir los cortes por tiempo.
Uso: python3 stub_ucfe.py [puerto]   (default 8766)
"""
import datetime
import itertools
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log', 'ucfe_requests.jsonl')
DEMORA_ARCHIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log', 'ucfe_demora')


def demora():
    try:
        return float(open(DEMORA_ARCHIVO).read().strip() or 0)
    except (OSError, ValueError):
        return float(os.environ.get('UCFE_DEMORA', '0'))
_numero = itertools.count(970001)

OK = ('<RespBody xmlns="http://schemas.datacontract.org/2004/07/Uruware.Ucfe.Inbox.WebService" '
      'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><ErrorCode>0</ErrorCode>'
      '<ErrorMessage i:nil="true"/><Resp xmlns:a="http://www.uruware.com/ucfe/inbox/webservice">'
      '<a:CaeNroDesde>970001</a:CaeNroDesde><a:CaeNroHasta>999999</a:CaeNroHasta>'
      '<a:Certificado>LOCAL</a:Certificado><a:CodRta>00</a:CodRta>'
      '<a:CodigoSeguridad>LOCAL1</a:CodigoSeguridad><a:DatosQr>https://local/qr/{n}</a:DatosQr>'
      '<a:FechaFirma>{fecha}</a:FechaFirma><a:IdCae>90000000001</a:IdCae>'
      '<a:MensajeRta i:nil="true"/><a:NumeroCfe>{n}</a:NumeroCfe><a:Serie>A</a:Serie>'
      '<a:TipoCfe>{tipo}</a:TipoCfe><a:VencimientoCae>2027-12-31</a:VencimientoCae></Resp></RespBody>')
ERR = ('<RespBody xmlns="http://schemas.datacontract.org/2004/07/Uruware.Ucfe.Inbox.WebService" '
       'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><ErrorCode>0</ErrorCode>'
       '<ErrorMessage i:nil="true"/><Resp xmlns:a="http://www.uruware.com/ucfe/inbox/webservice">'
       '<a:CodRta>{cod}</a:CodRta><a:MensajeRta>{msg}</a:MensajeRta></Resp></RespBody>')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            body = json.loads(raw or b'{}')
        except ValueError:
            body = {}
        req = (body or {}).get('Req') or {}
        status, out = 200, ''
        if 'ObtenerPdf' in self.path:
            status, out = 404, 'sin pdf en local'
        elif str(req.get('TipoMensaje')) == '310':
            time.sleep(demora())
            xml = req.get('CfeXmlOTexto') or ''
            if '<Receptor' not in xml:
                out = ERR.format(cod='31', msg='El CFE de tipo ERemito omite encabezado y/o receptor')
            else:
                out = OK.format(n=next(_numero), tipo=req.get('TipoCfe') or 181,
                                fecha=datetime.datetime.now().isoformat())
        else:
            out = ('<RespBody><Resp xmlns:a="x"><a:CodRta>00</a:CodRta>'
                   '<a:MensajeRta>OK</a:MensajeRta></Resp></RespBody>')
        with open(LOG, 'a') as f:
            f.write(json.dumps({'ts': datetime.datetime.now().isoformat(), 'path': self.path,
                                'tipo_mensaje': req.get('TipoMensaje'), 'uuid': req.get('Uuid'),
                                'status': status, 'cod': ('00' if 'CodRta>00<' in out else ('31' if 'CodRta>31<' in out else '')), 'resp': out[:300]}, ensure_ascii=False) + '\n')
        data = out.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    ThreadingHTTPServer(('127.0.0.1', puerto), Handler).serve_forever()

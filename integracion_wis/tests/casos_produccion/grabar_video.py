#!/usr/bin/env python3
"""Graba en video, en la interfaz de Odoo, cómo quedó cada caso de la réplica.

Uso: python3 grabar_video.py <db> <salida.mp4> log/video_*.json [...]
Requiere: playwright (chromium) y ffmpeg. Lee también log/resultado_*.json para mostrar el
resultado de los controles de cada caso.
"""
import glob
import html
import json
import os
import subprocess
import sys
import tempfile
import time
import xmlrpc.client

from playwright.sync_api import sync_playwright

URL = os.environ.get('CASOS_ODOO_URL', 'http://127.0.0.1:8099')
LOGIN = os.environ.get('CASOS_LOGIN', 'soporteforum@primate.uy')
CLAVE = os.environ.get('CASOS_CLAVE', '12345')
BASE = os.path.dirname(os.path.abspath(__file__))
ESTADO = {'done': 'Hecho', 'assigned': 'Listo', 'confirmed': 'En espera', 'waiting': 'En espera',
          'cancel': 'Cancelado', 'draft': 'Borrador'}
WIS = {'despachado': 'despachado por WIS', 'preparado': 'preparado por WIS', 'enviado': 'enviado a WIS',
       'anulado': 'anulado en WIS', 'no_integrado': 'no va a WIS', 'error_cancelacion': 'error en la cancelación',
       'sin_enviar': 'sin enviar', 'en_cola': 'en cola'}
CAPITULOS = []
T0 = [0.0]

PLACA = """<html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#14213d;font-family:-apple-system,Helvetica,Arial,sans-serif;color:#fff">
<div style="max-width:1100px;padding:40px">
<div style="font-size:22px;letter-spacing:.12em;text-transform:uppercase;color:#8ecae6">{eyebrow}</div>
<div style="font-size:46px;font-weight:700;margin:14px 0 26px;line-height:1.15">{titulo}</div>
<div style="font-size:21px;line-height:1.6">{cuerpo}</div></div></body></html>"""


def rpc(db):
    uid = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/common').authenticate(db, LOGIN, CLAVE, {})
    obj = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/object', allow_none=True)
    return lambda model, method, *a, **kw: obj.execute_kw(db, uid, CLAVE, model, method, list(a), kw)


def resultados():
    res = {}
    for f in glob.glob(os.path.join(BASE, 'log', 'resultado_*.json')):
        for caso, desc, ok, det in json.load(open(f)):
            res.setdefault(caso, []).append((desc, ok, det))
    return res


def esperar_vista(page):
    try:
        page.wait_for_selector('.o_form_view, .o_list_view, .o_kanban_view', timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def subtitulo(page, texto):
    page.evaluate("""t => {
        let d = document.getElementById('casos-sub');
        if (!d) { d = document.createElement('div'); d.id = 'casos-sub'; document.body.appendChild(d);
          Object.assign(d.style, {position:'fixed', left:'0', right:'0', bottom:'0', zIndex:99999,
            background:'rgba(20,33,61,.92)', color:'#fff', font:'600 20px -apple-system,Helvetica,Arial',
            padding:'14px 24px'}); }
        d.textContent = t; }""", texto)


def placa(page, eyebrow, titulo, cuerpo, segundos=4):
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f:
        f.write(PLACA.format(eyebrow=html.escape(eyebrow), titulo=html.escape(titulo), cuerpo=cuerpo))
    page.goto('file://' + f.name)
    page.wait_for_timeout(segundos * 1000)


def mostrar(page, modelo, rid, texto, segundos=4, vista='form'):
    page.goto(f'{URL}/web#id={rid}&model={modelo}&view_type={vista}')
    esperar_vista(page)
    subtitulo(page, texto)
    page.wait_for_timeout(segundos * 1000)
    page.mouse.wheel(0, 500)
    page.wait_for_timeout(1500)


def mostrar_lista(page, accion_xmlid, dominio_txt, texto, segundos=4):
    page.goto(f'{URL}/web#action={accion_xmlid}')
    esperar_vista(page)
    if dominio_txt:
        caja = page.locator('.o_searchview_input').first
        caja.fill(dominio_txt)
        page.keyboard.press('Enter')
        page.wait_for_timeout(1500)
    subtitulo(page, texto)
    page.wait_for_timeout(segundos * 1000)


def main():
    db, salida, archivos = sys.argv[1], sys.argv[2], sys.argv[3:]
    call = rpc(db)
    res = resultados()
    entradas = []
    for f in archivos:
        entradas += json.load(open(f))
    carpeta = tempfile.mkdtemp(prefix='video_casos_')
    with sync_playwright() as p:
        navegador = p.chromium.launch()
        ctx = navegador.new_context(viewport={'width': 1440, 'height': 900}, record_video_dir=carpeta,
                                    record_video_size={'width': 1440, 'height': 900}, locale='es-UY')
        page = ctx.new_page()
        T0[0] = time.time()
        page.goto(f'{URL}/web/login?db={db}')
        page.fill('input[name="login"]', LOGIN)
        page.fill('input[name="password"]', CLAVE)
        page.locator('form.oe_login_form button[type="submit"]').first.click()
        page.wait_for_url('**/web**', timeout=30000)
        page.wait_for_timeout(2000)
        total = sum(len(v) for v in res.values())
        bien = sum(ok for v in res.values() for _, ok, _ in v)
        placa(page, 'Integración Odoo ↔ WIS · réplica local', 'Casos de producción y variantes',
              f'Copia local de la base con WIS y Uruware simulados (ningún comprobante sale a DGI).<br>'
              f'Controles: <b>{bien} de {total}</b> en verde.<br>Cada caso muestra cómo quedó en Odoo de punta a cabo.', 6)
        for e in entradas:
            CAPITULOS.append((time.time() - T0[0], e['caso'], e['titulo']))
            controles = res.get(e['caso'], [])
            filas = ''.join(f"<div>{'✔' if ok else '✘'} {html.escape(d)}</div>" for d, ok, _ in controles)
            placa(page, e['caso'], e['titulo'], filas or 'Sin controles registrados.', 4 + len(controles) // 2)
            ids = e['pickings']
            if not ids:
                continue
            datos = call('stock.picking', 'read', ids, fields=['name', 'codigo_unico', 'picking_type_id', 'state', 'wms_estado'])
            codigos = sorted({d['codigo_unico'] for d in datos if d['codigo_unico']})
            if codigos:
                notifs = call('wis.notificacion', 'search', ['|'] * (len(codigos) - 1)
                              + [('payload', 'ilike', c) for c in codigos], limit=2, order='id')
                for nid in notifs:
                    mostrar(page, 'wis.notificacion', nid,
                            f"{e['caso']} · Notificación de WIS: se recibió, se respondió enseguida y se procesó en segundo plano", 3)
            for d in datos:
                mostrar(page, 'stock.picking', d['id'],
                        f"{e['caso']} · {d['picking_type_id'][1].split(': ')[-1]} {d['name']} · {ESTADO.get(d['state'], d['state'])} · {WIS.get(d['wms_estado'], d['wms_estado'])}", 3)
            # siguiente paso (entrega a tienda) y recepción en la tienda
            moves = call('stock.move', 'search_read', [('picking_id', 'in', ids)], fields=['move_dest_ids'])
            dest = sorted({m for mv in moves for m in mv['move_dest_ids']})
            if dest:
                dpk = call('stock.move', 'read', dest, fields=['picking_id', 'move_dest_ids'])
                entregas = sorted({m['picking_id'][0] for m in dpk if m['picking_id']} - set(ids))
                for eid in entregas[:2]:
                    mostrar(page, 'stock.picking', eid, f"{e['caso']} · Entrega a tienda/cliente: sólo lo despachado", 3)
                rec = sorted({m for mv in dpk for m in mv['move_dest_ids']})
                if rec:
                    rpk = call('stock.move', 'read', rec, fields=['picking_id'])
                    for rid in sorted({m['picking_id'][0] for m in rpk if m['picking_id']})[:1]:
                        mostrar(page, 'stock.picking', rid, f"{e['caso']} · Recepción en la tienda", 3)
            if codigos:
                filas = call('wms.integracion.log', 'search_read', ['|', ('codigo_unico', 'in', codigos), ('picking_id', 'in', ids)],
                             fields=['fecha', 'proceso', 'resultado', 'texto'], order='id', limit=14)
                tabla = ''.join(
                    f"<div style='font-size:16px;margin:4px 0'><b style='color:{'#90be6d' if f['resultado'] == 'exito' else '#f9c74f' if f['resultado'] == 'pendiente' else '#f94144'}'>"
                    f"{html.escape(f['resultado'] or '')}</b> · {html.escape(f['proceso'] or '')} · {html.escape((f['texto'] or '')[:150])}</div>"
                    for f in filas)
                placa(page, f"{e['caso']} · log de integración en Odoo", ', '.join(codigos)[:80], tabla or 'Sin registros.', 6)
        placa(page, 'Fin', 'Integración Odoo ↔ WIS', f'Controles en verde: <b>{bien} de {total}</b>.', 4)
        video = page.video.path()
        ctx.close()
        navegador.close()
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', video, '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-preset', 'veryfast', '-crf', '24', salida], check=True)
    indice = os.path.splitext(salida)[0] + '_capitulos.txt'
    with open(indice, 'w') as f:
        for seg, caso, titulo in CAPITULOS:
            f.write(f"{int(seg // 60):02d}:{int(seg % 60):02d}  {caso} · {titulo}\n")
    print(salida, indice)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Comprobaciones previas al merge de una PR (las mismas que se hacían a mano):

1. node --check de CADA <script> inline de index.html y editor.html, y de sw.js
   (node --check sobre el HTML entero no funciona).
2. Las tres marcas de versión coinciden: APP_VERSION en index.html, el ?v= del registro del
   service worker en index.html y CACHE en sw.js. Si falta una, la PWA sirve caché vieja.
"""
import os, re, subprocess, sys, tempfile

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
errores = []


def leer(nombre):
    with open(os.path.join(RAIZ, nombre), encoding='utf-8') as f:
        return f.read()


def node_check(codigo, etiqueta):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(codigo)
        ruta = f.name
    try:
        r = subprocess.run(['node', '--check', ruta], capture_output=True, text=True)
    finally:
        os.unlink(ruta)
    if r.returncode != 0:
        errores.append('Error de sintaxis en %s:\n%s' % (etiqueta, r.stderr.strip()[:800]))
        return False
    return True


for nombre in ('index.html', 'editor.html'):
    if not os.path.exists(os.path.join(RAIZ, nombre)):
        continue
    html = leer(nombre)
    bloques = [b for b in re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S) if b.strip()]
    ok = sum(node_check(b, '%s (script %d)' % (nombre, i + 1)) for i, b in enumerate(bloques))
    print('%s: %d/%d scripts OK' % (nombre, ok, len(bloques)))

if node_check(leer('sw.js'), 'sw.js'):
    print('sw.js: OK')

html = leer('index.html')
sw = leer('sw.js')
m_app = re.search(r"const APP_VERSION\s*=\s*'(vEXT_\d+|v\d+)'", html)
m_reg = re.search(r"sw\.js\?v=([A-Za-z0-9_]+)", html)
m_cache = re.search(r"const CACHE\s*=\s*'entreno-boyle-([A-Za-z0-9_]+)'", sw)
if not (m_app and m_reg and m_cache):
    errores.append('No se encontró alguna marca de versión (APP_VERSION, sw.js?v= o CACHE).')
else:
    app = m_app.group(1)
    esperado = app[1:] if app.startswith('vEXT_') else app  # vEXT_034 -> EXT_034
    reg = m_reg.group(1)
    cache = m_cache.group(1)
    print('Versión: APP_VERSION=%s · sw.js?v=%s · CACHE=%s' % (app, reg, cache))
    if reg != esperado:
        errores.append("El registro del service worker usa ?v=%s pero APP_VERSION es %s (debería ser ?v=%s)." % (reg, app, esperado))
    if cache != app:
        errores.append("CACHE en sw.js es 'entreno-boyle-%s' pero APP_VERSION es %s." % (cache, app))

if errores:
    print('\n❌ ' + '\n❌ '.join(errores))
    sys.exit(1)
print('\n✅ Todo correcto')

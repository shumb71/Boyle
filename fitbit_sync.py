#!/usr/bin/env python3
"""
fitbit_sync.py — Sincroniza datos de Fitbit Air (via Google Health API v4) y genera
fitbit_historial.json en el mismo formato que garmin_historial.json, para que la app
Entreno PRO lo descargue directamente desde GitHub Pages (fetchFitbitAuto en index.html).

IMPORTANTE — API MUY NUEVA (lanzada 2026), léelo antes de correrlo la primera vez:
La Google Health API v4 es reciente y su documentación pública no incluye ejemplos completos
de respuesta JSON para cada tipo de dato. Este script está escrito con los nombres de tipo de
dato y forma de endpoint documentados hasta ahora (developers.google.com/health), pero es MUY
POSIBLE que algún nombre de tipo o campo de respuesta no coincida exactamente la primera vez
que lo ejecutes. Por eso:
  - Cada tipo de dato se pide por separado, con manejo de errores individual (si uno falla,
    los demás se siguen sincronizando).
  - Se imprime en el log de GitHub Actions la respuesta cruda de cada tipo que falle o tenga
    forma inesperada, para poder ajustar el parseo rápidamente.
  - Revisa el log del primer Run antes de asumir que está funcionando al 100%.

Variables de entorno requeridas (configúralas como Secrets del repo en GitHub):
  FITBIT_CLIENT_ID       - Client ID de OAuth (Google Cloud Console)
  FITBIT_CLIENT_SECRET   - Client Secret de OAuth
  FITBIT_REFRESH_TOKEN   - Refresh token obtenido vía OAuth Playground (ver guía)

Salida: fitbit_historial.json en la raíz del repo (mismo sitio que garmin_historial.json),
publicado vía GitHub Pages en https://shumb71.github.io/Boyle/fitbit_historial.json
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4"
OUTPUT_FILE = "fitbit_historial.json"

# Cuántos días hacia atrás se sincronizan en cada ejecución. Los días ya sincronizados se
# sobreescriben (igual que Garmin), útil porque Fitbit puede tardar en consolidar datos del
# día en curso (ej. sueño de la noche anterior se cierra por la mañana).
DIAS_ATRAS = 7

# Mapeo: nombre del tipo de dato en la Google Health API -> campo local que usa Entreno PRO.
# Los nombres de tipo son los documentados en developers.google.com/health a fecha de escritura
# de este script (agosto 2026). Si alguno falla con 404, revisa el índice de tipos de datos en
# la documentación oficial y ajusta el string aquí.
DATA_TYPES = {
    "steps": "pasos",
    "calories": "calorias_dia",
    "distance": "distancia_km",
    "active-minutes": "minutos_activos",
    "resting-heart-rate": "fc_reposo",
    "heart-rate-variability": "hrv",
    "oxygen-saturation": "spo2",
    # El sueño se trata aparte (endpoint distinto, estructura por etapas)
}


def obtener_access_token():
    """Intercambia el refresh_token por un access_token nuevo (expiran ~1h)."""
    client_id = os.environ["FITBIT_CLIENT_ID"]
    client_secret = os.environ["FITBIT_CLIENT_SECRET"]
    refresh_token = os.environ["FITBIT_REFRESH_TOKEN"]

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def rango_fechas(dias_atras):
    hoy = date.today()
    for i in range(dias_atras, -1, -1):
        yield hoy - timedelta(days=i)


def pedir_daily_rollup(session, tipo, fecha_str):
    """Pide el rollup diario de un tipo de dato para una fecha concreta.
    Devuelve el valor numérico si se puede extraer, o None si falla/no hay datos.
    """
    url = f"{API_BASE}/users/me/dataTypes/{tipo}:dailyRollUp"
    params = {"date": fecha_str}
    try:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 404:
            # Tipo de dato sin datos ese día, o nombre de tipo incorrecto — no es fatal
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  {tipo} {fecha_str}: error de red/HTTP — {e}")
        return None

    # La forma exacta de la respuesta no está confirmada al 100% para todos los tipos —
    # probamos las rutas más probables y avisamos si no encontramos nada reconocible.
    valor = None
    if isinstance(data, dict):
        for clave in ("value", "total", "sum", "aggregateValue"):
            if clave in data:
                valor = data[clave]
                break
        if valor is None and "dataPoints" in data and data["dataPoints"]:
            # Fallback: promedio/último punto si viene como lista de puntos
            puntos = data["dataPoints"]
            valores = [p.get("value") for p in puntos if isinstance(p.get("value"), (int, float))]
            if valores:
                valor = sum(valores) / len(valores) if tipo in ("resting-heart-rate", "heart-rate-variability", "oxygen-saturation") else sum(valores)

    if valor is None:
        print(f"  ⚠️  {tipo} {fecha_str}: respuesta con forma inesperada, revisar manualmente:")
        print(f"      {json.dumps(data)[:300]}")
    return valor


def pedir_sueno(session, fecha_str):
    """Pide el rollup de sueño del día. Devuelve dict con minutos totales y por etapa,
    o None si no hay datos."""
    url = f"{API_BASE}/users/me/dataTypes/sleep:dailyRollUp"
    params = {"date": fecha_str}
    try:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  sleep {fecha_str}: error de red/HTTP — {e}")
        return None

    def mins(segundos_o_min):
        # Algunos endpoints de Google devuelven duración en segundos; si el número es grande
        # asumimos segundos y convertimos, si es pequeño asumimos que ya viene en minutos.
        if segundos_o_min is None:
            return 0
        return round(segundos_o_min / 60) if segundos_o_min > 1440 else round(segundos_o_min)

    total = data.get("totalSleepDuration") or data.get("value") or data.get("total")
    if total is None:
        print(f"  ⚠️  sleep {fecha_str}: respuesta con forma inesperada, revisar manualmente:")
        print(f"      {json.dumps(data)[:300]}")
        return None

    etapas = data.get("stages", {}) or {}
    return {
        "sueno_total_min": mins(total),
        "sueno_profundo_min": mins(etapas.get("deep")),
        "sueno_ligero_min": mins(etapas.get("light")),
        "sueno_rem_min": mins(etapas.get("rem")),
    }


def main():
    print("=== Fitbit sync — Google Health API v4 ===")
    try:
        access_token = obtener_access_token()
    except Exception as e:
        print(f"❌ No se pudo obtener access_token: {e}", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    # Cargar histórico previo si existe, para no perder días fuera del rango sincronizado hoy
    dias = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                previo = json.load(f)
                dias = previo.get("dias", {})
        except (json.JSONDecodeError, OSError):
            print("⚠️  No se pudo leer el fitbit_historial.json previo, empezando de cero.")

    for fecha in rango_fechas(DIAS_ATRAS):
        fecha_str = fecha.isoformat()
        print(f"Sincronizando {fecha_str}...")
        dia = dias.get(fecha_str, {})

        for tipo, campo_local in DATA_TYPES.items():
            valor = pedir_daily_rollup(session, tipo, fecha_str)
            if valor is not None:
                dia[campo_local] = round(valor, 2) if isinstance(valor, float) else valor

        sueno = pedir_sueno(session, fecha_str)
        if sueno:
            dia.update(sueno)

        if dia:
            dias[fecha_str] = dia

    salida = {
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "dias": dias,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)

    print(f"✅ Escrito {OUTPUT_FILE} con {len(dias)} días en total.")


if __name__ == "__main__":
    main()

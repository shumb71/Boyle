#!/usr/bin/env python3
"""
fitbit_sync.py — Sincroniza datos de Fitbit Air (via Google Health API v4) y genera
fitbit_historial.json en el mismo formato que garmin_historial.json, para que la app
Entreno PRO lo descargue directamente desde GitHub Pages (fetchFitbitAuto en index.html).

v2 — corregido tras el primer intento fallido (salió "dias": {} vacío). Los dos bugs reales
del v1 eran:
  1. Usaba GET a ".../dataTypes/{tipo}:dailyRollUp" — ese método es POST, no GET, y con un
     esquema de cuerpo (CivilTimeInterval) que la documentación no detalla del todo bien
     todavía. Por eso todas las peticiones fallaban silenciosamente (404/405).
  2. Los tipos "FC reposo / HRV / SpO2" son de tipo "Daily" en la API — ya vienen
     pre-agregados por día y NO soportan dailyRollUp, solo list/reconcile. Estaba pidiendo
     un método que esos tipos no ofrecen.

v2 usa el método `list` (GET, bien documentado con ejemplos reales) para todo, filtrando por
rango de fechas cuando aplica, y sumando/parseando los data points en Python.

AVISO — API MUY NUEVA (2026): la forma exacta de los campos dentro de cada data point (p.ej.
si "distance" viene en metros con clave "distance" o "meters") no está 100% confirmada en la
documentación pública a fecha de escritura. Por eso cada parser prueba varias claves plausibles
y, si no reconoce ninguna, IMPRIME el JSON crudo en el log en vez de fallar en silencio — así
la próxima iteración es cuestión de añadir la clave correcta, no de adivinar a ciegas otra vez.

Variables de entorno requeridas (Secrets del repo en GitHub):
  FITBIT_CLIENT_ID, FITBIT_CLIENT_SECRET, FITBIT_REFRESH_TOKEN
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4"
OUTPUT_FILE = "fitbit_historial.json"
DIAS_ATRAS = 8  # cuántos días hacia atrás se re-sincronizan en cada ejecución

# Tipos "Interval" (un valor por franja horaria a lo largo del día — hay que sumarlos por día)
# endpoint_id, lista de claves plausibles del valor, campo local
TIPOS_INTERVAL = [
    ("steps", ["steps", "count", "value"], "pasos"),
    ("distance", ["distance", "meters", "value"], "_distancia_m"),  # se convierte a km después
    ("active-minutes", ["minutes", "activeMinutes", "value"], "minutos_activos"),
    ("total-calories", ["kcal", "calories", "value"], "calorias_dia"),
]

# Tipos "Daily" (ya vienen un registro por día, sin necesidad de sumar)
# endpoint_id, lista de claves plausibles del valor, campo local
TIPOS_DAILY = [
    ("daily-resting-heart-rate", ["restingHeartRate", "bpm", "value"], "fc_reposo"),
    ("daily-heart-rate-variability", ["heartRateVariability", "hrv", "ms", "value"], "hrv"),
    ("daily-oxygen-saturation", ["oxygenSaturation", "spo2", "percentage", "value"], "spo2"),
]


def obtener_access_token():
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
    if not resp.ok:
        print(f"❌ Google devolvió {resp.status_code} al pedir el access_token:")
        print(f"   {resp.text}")
    resp.raise_for_status()
    return resp.json()["access_token"]


def extraer_valor(punto, claves_posibles):
    """Prueba varias claves plausibles dentro de un data point y devuelve la primera que
    encuentre como número. Devuelve None si ninguna coincide."""
    for clave in claves_posibles:
        if clave in punto and isinstance(punto[clave], (int, float)):
            return punto[clave]
    return None


def extraer_fecha_civil(punto):
    """Saca la fecha (YYYY-MM-DD) del startTime de un data point. Devuelve None si no se
    puede determinar."""
    ts = punto.get("startTime") or punto.get("civilStartTime") or punto.get("date")
    if not ts:
        return None
    try:
        return ts[:10]
    except (TypeError, IndexError):
        return None


def pedir_lista(session, tipo_endpoint, desde, hasta):
    """Llama al método list para un tipo de dato en un rango de fechas. Devuelve la lista de
    data points (posiblemente vacía) o None si la petición falla."""
    url = f"{API_BASE}/users/me/dataTypes/{tipo_endpoint}/dataPoints"
    campo_filtro = tipo_endpoint.replace("-", "_")
    filtro = (
        f'{campo_filtro}.interval.start_time >= "{desde}T00:00:00Z" AND '
        f'{campo_filtro}.interval.start_time < "{hasta}T00:00:00Z"'
    )
    try:
        resp = session.get(url, params={"filter": filtro, "pageSize": 10000}, timeout=30)
        if resp.status_code == 404:
            print(f"  ⚠️  {tipo_endpoint}: 404 — el tipo de dato o el filtro no es válido, revisar nombre de endpoint/campo de filtro.")
            return None
        if not resp.ok:
            print(f"  ⚠️  {tipo_endpoint}: HTTP {resp.status_code} — {resp.text[:300]}")
            return None
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  {tipo_endpoint}: error de red — {e}")
        return None

    puntos = data.get("dataPoints") or data.get("data_points") or []
    if not puntos and data:
        print(f"  ℹ️  {tipo_endpoint}: respuesta sin dataPoints reconocibles, forma completa:")
        print(f"      {json.dumps(data)[:400]}")
    return puntos


def procesar_intervalos(session, dias):
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()

    for tipo_endpoint, claves, campo_local in TIPOS_INTERVAL:
        print(f"Pidiendo {tipo_endpoint}...")
        puntos = pedir_lista(session, tipo_endpoint, desde, hasta)
        if puntos is None:
            continue
        acumulado_por_dia = {}
        sin_reconocer = 0
        for p in puntos:
            fecha = extraer_fecha_civil(p)
            valor = extraer_valor(p, claves)
            if fecha is None or valor is None:
                sin_reconocer += 1
                continue
            acumulado_por_dia[fecha] = acumulado_por_dia.get(fecha, 0) + valor
        if sin_reconocer and puntos:
            print(f"  ⚠️  {tipo_endpoint}: {sin_reconocer}/{len(puntos)} puntos con forma no reconocida. Ejemplo:")
            print(f"      {json.dumps(puntos[0])[:400]}")
        for fecha, total in acumulado_por_dia.items():
            if fecha not in dias:
                dias[fecha] = {}
            dias[fecha][campo_local] = round(total, 2)

    for fecha, dia in dias.items():
        if "_distancia_m" in dia:
            dia["distancia_km"] = round(dia.pop("_distancia_m") / 1000, 2)


def procesar_daily(session, dias):
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()

    for tipo_endpoint, claves, campo_local in TIPOS_DAILY:
        print(f"Pidiendo {tipo_endpoint}...")
        url = f"{API_BASE}/users/me/dataTypes/{tipo_endpoint}/dataPoints"
        try:
            resp = session.get(url, params={"pageSize": 100}, timeout=30)
            if resp.status_code == 404:
                print(f"  ⚠️  {tipo_endpoint}: 404 — revisar nombre de endpoint.")
                continue
            if not resp.ok:
                print(f"  ⚠️  {tipo_endpoint}: HTTP {resp.status_code} — {resp.text[:300]}")
                continue
            data = resp.json()
        except requests.RequestException as e:
            print(f"  ⚠️  {tipo_endpoint}: error de red — {e}")
            continue

        puntos = data.get("dataPoints") or data.get("data_points") or []
        if not puntos:
            print(f"  ℹ️  {tipo_endpoint}: sin dataPoints. Respuesta completa:")
            print(f"      {json.dumps(data)[:400]}")
            continue

        sin_reconocer = 0
        for p in puntos:
            fecha = extraer_fecha_civil(p)
            valor = extraer_valor(p, claves)
            if fecha is None or valor is None or fecha < desde or fecha >= hasta:
                sin_reconocer += 1
                continue
            if fecha not in dias:
                dias[fecha] = {}
            dias[fecha][campo_local] = round(valor, 2) if isinstance(valor, float) else valor
        if sin_reconocer == len(puntos) and puntos:
            print(f"  ⚠️  {tipo_endpoint}: ningún punto reconocido de {len(puntos)}. Ejemplo:")
            print(f"      {json.dumps(puntos[0])[:400]}")


def procesar_sueno(session, dias):
    print("Pidiendo sleep...")
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/users/me/dataTypes/sleep/dataPoints"
    filtro = f'sleep.interval.start_time >= "{desde}T00:00:00Z" AND sleep.interval.start_time < "{hasta}T00:00:00Z"'
    try:
        resp = session.get(url, params={"filter": filtro, "pageSize": 100}, timeout=30)
        if resp.status_code == 404:
            print("  ⚠️  sleep: 404 — revisar nombre de endpoint/campo de filtro.")
            return
        if not resp.ok:
            print(f"  ⚠️  sleep: HTTP {resp.status_code} — {resp.text[:300]}")
            return
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  sleep: error de red — {e}")
        return

    puntos = data.get("dataPoints") or data.get("data_points") or []
    if not puntos:
        print("  ℹ️  sleep: sin dataPoints. Respuesta completa:")
        print(f"      {json.dumps(data)[:400]}")
        return

    for p in puntos:
        fecha = extraer_fecha_civil(p)
        if fecha is None:
            continue
        summary = p.get("summary") or p.get("sleepSummary") or {}
        total_seg = summary.get("totalSleepDuration") or p.get("totalSleepDuration")
        if total_seg is None:
            print(f"  ⚠️  sleep {fecha}: forma no reconocida, punto completo:")
            print(f"      {json.dumps(p)[:400]}")
            continue

        def mins(v):
            if v is None:
                return 0
            return round(v / 60) if v > 1440 else round(v)

        if fecha not in dias:
            dias[fecha] = {}
        dias[fecha]["sueno_total_min"] = mins(total_seg)
        etapas = summary.get("stages", {}) or {}
        if etapas.get("deep") is not None:
            dias[fecha]["sueno_profundo_min"] = mins(etapas.get("deep"))
        if etapas.get("light") is not None:
            dias[fecha]["sueno_ligero_min"] = mins(etapas.get("light"))
        if etapas.get("rem") is not None:
            dias[fecha]["sueno_rem_min"] = mins(etapas.get("rem"))


def main():
    print("=== Fitbit sync v2 — Google Health API v4 ===")
    try:
        access_token = obtener_access_token()
    except Exception as e:
        print(f"❌ No se pudo obtener access_token: {e}", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    dias = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                previo = json.load(f)
                dias = previo.get("dias", {})
        except (json.JSONDecodeError, OSError):
            print("⚠️  No se pudo leer el fitbit_historial.json previo, empezando de cero.")

    procesar_intervalos(session, dias)
    procesar_daily(session, dias)
    procesar_sueno(session, dias)

    salida = {
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "dias": dias,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)

    print(f"✅ Escrito {OUTPUT_FILE} con {len(dias)} días en total.")


if __name__ == "__main__":
    main()

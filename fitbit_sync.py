#!/usr/bin/env python3
"""
fitbit_sync.py — Sincroniza datos de Fitbit Air (via Google Health API v4) y genera
fitbit_historial.json en el mismo formato que garmin_historial.json, para que la app
Entreno PRO lo descargue directamente desde GitHub Pages (fetchFitbitAuto en index.html).

v4 — steps y FC reposo ya funcionaban en v3. Correcciones de esta versión, confirmadas con
datos reales del log de un Run:

  - distance: el valor viene en "millimeters" (como string), no "meters"/"distance"/"value".
  - active-minutes: NO es un campo único — es un array "activeMinutesByActivityLevel" con un
    objeto {"activityLevel":..., "activeMinutes": "N"} por nivel de intensidad. Se suman todos.
  - total-calories (dailyRollUp): el cuerpo POST anterior daba 400 porque "range" no acepta
    "startTime"/"endTime" directos. Se prueba con "civilStartTime"/"civilEndTime" anidando
    {"date": {...}}, siguiendo el mismo patrón usado en el resto de la API. Sigue siendo una
    suposición fundamentada, no 100% confirmada — si vuelve a fallar, el log lo dirá.
  - daily-heart-rate-variability: campo real "averageHeartRateVariabilityMilliseconds".
  - daily-oxygen-saturation: campo real "averagePercentage".
  - sleep: NO trae un resumen ("summary") — trae una lista "stages" con tramos individuales
    (tipo AWAKE/LIGHT/DEEP/REM, cada uno con su propio startTime/endTime). Se suman las
    duraciones por tipo en Python. La fecha civil tampoco viene directa en el intervalo de
    sleep (a diferencia de steps/distance/etc.) — se calcula a partir de startTime + utcOffset.

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
DIAS_ATRAS = 8
DEBUG_LEN = 2000


def log_json(prefijo, obj):
    print(prefijo)
    print(f"    {json.dumps(obj, ensure_ascii=False)[:DEBUG_LEN]}")


def fecha_desde_civil_date(date_obj):
    if not date_obj:
        return None
    y, m, d = date_obj.get("year"), date_obj.get("month"), date_obj.get("day")
    if not (y and m and d):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def fecha_civil_desde_utc(iso_str, utc_offset_str):
    """Fecha local a partir de un timestamp UTC + offset tipo '7200s'."""
    dt = parse_iso(iso_str)
    if dt is None:
        return None
    offset_sec = 0
    if utc_offset_str and utc_offset_str.endswith("s"):
        try:
            offset_sec = int(utc_offset_str[:-1])
        except ValueError:
            pass
    return (dt + timedelta(seconds=offset_sec)).date().isoformat()


def a_numero(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


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


def pedir_lista_interval(session, tipo_endpoint):
    """GET .../dataPoints con filtro de rango de fechas. Devuelve la lista de puntos o None."""
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/users/me/dataTypes/{tipo_endpoint}/dataPoints"
    campo_filtro = tipo_endpoint.replace("-", "_")
    filtro = (
        f'{campo_filtro}.interval.start_time >= "{desde}T00:00:00Z" AND '
        f'{campo_filtro}.interval.start_time < "{hasta}T00:00:00Z"'
    )
    try:
        resp = session.get(url, params={"filter": filtro, "pageSize": 10000}, timeout=30)
        if not resp.ok:
            print(f"  ⚠️  {tipo_endpoint}: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
            return None
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  {tipo_endpoint}: error de red — {e}")
        return None

    puntos = data.get("dataPoints") or data.get("data_points") or []
    if not puntos:
        log_json(f"  ℹ️  {tipo_endpoint}: sin dataPoints, respuesta completa:", data)
    return puntos


def procesar_steps(session, dias):
    print("Pidiendo steps...")
    puntos = pedir_lista_interval(session, "steps")
    if not puntos:
        return
    acumulado = {}
    sin_reconocer = 0
    for p in puntos:
        sub = p.get("steps") or {}
        civil = (sub.get("interval") or {}).get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        valor = a_numero(sub.get("count")) or a_numero(sub.get("value"))
        if fecha is None or valor is None:
            sin_reconocer += 1
            continue
        acumulado[fecha] = acumulado.get(fecha, 0) + valor
    if sin_reconocer:
        log_json(f"  ⚠️  steps: {sin_reconocer}/{len(puntos)} sin reconocer. Ejemplo:", puntos[0])
    for fecha, total in acumulado.items():
        dias.setdefault(fecha, {})["pasos"] = round(total)


def procesar_distancia(session, dias):
    print("Pidiendo distance...")
    puntos = pedir_lista_interval(session, "distance")
    if not puntos:
        return
    acumulado_mm = {}
    sin_reconocer = 0
    for p in puntos:
        sub = p.get("distance") or {}
        civil = (sub.get("interval") or {}).get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        valor = a_numero(sub.get("millimeters"))
        if fecha is None or valor is None:
            sin_reconocer += 1
            continue
        acumulado_mm[fecha] = acumulado_mm.get(fecha, 0) + valor
    if sin_reconocer:
        log_json(f"  ⚠️  distance: {sin_reconocer}/{len(puntos)} sin reconocer. Ejemplo:", puntos[0])
    for fecha, total_mm in acumulado_mm.items():
        dias.setdefault(fecha, {})["distancia_km"] = round(total_mm / 1_000_000, 2)


def procesar_minutos_activos(session, dias):
    print("Pidiendo active-minutes...")
    puntos = pedir_lista_interval(session, "active-minutes")
    if not puntos:
        return
    acumulado = {}
    sin_reconocer = 0
    for p in puntos:
        sub = p.get("activeMinutes") or {}
        civil = (sub.get("interval") or {}).get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        niveles = sub.get("activeMinutesByActivityLevel") or []
        total_punto = 0
        encontrado = False
        for nivel in niveles:
            v = a_numero(nivel.get("activeMinutes"))
            if v is not None:
                total_punto += v
                encontrado = True
        if fecha is None or not encontrado:
            sin_reconocer += 1
            continue
        acumulado[fecha] = acumulado.get(fecha, 0) + total_punto
    if sin_reconocer:
        log_json(f"  ⚠️  active-minutes: {sin_reconocer}/{len(puntos)} sin reconocer. Ejemplo:", puntos[0])
    for fecha, total in acumulado.items():
        dias.setdefault(fecha, {})["minutos_activos"] = round(total)


def procesar_calorias(session, dias):
    print("Pidiendo total-calories (dailyRollUp)...")
    hoy = date.today()
    desde_d = hoy - timedelta(days=DIAS_ATRAS)
    hasta_d = hoy + timedelta(days=1)
    url = f"{API_BASE}/users/me/dataTypes/total-calories/dataPoints:dailyRollUp"
    body = {
        "range": {
            "civilStartTime": {"date": {"year": desde_d.year, "month": desde_d.month, "day": desde_d.day}},
            "civilEndTime": {"date": {"year": hasta_d.year, "month": hasta_d.month, "day": hasta_d.day}},
        }
    }
    try:
        resp = session.post(url, json=body, timeout=30)
        if not resp.ok:
            print(f"  ⚠️  total-calories: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
            return
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  total-calories: error de red — {e}")
        return

    puntos = data.get("dataPoints") or data.get("data_points") or []
    if not puntos:
        log_json("  ℹ️  total-calories: sin dataPoints, respuesta completa:", data)
        return

    sin_reconocer = 0
    for p in puntos:
        sub = p.get("totalCalories") or {}
        civil = (sub.get("interval") or {}).get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        valor = a_numero(sub.get("kcal")) or a_numero(sub.get("value"))
        if fecha is None or valor is None:
            sin_reconocer += 1
            continue
        dias.setdefault(fecha, {})["calorias_dia"] = round(valor, 2)
    if sin_reconocer:
        log_json(f"  ⚠️  total-calories: {sin_reconocer}/{len(puntos)} sin reconocer. Ejemplo:", puntos[0])


TIPOS_DAILY = [
    ("daily-resting-heart-rate", "dailyRestingHeartRate", ["beatsPerMinute", "value"], "fc_reposo"),
    ("daily-heart-rate-variability", "dailyHeartRateVariability", ["averageHeartRateVariabilityMilliseconds", "value"], "hrv"),
    ("daily-oxygen-saturation", "dailyOxygenSaturation", ["averagePercentage", "value"], "spo2"),
]


def procesar_daily(session, dias):
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()

    for tipo_endpoint, clave_anidada, claves_valor, campo_local in TIPOS_DAILY:
        print(f"Pidiendo {tipo_endpoint}...")
        url = f"{API_BASE}/users/me/dataTypes/{tipo_endpoint}/dataPoints"
        try:
            resp = session.get(url, params={"pageSize": 100}, timeout=30)
            if not resp.ok:
                print(f"  ⚠️  {tipo_endpoint}: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
                continue
            data = resp.json()
        except requests.RequestException as e:
            print(f"  ⚠️  {tipo_endpoint}: error de red — {e}")
            continue

        puntos = data.get("dataPoints") or data.get("data_points") or []
        if not puntos:
            log_json(f"  ℹ️  {tipo_endpoint}: sin dataPoints. Respuesta:", data)
            continue

        sin_reconocer = 0
        for p in puntos:
            sub = p.get(clave_anidada) or {}
            fecha = fecha_desde_civil_date(sub.get("date"))
            valor = None
            for clave in claves_valor:
                valor = a_numero(sub.get(clave))
                if valor is not None:
                    break
            if fecha is None or valor is None or fecha < desde or fecha >= hasta:
                sin_reconocer += 1
                continue
            dias.setdefault(fecha, {})[campo_local] = round(valor, 2)

        if sin_reconocer == len(puntos) and puntos:
            log_json(f"  ⚠️  {tipo_endpoint}: ningún punto reconocido de {len(puntos)}. Ejemplo:", puntos[0])


def procesar_sueno(session, dias):
    print("Pidiendo sleep (sin filtro, tomando lo más reciente)...")
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/users/me/dataTypes/sleep/dataPoints"
    try:
        resp = session.get(url, params={"pageSize": 25}, timeout=30)
        if not resp.ok:
            print(f"  ⚠️  sleep: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
            return
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  sleep: error de red — {e}")
        return

    puntos = data.get("dataPoints") or data.get("data_points") or []
    if not puntos:
        log_json("  ℹ️  sleep: sin dataPoints, respuesta completa:", data)
        return

    sin_reconocer = 0
    for p in puntos:
        sub = p.get("sleep") or {}
        interval = sub.get("interval") or {}
        fecha = fecha_civil_desde_utc(interval.get("startTime"), interval.get("startUtcOffset"))
        stages = sub.get("stages") or []

        if fecha is None or fecha < desde or fecha >= hasta or not stages:
            sin_reconocer += 1
            continue

        segundos = {"DEEP": 0, "LIGHT": 0, "REM": 0}
        for s in stages:
            tipo = s.get("type")
            if tipo not in segundos:
                continue
            ini, fin = parse_iso(s.get("startTime")), parse_iso(s.get("endTime"))
            if ini and fin:
                segundos[tipo] += (fin - ini).total_seconds()

        total_seg = segundos["DEEP"] + segundos["LIGHT"] + segundos["REM"]
        if total_seg <= 0:
            sin_reconocer += 1
            continue

        dia = dias.setdefault(fecha, {})
        dia["sueno_total_min"] = round(total_seg / 60)
        dia["sueno_profundo_min"] = round(segundos["DEEP"] / 60)
        dia["sueno_ligero_min"] = round(segundos["LIGHT"] / 60)
        dia["sueno_rem_min"] = round(segundos["REM"] / 60)

    if sin_reconocer == len(puntos) and puntos:
        log_json(f"  ⚠️  sleep: ningún punto reconocido de {len(puntos)}. Ejemplo:", puntos[0])


def main():
    print("=== Fitbit sync v4 — Google Health API v4 ===")
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

    procesar_steps(session, dias)
    procesar_distancia(session, dias)
    procesar_minutos_activos(session, dias)
    procesar_calorias(session, dias)
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

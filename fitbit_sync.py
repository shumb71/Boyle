#!/usr/bin/env python3
"""
fitbit_sync.py — Sincroniza datos de Fitbit Air (via Google Health API v4) y genera
fitbit_historial.json en el mismo formato que garmin_historial.json, para que la app
Entreno PRO lo descargue directamente desde GitHub Pages (fetchFitbitAuto en index.html).

v3 — corregido con datos REALES capturados del log de un Run que sí llegó a la API (v2
fallaba en el parseo, no en la conexión). Confirmado por la propia respuesta de Google:

  - steps / distance / active-minutes: el valor no está en la raíz del data point, está
    anidado bajo una clave con el nombre del tipo en camelCase, ej.:
      {"dataSource": {...}, "steps": {"interval": {...}}}
    (el campo final del valor dentro de ese objeto sigue sin confirmar al 100% — se prueban
    varias claves candidatas y se imprime el JSON COMPLETO, sin truncar, si no se reconoce).

  - daily-resting-heart-rate: SÍ funciona con `list`, pero el valor viene como STRING
    ("beatsPerMinute": "55") no como número, y la fecha viene en
    {"date": {"year":Y,"month":M,"day":D}} anidado — no como texto ISO.

  - total-calories: NO admite `list` (error 400 explícito de Google: "supported: rollup,
    dailyRollup"). Se cambia a POST .../dataPoints:dailyRollUp.

  - sleep: el filtro `sleep.interval.start_time` es rechazado por la API
    (INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER). Se quita el filtro y se pide la lista
    reciente sin filtrar, descartando en Python lo que quede fuera del rango de fechas.

  - daily-heart-rate-variability / daily-oxygen-saturation: devolvieron {} vacío — puede ser
    simplemente que Fitbit Air aún no ha calculado esas métricas derivadas (llevan más de un
    día de uso en muchos wearables). No se toca la lógica, solo se deja preparada para cuando
    haya datos.

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

# Límite de caracteres al imprimir JSON de depuración en el log. Antes estaba en 300-400 y
# eso ocultaba justo el campo que necesitábamos ver — ahora se imprime bastante más.
DEBUG_LEN = 2000


def log_json(prefijo, obj):
    print(f"{prefijo}")
    print(f"    {json.dumps(obj, ensure_ascii=False)[:DEBUG_LEN]}")


def fecha_desde_civil_date(date_obj):
    """Convierte {"year":Y,"month":M,"day":D} en 'YYYY-MM-DD'."""
    if not date_obj:
        return None
    y, m, d = date_obj.get("year"), date_obj.get("month"), date_obj.get("day")
    if not (y and m and d):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


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


# ── Tipos "Interval" (steps, distance, active-minutes): un valor por franja horaria ────────
# endpoint_id, clave_anidada_camelCase, claves candidatas del valor dentro de esa clave, campo local
TIPOS_INTERVAL = [
    ("steps", "steps", ["count", "value", "steps", "total"], "pasos"),
    ("distance", "distance", ["meters", "value", "distance", "total"], "_distancia_m"),
    ("active-minutes", "activeMinutes", ["minutes", "value", "activeMinutes", "total"], "minutos_activos"),
]


def procesar_intervalos(session, dias):
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()

    for tipo_endpoint, clave_anidada, claves_valor, campo_local in TIPOS_INTERVAL:
        print(f"Pidiendo {tipo_endpoint}...")
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
                continue
            data = resp.json()
        except requests.RequestException as e:
            print(f"  ⚠️  {tipo_endpoint}: error de red — {e}")
            continue

        puntos = data.get("dataPoints") or data.get("data_points") or []
        if not puntos:
            log_json(f"  ℹ️  {tipo_endpoint}: sin dataPoints, respuesta completa:", data)
            continue

        acumulado_por_dia = {}
        sin_reconocer = 0
        for p in puntos:
            sub = p.get(clave_anidada) or {}
            interval = sub.get("interval") or {}
            civil = interval.get("civilStartTime") or {}
            fecha = fecha_desde_civil_date(civil.get("date"))
            valor = None
            for clave in claves_valor:
                v = sub.get(clave)
                if isinstance(v, (int, float)):
                    valor = v
                    break
                if isinstance(v, str):
                    try:
                        valor = float(v)
                        break
                    except ValueError:
                        pass
            if fecha is None or valor is None:
                sin_reconocer += 1
                continue
            acumulado_por_dia[fecha] = acumulado_por_dia.get(fecha, 0) + valor

        if sin_reconocer:
            log_json(f"  ⚠️  {tipo_endpoint}: {sin_reconocer}/{len(puntos)} puntos sin reconocer. Ejemplo completo:", puntos[0])

        for fecha, total in acumulado_por_dia.items():
            if fecha not in dias:
                dias[fecha] = {}
            dias[fecha][campo_local] = round(total, 2)

    for fecha, dia in dias.items():
        if "_distancia_m" in dia:
            dia["distancia_km"] = round(dia.pop("_distancia_m") / 1000, 2)


# ── total-calories: solo admite rollup/dailyRollup (POST), no list ─────────────────────────
def procesar_calorias(session, dias):
    print("Pidiendo total-calories (dailyRollUp)...")
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/users/me/dataTypes/total-calories/dataPoints:dailyRollUp"
    body = {
        "range": {"startTime": f"{desde}T00:00:00Z", "endTime": f"{hasta}T00:00:00Z"},
        "windowSizeDays": 1,
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
        sub = p.get("totalCalories") or p.get("total_calories") or {}
        interval = sub.get("interval") or {}
        civil = interval.get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        valor = None
        for clave in ("kcal", "value", "calories"):
            v = sub.get(clave)
            if isinstance(v, (int, float)):
                valor = v
                break
        if fecha is None or valor is None:
            sin_reconocer += 1
            continue
        if fecha not in dias:
            dias[fecha] = {}
        dias[fecha]["calorias_dia"] = round(valor, 2)

    if sin_reconocer:
        log_json(f"  ⚠️  total-calories: {sin_reconocer}/{len(puntos)} puntos sin reconocer. Ejemplo completo:", puntos[0])


# ── Tipos "Daily" (ya vienen un registro por día) ───────────────────────────────────────────
# endpoint_id, clave_anidada_camelCase, claves candidatas del valor, campo local
TIPOS_DAILY = [
    ("daily-resting-heart-rate", "dailyRestingHeartRate", ["beatsPerMinute", "value"], "fc_reposo"),
    ("daily-heart-rate-variability", "dailyHeartRateVariability", ["milliseconds", "value", "hrv"], "hrv"),
    ("daily-oxygen-saturation", "dailyOxygenSaturation", ["percentage", "value", "spo2"], "spo2"),
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
            log_json(f"  ℹ️  {tipo_endpoint}: sin dataPoints (puede que aún no haya datos calculados). Respuesta:", data)
            continue

        sin_reconocer = 0
        for p in puntos:
            sub = p.get(clave_anidada) or {}
            fecha = fecha_desde_civil_date(sub.get("date"))
            valor = None
            for clave in claves_valor:
                v = sub.get(clave)
                if isinstance(v, (int, float)):
                    valor = v
                    break
                if isinstance(v, str):
                    try:
                        valor = float(v)
                        break
                    except ValueError:
                        pass
            if fecha is None or valor is None or fecha < desde or fecha >= hasta:
                sin_reconocer += 1
                continue
            if fecha not in dias:
                dias[fecha] = {}
            dias[fecha][campo_local] = round(valor, 2) if isinstance(valor, float) else valor

        if sin_reconocer == len(puntos) and puntos:
            log_json(f"  ⚠️  {tipo_endpoint}: ningún punto reconocido de {len(puntos)}. Ejemplo completo:", puntos[0])


# ── sleep: sin filtro (el nombre de campo de filtro no es válido según la API) ──────────────
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
        civil = interval.get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        summary = sub.get("summary") or {}
        total_seg = summary.get("totalSleepDuration") or summary.get("totalDuration")

        if fecha is None or fecha < desde or fecha >= hasta or total_seg is None:
            sin_reconocer += 1
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

    if sin_reconocer == len(puntos) and puntos:
        log_json(f"  ⚠️  sleep: ningún punto reconocido de {len(puntos)}. Ejemplo completo:", puntos[0])


def main():
    print("=== Fitbit sync v3 — Google Health API v4 ===")
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

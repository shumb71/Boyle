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

v5 — Añadido procesar_ejercicios(): trae CUALQUIER sesión "exercise" (no solo boxeo), como
  lista por día en 'actividadesFitbit'. Los nombres de campo de calorías/FC de "exercise" no
  están confirmados con datos reales todavía (a diferencia del resto del script) — revisar el
  log "🔍 exercise: ejemplo de punto crudo" en la primera ejecución con datos reales y ajustar
  los candidatos en procesar_ejercicios() si hiciera falta.

v6 — Corregido el filtro de exercise: devolvía siempre HTTP 400 (INVALID_DATA_POINT_FILTER)
  porque usaba interval.end_time, que la API NO soporta para este tipo de dato (confirmado
  contra la doc oficial). "exercise" (salvo Sleep y ECG) solo admite filtrar por
  interval.civil_start_time, con fecha civil (sin "Z"). Con esto ya llegan sesiones reales.
  Confirmado con datos reales: el resumen de calorías/FC vive en "metricsSummary"
  (caloriesKcal, averageHeartRateBeatsPerMinute), no en "exerciseSummary"/"summary".

v7 — Añadido procesar_peso(): peso corporal desde la báscula Renpho, vía Google Health
  (tipo de dato "weight"). "weight" es Sample (medición puntual), se filtra por
  weight.sample_time.physical_time — NO por interval como los tipos Interval/Session. El
  nombre exacto del campo del valor de peso NO está confirmado todavía con datos reales;
  se prueban varios candidatos y se loguea el punto crudo del primer dato recibido, mismo
  patrón que se usó para exercise en v5/v6. Revisar ese log tras la primera ejecución real
  y ajustar procesar_peso() si el candidato acertado no es el primero de la lista.

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
    # CivilTimeInterval real: {"start": CivilDateTime, "end": CivilDateTime}, y CivilDateTime
    # es {"date": {"year","month","day"}} — confirmado en la referencia RPC oficial.
    body = {
        "range": {
            "start": {"date": {"year": desde_d.year, "month": desde_d.month, "day": desde_d.day}},
            "end": {"date": {"year": hasta_d.year, "month": hasta_d.month, "day": hasta_d.day}},
        },
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

    # La respuesta de dailyRollUp usa "rollupDataPoints", no "dataPoints" (distinto del list).
    puntos = data.get("rollupDataPoints") or data.get("rollup_data_points") or data.get("dataPoints") or []
    if not puntos:
        log_json("  ℹ️  total-calories: sin rollupDataPoints, respuesta completa:", data)
        return

    sin_reconocer = 0
    for p in puntos:
        # civilStartTime va directo en el punto (no anidado bajo "interval" como en list)
        civil = p.get("civilStartTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        sub = p.get("totalCalories") or {}
        valor = a_numero(sub.get("kcalSum")) or a_numero(sub.get("kcal")) or a_numero(sub.get("value"))
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
        # pageSize subido de 25 a 200: sin filtro de fecha disponible en este endpoint, no hay
        # garantía de que la API devuelva los puntos más recientes primero — con pocos días de
        # historial no se notaba, pero al acumular más sesiones podía dejar fuera las de hoy.
        resp = session.get(url, params={"pageSize": 200}, timeout=30)
        if not resp.ok:
            print(f"  ⚠️  sleep: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
            return
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  sleep: error de red — {e}")
        return

    puntos = data.get("dataPoints") or data.get("data_points") or []
    print(f"  ℹ️  sleep: {len(puntos)} puntos recibidos en total.")
    if not puntos:
        log_json("  ℹ️  sleep: sin dataPoints, respuesta completa:", data)
        return

    # Limpiar campos de sueño obsoletos dentro del rango, SOLO ahora que sabemos que la petición
    # trajo puntos con los que repoblar (si hubiera fallado antes, ya habríamos salido con
    # `return` sin tocar nada). Necesario porque este endpoint no admite filtro de fecha fiable
    # y en una versión anterior del script la fecha se calculaba desde el inicio del sueño en
    # vez del despertar — eso dejó restos mal fechados en el JSON (sueño de una noche archivado
    # un día antes de lo correcto). Al limpiar y repoblar cada vez, esos restos se autocorrigen
    # solos en la siguiente ejecución sin tener que editar el JSON a mano.
    CAMPOS_SUENO = ("sueno_total_min", "sueno_profundo_min", "sueno_ligero_min", "sueno_rem_min")
    for fecha_existente, dia_existente in dias.items():
        if desde <= fecha_existente < hasta:
            for campo in CAMPOS_SUENO:
                dia_existente.pop(campo, None)

    sin_reconocer = 0
    fechas_vistas = []
    for p in puntos:
        sub = p.get("sleep") or {}
        interval = sub.get("interval") or {}
        # Fecha del sueño = día en que te DESPIERTAS (endTime), no en que te duermes (startTime).
        # Es la convención habitual (Garmin, Fitbit, etc): el sueño de la noche del 21 al 22
        # se etiqueta como "sueño del 22", que es el día al que realmente pertenece para el
        # usuario. Con startTime se etiquetaba mal bajo el día anterior.
        fecha = fecha_civil_desde_utc(interval.get("endTime"), interval.get("endUtcOffset"))
        if fecha:
            fechas_vistas.append(fecha)
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

    if fechas_vistas:
        print(f"  ℹ️  sleep: fechas recibidas desde {min(fechas_vistas)} hasta {max(fechas_vistas)}.")
    if sin_reconocer == len(puntos) and puntos:
        log_json(f"  ⚠️  sleep: ningún punto reconocido de {len(puntos)}. Ejemplo:", puntos[0])


def procesar_ejercicios(session, dias):
    """Sesiones de ejercicio (CUALQUIER tipo: running, walking, boxing, kickboxing...)
    registradas manual o automáticamente en Google Health. Se guardan como una LISTA por
    día en dias[fecha]['actividadesFitbit'] — a diferencia del resto de campos (que son un
    único valor por día), separado a propósito de 'actividades'/'fuerza' que sigue
    aportando Garmin en exclusiva (ver FITBIT_OWNED_FIELDS en index.html).

    NOTA: a diferencia del resto de este script, los nombres de los campos de resumen
    (calorías, FC media/máxima) de "exercise" NO están confirmados todavía con datos reales
    — se prueban varios candidatos habituales y, si ninguno encaja, la sesión se guarda
    igualmente con lo que sí se puede leer con seguridad (tipo, inicio, fin, duración), y se
    loguea el JSON crudo del primer punto para poder afinar los nombres en una próxima
    versión, siguiendo el mismo patrón iterativo que ya se usó para total-calories y sleep.
    """
    print("Pidiendo exercise...")
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/users/me/dataTypes/exercise/dataPoints"
    # "exercise" (salvo Sleep y ECG) SOLO admite filtrar por civil_start_time — la doc
    # oficial de la API rechaza interval.end_time e interval.start_time (físico) para este
    # tipo de dato con INVALID_DATA_POINT_FILTER. Formato: fecha civil YYYY-MM-DD, sin "Z".
    filtro = (
        f'exercise.interval.civil_start_time >= "{desde}" AND '
        f'exercise.interval.civil_start_time < "{hasta}"'
    )

    puntos = []
    page_token = None
    for _ in range(10):  # tope de seguridad: 10 páginas x 25 (máximo de este tipo) = 250 sesiones
        params = {"filter": filtro, "pageSize": 25}
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = session.get(url, params=params, timeout=30)
            if not resp.ok:
                print(f"  ⚠️  exercise: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
                break
            data = resp.json()
        except requests.RequestException as e:
            print(f"  ⚠️  exercise: error de red — {e}")
            break
        pagina = data.get("dataPoints") or data.get("data_points") or []
        puntos.extend(pagina)
        page_token = data.get("nextPageToken") or data.get("next_page_token")
        if not page_token or not pagina:
            break

    print(f"  ℹ️  exercise: {len(puntos)} sesiones recibidas en total.")
    if not puntos:
        return

    # Log del primer punto SIEMPRE (no solo si algo falla) — es territorio no confirmado
    # todavía, interesa ver la forma real del dato la primera vez que haya sesiones de verdad.
    log_json("  🔍  exercise: ejemplo de punto crudo recibido:", puntos[0])

    # Limpiar sesiones dentro del rango antes de repoblar (mismo patrón que sleep), para no
    # ir acumulando duplicados en cada ejecución del cron cada 3h.
    for fecha_existente, dia_existente in dias.items():
        if desde <= fecha_existente < hasta:
            dia_existente["actividadesFitbit"] = []

    sin_reconocer = 0
    for p in puntos:
        sub = p.get("exercise") or {}
        interval = sub.get("interval") or {}
        fecha = fecha_civil_desde_utc(interval.get("endTime"), interval.get("endUtcOffset"))
        if fecha is None or fecha < desde or fecha >= hasta:
            sin_reconocer += 1
            continue

        inicio_dt = parse_iso(interval.get("startTime"))
        fin_dt = parse_iso(interval.get("endTime"))
        duracion_min = None
        if inicio_dt and fin_dt:
            duracion_min = round((fin_dt - inicio_dt).total_seconds() / 60)

        tipo = sub.get("exerciseType") or sub.get("activityType") or sub.get("type") or "DESCONOCIDO"
        nombre = sub.get("displayName") or None

        # CONFIRMADO con datos reales (log del 27/08): el resumen de calorías/FC vive en
        # "metricsSummary", no en "exerciseSummary"/"summary" (los candidatos de v5 estaban
        # equivocados). Campos reales: caloriesKcal (número) y averageHeartRateBeatsPerMinute
        # (string). FC máxima NO apareció en el ejemplo recibido — se deja como candidato sin
        # confirmar por si algún tipo de ejercicio más intenso sí la trae.
        metrics = sub.get("metricsSummary") or {}
        calorias = a_numero(metrics.get("caloriesKcal"))
        fc_media = a_numero(metrics.get("averageHeartRateBeatsPerMinute"))
        fc_max = a_numero(metrics.get("maxHeartRateBeatsPerMinute") or metrics.get("peakHeartRateBeatsPerMinute"))

        punto_id = (p.get("name") or "").rstrip("/").split("/")[-1] or None

        sesion = {
            "id": punto_id,
            "tipo": tipo,
            "nombre": nombre,
            "inicio": interval.get("startTime"),
            "fin": interval.get("endTime"),
            "duracion_min": duracion_min,
            "calorias": round(calorias) if calorias is not None else None,
            "fc_media": round(fc_media) if fc_media is not None else None,
            "fc_max": round(fc_max) if fc_max is not None else None,
        }
        dias.setdefault(fecha, {}).setdefault("actividadesFitbit", []).append(sesion)

    if sin_reconocer:
        print(f"  ⚠️  exercise: {sin_reconocer}/{len(puntos)} fuera de rango o sin fecha reconocible.")


def procesar_peso(session, dias):
    """Peso corporal (báscula Renpho vía Google Health). "weight" es un tipo Sample
    (medición puntual, no un intervalo ni una sesión) — se filtra por
    weight.sample_time.physical_time, igual que body-fat en la documentación oficial.

    IMPORTANTE: el nombre exacto del campo con el valor del peso NO está confirmado
    todavía con datos reales (a diferencia del resto de este script). Se prueban varios
    candidatos razonables y, además, SIEMPRE se loguea el punto crudo completo del primer
    dato recibido — hay que revisar ese log tras la primera ejecución con datos reales y
    ajustar la extracción si el candidato acertado no es el primero de la lista.
    """
    print("Pidiendo weight...")
    hoy = date.today()
    desde = (hoy - timedelta(days=DIAS_ATRAS)).isoformat()
    hasta = (hoy + timedelta(days=1)).isoformat()
    url = f"{API_BASE}/users/me/dataTypes/weight/dataPoints"
    filtro = (
        f'weight.sample_time.physical_time >= "{desde}T00:00:00Z" AND '
        f'weight.sample_time.physical_time < "{hasta}T00:00:00Z"'
    )
    try:
        resp = session.get(url, params={"filter": filtro, "pageSize": 1000}, timeout=30)
        if not resp.ok:
            print(f"  ⚠️  weight: HTTP {resp.status_code} — {resp.text[:DEBUG_LEN]}")
            return
        data = resp.json()
    except requests.RequestException as e:
        print(f"  ⚠️  weight: error de red — {e}")
        return

    puntos = data.get("dataPoints") or data.get("data_points") or []
    if not puntos:
        log_json("  ℹ️  weight: sin dataPoints, respuesta completa:", data)
        return

    print(f"  ℹ️  weight: {len(puntos)} puntos recibidos en total.")
    log_json("  🔍  weight: ejemplo de punto crudo recibido:", puntos[0])

    sin_reconocer = 0
    for p in puntos:
        sub = p.get("weight") or {}
        sample_time = sub.get("sampleTime") or {}
        civil = sample_time.get("civilTime") or {}
        fecha = fecha_desde_civil_date(civil.get("date"))
        if fecha is None:
            fecha = fecha_civil_desde_utc(sample_time.get("physicalTime"), sample_time.get("utcOffset"))

        # Candidatos sin confirmar — probar varios nombres/formas posibles del valor.
        peso_kg = None
        peso_obj = sub.get("weight")
        if isinstance(peso_obj, dict):
            if peso_obj.get("kilograms") is not None:
                peso_kg = a_numero(peso_obj.get("kilograms"))
            elif peso_obj.get("grams") is not None:
                g = a_numero(peso_obj.get("grams"))
                peso_kg = g / 1000 if g is not None else None
        if peso_kg is None:
            peso_kg = a_numero(sub.get("kilograms"))
        if peso_kg is None and sub.get("grams") is not None:
            g = a_numero(sub.get("grams"))
            peso_kg = g / 1000 if g is not None else None
        if peso_kg is None:
            peso_kg = a_numero(sub.get("mass"))

        if fecha is None or peso_kg is None or fecha < desde or fecha >= hasta:
            sin_reconocer += 1
            continue
        dias.setdefault(fecha, {})["peso_kg"] = round(peso_kg, 1)

    if sin_reconocer:
        print(f"  ⚠️  weight: {sin_reconocer}/{len(puntos)} sin reconocer (revisa el log del punto crudo de arriba).")


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
    procesar_ejercicios(session, dias)
    procesar_peso(session, dias)

    salida = {
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "dias": dias,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)

    print(f"✅ Escrito {OUTPUT_FILE} con {len(dias)} días en total.")


if __name__ == "__main__":
    main()

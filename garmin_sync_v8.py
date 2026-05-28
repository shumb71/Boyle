import json
import os
import time
from datetime import date, timedelta

EMAIL    = os.environ.get("GARMIN_EMAIL", "")
PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

FICHERO_SALIDA = "garmin_historial.json"
DIAS_ATRAS     = 30

def login():
    from garminconnect import Garmin
    client = Garmin(email=EMAIL, password=PASSWORD)
    print("🔑 Haciendo login en Garmin...")
    client.login()
    print("✅ Login OK")
    return client

def cargar_historial_existente():
    if os.path.exists(FICHERO_SALIDA):
        try:
            with open(FICHERO_SALIDA, "r", encoding="utf-8") as f:
                data = json.load(f)
                dias = data.get("dias", {})
                print(f"📂 Historial existente: {len(dias)} día(s)")
                return dias
        except Exception:
            pass
    return {}

def fetch_dia(client, fecha_str, reintentos=2):
    d = {"fecha": fecha_str}

    for intento in range(reintentos + 1):
        try:
            stats = client.get_stats(fecha_str)
            d["pasos"]            = stats.get("totalSteps", 0)
            d["calorias_dia"]     = stats.get("totalKilocalories", 0)
            d["calorias_activas"] = stats.get("activeKilocalories", 0)
            d["distancia_km"]     = round((stats.get("totalDistanceMeters") or 0) / 1000, 2)
            d["minutos_activos"]  = stats.get("moderateIntensityMinutes", 0)
            d["fc_reposo"]        = stats.get("restingHeartRate")
            d["estres_medio"]     = stats.get("averageStressLevel")
            print(f"  Pasos: {d['pasos']}")
            break
        except Exception as e:
            print(f"  ⚠️ Stats intento {intento+1}: {e}")
            if intento < reintentos:
                time.sleep(3)

    try:
        sueno = client.get_sleep_data(fecha_str)
        sd = sueno.get("dailySleepDTO", {})
        d["sueno_total_min"]    = sd.get("sleepTimeSeconds", 0) // 60
        d["sueno_profundo_min"] = sd.get("deepSleepSeconds", 0) // 60
        d["sueno_ligero_min"]   = sd.get("lightSleepSeconds", 0) // 60
        d["sueno_rem_min"]      = sd.get("remSleepSeconds", 0) // 60
        d["sueno_puntuacion"]   = sd.get("sleepScores", {}).get("overall", {}).get("value", 0)
        print(f"  Sueño: {d['sueno_total_min']} min")
    except Exception as e:
        print(f"  ⚠️ Sueño: {e}")

    try:
        bb = client.get_body_battery(fecha_str)
        if bb:
            vals = [x.get("value", 0) for x in bb if x.get("value") is not None]
            if vals:
                d["body_battery_max"] = max(vals)
                d["body_battery_min"] = min(vals)
        print(f"  Body Battery OK")
    except Exception as e:
        print(f"  ⚠️ Body Battery: {e}")

    try:
        acts = client.get_activities_by_date(fecha_str, fecha_str)
        if acts:
            d["actividades"] = []
            for a in acts:
                act = {
                    "nombre":       a.get("activityName", ""),
                    "tipo":         a.get("activityType", {}).get("typeKey", ""),
                    "duracion_min": round((a.get("duration") or 0) / 60),
                    "calorias":     a.get("calories", 0),
                    "fc_media":     a.get("averageHR"),
                    "fc_max":       a.get("maxHR"),
                }
                d["actividades"].append(act)
                tipo = act["tipo"].lower()
                if "strength" in tipo or "fuerza" in tipo or "weight" in tipo:
                    d["fuerza"] = act
            print(f"  Actividades: {len(acts)}")
    except Exception as e:
        print(f"  ⚠️ Actividades: {e}")

    return d

def main():
    if not EMAIL or not PASSWORD:
        print("❌ Faltan GARMIN_EMAIL o GARMIN_PASSWORD")
        exit(1)

    dias_existentes = cargar_historial_existente()
    hoy = date.today()
    fechas_a_descargar = []

    for i in range(0, DIAS_ATRAS + 1):
        fecha = (hoy - timedelta(days=i)).isoformat()
        if i <= 3:
            # Últimos 3 días siempre se re-descargan (datos pueden llegar tarde)
            fechas_a_descargar.append(fecha)
            print(f"🔄 {fecha} — últimos 3 días, siempre se actualiza")
        elif fecha not in dias_existentes:
            fechas_a_descargar.append(fecha)
            print(f"📥 {fecha} — falta, se descarga")
        else:
            print(f"⏭️  {fecha} — ya existe, saltando")

    print(f"\n📥 Descargando {len(fechas_a_descargar)} día(s)...\n")

    # Login con reintentos
    client = None
    for intento in range(3):
        try:
            client = login()
            break
        except Exception as e:
            print(f"⚠️ Login intento {intento+1} fallido: {e}")
            if intento < 2:
                time.sleep(5)

    if not client:
        print("❌ No se pudo hacer login después de 3 intentos")
        exit(1)

    for fecha in sorted(fechas_a_descargar):
        print(f"\n📅 {fecha}")
        try:
            datos = fetch_dia(client, fecha)
            dias_existentes[fecha] = datos
        except Exception as e:
            print(f"  ❌ Error: {e}")
        time.sleep(1)  # Pausa entre peticiones para no saturar la API

    with open(FICHERO_SALIDA, "w", encoding="utf-8") as f:
        json.dump({
            "dias": dias_existentes,
            "actualizado": hoy.isoformat()
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Historial guardado: {len(dias_existentes)} días totales")

if __name__ == "__main__":
    main()

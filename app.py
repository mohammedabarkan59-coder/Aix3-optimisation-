
import os
import json
import math
import hmac
import re
from datetime import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
load_dotenv(BASE_DIR / ".env")


# =========================
# Helpers secrets / env
# =========================

def get_secret(name, default=""):
    """Lit d'abord st.secrets, sinon variables d'environnement/.env."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return str(os.getenv(name, default))


def get_nested_secret(section, key, default=""):
    try:
        if section in st.secrets and key in st.secrets[section]:
            return str(st.secrets[section][key])
    except Exception:
        pass
    env_key = f"{section}_{key}".upper()
    return str(os.getenv(env_key, default))


def parse_centers(value):
    return [x.strip().upper() for x in str(value).split(",") if x.strip()]


def get_accounts():
    """
    Secrets recommandés :
    [accounts]
    mohammed = "motdepasse|AIX3,LIL3"
    lil3 = "motdepasse|LIL3"
    admin = "motdepasse|AIX3,LIL3"
    """
    accounts = {}
    try:
        if "accounts" in st.secrets:
            for user, raw in st.secrets["accounts"].items():
                raw = str(raw)
                if "|" in raw:
                    pwd, centers = raw.split("|", 1)
                    accounts[user] = {"password": pwd, "centers": parse_centers(centers)}
    except Exception:
        pass

    # Fallback local seulement pour tests.
    if not accounts:
        accounts = {
            "mohammed": {"password": get_secret("APP_PASSWORD_MOHAMMED", "change-moi"), "centers": ["AIX3", "LIL3"]},
            "lil3": {"password": get_secret("APP_PASSWORD_LIL3", "change-moi"), "centers": ["LIL3"]},
            "admin": {"password": get_secret("APP_PASSWORD_ADMIN", "change-moi"), "centers": ["AIX3", "LIL3"]},
        }
    return accounts


CENTERS = {
    "AIX3": {
        "label": "AIX3",
        "sorting_center_id": get_secret("AIX3_SORTING_CENTER_ID", get_secret("VINTED_SORTING_CENTER_ID", "288")),
        "depot_addr": get_secret("AIX3_DEPOT_ADDR", "Vinted Go, Bâtiment A, 13310 Saint-Martin-de-Crau"),
        "depot_lat": float(get_secret("AIX3_DEPOT_LAT", "43.6313869")),
        "depot_lon": float(get_secret("AIX3_DEPOT_LON", "4.7799518")),
        "default_routes": int(get_secret("AIX3_DEFAULT_ROUTES", "6")),
    },
    "LIL3": {
        "label": "LIL3",
        "sorting_center_id": get_secret("LIL3_SORTING_CENTER_ID", "265"),
        "depot_addr": get_secret("LIL3_DEPOT_ADDR", "15 Avenue de l'Europe, 59223 Roncq"),
        # Coordonnées par défaut approximatives sur Roncq. Ajustables dans les secrets.
        "depot_lat": float(get_secret("LIL3_DEPOT_LAT", "50.746301")),
        "depot_lon": float(get_secret("LIL3_DEPOT_LON", "3.115632")),
        "default_routes": int(get_secret("LIL3_DEFAULT_ROUTES", "14")),
    },
}

TOKEN = get_secret("VINTED_TOKEN", "")

AVG_SPEED = 50
ROAD_FACTOR = 1.15
SAFETY = 10
MAX_MIN = 420


# =========================
# Auth
# =========================

def login_screen():
    st.title("Connexion — Optimisation VintedGo")
    st.caption("Entre ton identifiant pour accéder à l'application.")
    with st.form("login_form"):
        user = st.text_input("Utilisateur")
        password = st.text_input("Mot de passe", type="password")
        ok = st.form_submit_button("Se connecter", type="primary")

    if ok:
        accounts = get_accounts()
        if user in accounts and hmac.compare_digest(password, accounts[user]["password"]):
            st.session_state.authenticated = True
            st.session_state.username = user
            st.session_state.allowed_centers = accounts[user]["centers"]
            st.rerun()
        else:
            st.error("Identifiant ou mot de passe incorrect.")


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    login_screen()
    st.stop()


# =========================
# API VintedGo
# =========================

def headers(token):
    return {
        "accept": "*/*",
        "accept-language": "en",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": "https://admin.vintedgo.com",
        "referer": "https://admin.vintedgo.com/",
        "user-agent": "Mozilla/5.0",
    }


def detect_latest_batch(token, sorting_center_id, center_code):
    url = f"https://carrier.vintedgo.com/drivers/point_visits_batches?limit=10&sorting_center_id={sorting_center_id}"
    r = requests.get(url, headers=headers(token), timeout=40)
    if r.status_code == 401:
        raise RuntimeError("Token VintedGo expiré ou incorrect. Mets un token frais dans les secrets.")
    r.raise_for_status()
    data = r.json()

    with open(DATA_DIR / f"debug_batches_{center_code}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    if isinstance(data, dict):
        batches = data.get("data") or data.get("point_visits_batches") or data.get("results") or data.get("items") or []
    elif isinstance(data, list):
        batches = data
    else:
        batches = []

    if not batches:
        raise RuntimeError(f"Aucun batch détecté pour {center_code}.")

    rows = []
    for b in batches:
        rows.append({
            "id": b.get("id"),
            "status": b.get("status"),
            "scheduled_for": b.get("scheduled_for"),
            "published_at": b.get("published_at"),
            "last_published_at": b.get("last_published_at"),
            "sorting_center_code": b.get("sorting_center_code"),
            "sorting_center_id": b.get("sorting_center_id"),
            "point_visits_count": b.get("point_visits_count") or b.get("visits_count") or b.get("point_count"),
        })

    df = pd.DataFrame(rows).dropna(subset=["id"]).copy()
    if df.empty:
        raise RuntimeError("Réponse reçue, mais aucun ID de batch exploitable.")

    df["_date"] = pd.to_datetime(df["scheduled_for"], errors="coerce")
    df["_id"] = pd.to_numeric(df["id"], errors="coerce")
    df = df.sort_values(["_date", "_id"], ascending=[False, False])
    latest_id = str(int(df.iloc[0]["id"]))

    clean = df.drop(columns=["_date", "_id"], errors="ignore")
    clean.to_csv(DATA_DIR / f"batches_detected_{center_code}.csv", index=False, encoding="utf-8-sig")
    clean.to_excel(DATA_DIR / f"batches_detected_{center_code}.xlsx", index=False)
    return latest_id, clean


def fetch_points(token, batch_id, center_code):
    url = f"https://carrier.vintedgo.com/drivers/point_visits_batches/{batch_id}/route_editor_data"
    r = requests.get(url, headers=headers(token), timeout=40)
    if r.status_code == 401:
        raise RuntimeError("Token VintedGo expiré ou incorrect. Mets un token frais dans les secrets.")
    r.raise_for_status()
    data = r.json()

    with open(DATA_DIR / f"debug_vinted_response_{center_code}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    batch = data.get("point_visits_batch", {}) or {}
    visits = []

    for route in data.get("driver_routes", []) or []:
        for v in route.get("point_visits", []) or []:
            v["route_name_vinted"] = route.get("name")
            visits.append(v)

    for v in data.get("unassigned_point_visits", []) or []:
        v["route_name_vinted"] = "Non assigné"
        visits.append(v)

    rows = []
    for v in visits:
        p = v.get("point", {}) or {}
        point_type = p.get("point_type")

        if point_type == "locker":
            typ = "Casier / Locker"
            service = 15
        else:
            typ = "Point relais"
            service = 5

        lat = p.get("latitude")
        lon = p.get("longitude")

        rows.append({
            "center_code": center_code,
            "batch_id": batch.get("id"),
            "batch_status": batch.get("status"),
            "scheduled_for": batch.get("scheduled_for"),
            "sorting_center_code": batch.get("sorting_center_code") or center_code,
            "route_name_vinted": v.get("route_name_vinted"),
            "visit_id": v.get("id"),
            "sequence_vinted": v.get("sequence"),
            "point_id": p.get("id"),
            "code": p.get("code"),
            "type": typ,
            "point_type": point_type,
            "service": service,
            "nom": p.get("name"),
            "adresse": p.get("address"),
            "ville": p.get("city"),
            "code_postal": p.get("postal_code"),
            "pays": p.get("country_code"),
            "lat": lat,
            "lon": lon,
            "maps": None,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    df["maps"] = df.apply(lambda r: f"https://www.google.com/maps/search/?api=1&query={r['lat']:.6f},{r['lon']:.6f}", axis=1)

    df.to_csv(DATA_DIR / f"points_vinted_latest_{center_code}.csv", index=False, encoding="utf-8-sig")
    df.to_excel(DATA_DIR / f"points_vinted_latest_{center_code}.xlsx", index=False)
    return df



# =========================
# Horaires d'ouverture points
# =========================

DAY_ALIASES = {
    0: ["monday", "mon", "lundi"],
    1: ["tuesday", "tue", "mardi"],
    2: ["wednesday", "wed", "mercredi"],
    3: ["thursday", "thu", "jeudi"],
    4: ["friday", "fri", "vendredi"],
    5: ["saturday", "sat", "samedi"],
    6: ["sunday", "sun", "dimanche"],
}


def time_to_min(value, default=None):
    try:
        if value is None or str(value).strip() == "":
            return default
        h, m = str(value).strip()[:5].split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


def min_to_time(value):
    if value is None or pd.isna(value):
        return ""
    value = int(round(float(value)))
    value = max(0, min(value, 24 * 60 - 1))
    return f"{value // 60:02d}:{value % 60:02d}"


def intervals_have_lunch_break(intervals):
    """Détecte une fermeture qui coupe la plage 12h-14h."""
    if not intervals:
        return False

    mins = []
    for it in intervals:
        o = time_to_min(it.get("opens_at"))
        c = time_to_min(it.get("closes_at"))
        if o is not None and c is not None:
            mins.append((o, c))

    mins = sorted(mins)
    if len(mins) >= 2:
        for (_, close_1), (open_2, _) in zip(mins, mins[1:]):
            if close_1 <= 14 * 60 and open_2 >= 12 * 60 and open_2 > close_1:
                return True

    # Cas extrême : pas ouvert du tout pendant 12-14.
    open_at_lunch = any(o <= 12 * 60 and c >= 14 * 60 for o, c in mins)
    return not open_at_lunch and bool(mins)


def parse_hours_from_rsc_text(text):
    """
    Extrait les horaires depuis la réponse RSC Next.js.
    On cherche les champs opens_at / closes_at observés dans le cURL.
    """
    if not text:
        return []

    # Exemple observé : "opens_at":"07:30","closes_at":"20:00"
    # On capture éventuellement day/name autour.
    entries = []

    # Pattern avec un day avant opens/closes
    pattern_day = re.compile(
        r'"(?:day|weekday|day_name|name)"\s*:\s*"([^"]+)".{0,250}?"opens_at"\s*:\s*"([0-2]\d:[0-5]\d)".{0,80}?"closes_at"\s*:\s*"([0-2]\d:[0-5]\d)"',
        re.IGNORECASE | re.DOTALL,
    )
    for day, open_at, close_at in pattern_day.findall(text):
        entries.append({"day": day.lower(), "opens_at": open_at, "closes_at": close_at})

    if entries:
        return entries

    # Fallback sans jour : on récupère toutes les paires horaires.
    pattern_simple = re.compile(
        r'"opens_at"\s*:\s*"([0-2]\d:[0-5]\d)".{0,80}?"closes_at"\s*:\s*"([0-2]\d:[0-5]\d)"',
        re.IGNORECASE | re.DOTALL,
    )
    for open_at, close_at in pattern_simple.findall(text):
        entries.append({"day": "unknown", "opens_at": open_at, "closes_at": close_at})

    # Déduplication
    seen = set()
    out = []
    for e in entries:
        key = (e["day"], e["opens_at"], e["closes_at"])
        if key not in seen:
            out.append(e)
            seen.add(key)
    return out


def fetch_working_hours_for_point(point_id, admin_cookie):
    """
    Appelle la page admin du point et extrait les horaires.
    Nécessite VINTED_ADMIN_COOKIE dans les secrets Streamlit.
    """
    if not admin_cookie:
        return []

    url = f"https://admin.vintedgo.com/fr/points/{int(point_id)}?tab=working_hours&_rsc=1a6v3"
    h = {
        "accept": "*/*",
        "accept-language": "fr,fr-FR;q=0.9,en;q=0.8",
        "cookie": admin_cookie,
        "referer": f"https://admin.vintedgo.com/fr/points/{int(point_id)}?tab=working_hours",
        "rsc": "1",
        "user-agent": "Mozilla/5.0",
    }

    r = requests.get(url, headers=h, timeout=25)
    if r.status_code in (401, 403):
        raise RuntimeError("Cookie admin VintedGo expiré ou non autorisé. Remplace VINTED_ADMIN_COOKIE dans les secrets.")
    r.raise_for_status()
    return parse_hours_from_rsc_text(r.text)


def choose_intervals_for_day(hours_json, scheduled_for):
    """
    Retourne les intervalles du jour de la tournée.
    Si les jours ne sont pas identifiables, retourne tous les intervalles.
    """
    try:
        intervals = json.loads(hours_json) if isinstance(hours_json, str) else hours_json
    except Exception:
        return []

    if not intervals:
        return []

    try:
        weekday = pd.to_datetime(scheduled_for).weekday()
        aliases = DAY_ALIASES.get(weekday, [])
        matched = [it for it in intervals if str(it.get("day", "")).lower() in aliases]
        if matched:
            return matched
    except Exception:
        pass

    unknown = [it for it in intervals if str(it.get("day", "")).lower() in ("unknown", "", "none")]
    return unknown if unknown else intervals



def intervals_to_display(hours_json, scheduled_for):
    intervals = choose_intervals_for_day(hours_json, scheduled_for)
    if not intervals:
        return ""
    parts = []
    for it in intervals:
        o = it.get("opens_at")
        c = it.get("closes_at")
        if o and c:
            parts.append(f"{o}-{c}")
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return " / ".join(seen)


def add_hours_display_columns(df):
    df = df.copy()
    if "hours_json" not in df.columns:
        return df
    df["horaires_ouverture"] = df.apply(
        lambda r: "24/24 ou flexible" if r.get("type") == "Casier / Locker" else intervals_to_display(r.get("hours_json", "[]"), r.get("scheduled_for")),
        axis=1,
    )
    df["pause_midi_detectee"] = df.get("has_lunch_break", False)
    return df


def estimate_max_route_duration_for_k(df, k, depot_lat, depot_lon):
    df_sorted, groups = balance_groups_by_service(df, k, depot_lat, depot_lon)
    groups = improve_balance(df_sorted, groups)
    groups = rebalance_groups_by_duration(df_sorted, groups, depot_lat, depot_lon, target_gap=45)
    totals = []
    for group in groups:
        part = df_sorted.loc[group].copy()
        _, _, total = stats_route(part, depot_lat, depot_lon)
        totals.append(total)
    return max(totals) if totals else 0


def choose_auto_route_count(df, depot_lat, depot_lon, target_min=420, max_routes=40):
    """
    Version rapide pour la V1 manager :
    estime directement un nombre cohérent de tournées sans tester 40 optimisations.
    Principe :
    - environ 20 points max par tournée ;
    - contrainte service total ;
    - puis on garde le plus petit nombre cohérent pour ne pas créer trop de tournées.
    """
    n = len(df)
    if n == 0:
        return 1, 0

    total_service = float(df["service"].sum()) if "service" in df.columns else n * 8
    # On réserve une marge pour les trajets et les retours dépôt.
    service_capacity = max(180, int(target_min) - 120)

    k_by_points = math.ceil(n / 20)
    k_by_service = math.ceil(total_service / service_capacity)

    k = max(2, k_by_points, k_by_service)
    k = min(int(max_routes), k)

    # Estimation rapide seulement informative.
    estimated_max = (total_service / k) + 120
    return k, estimated_max




# =========================
# Optimisation
# =========================

def hav(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dlat = math.radians(float(lat2) - float(lat1))
    dlon = math.radians(float(lon2) - float(lon1))
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def order_nearest(df_route, depot_lat, depot_lon):
    remaining = list(df_route.index)
    order = []
    cur_lat, cur_lon = depot_lat, depot_lon
    while remaining:
        best = min(remaining, key=lambda i: hav(cur_lat, cur_lon, df_route.loc[i, "lat"], df_route.loc[i, "lon"]))
        order.append(best)
        cur_lat, cur_lon = df_route.loc[best, "lat"], df_route.loc[best, "lon"]
        remaining.remove(best)
    return order


def stats_route(df_route, depot_lat, depot_lon):
    if df_route.empty:
        return 0, 0, 0

    order = order_nearest(df_route, depot_lat, depot_lon)
    km = 0
    cur_lat, cur_lon = depot_lat, depot_lon

    for i in order:
        km += hav(cur_lat, cur_lon, df_route.loc[i, "lat"], df_route.loc[i, "lon"])
        cur_lat, cur_lon = df_route.loc[i, "lat"], df_route.loc[i, "lon"]

    km += hav(cur_lat, cur_lon, depot_lat, depot_lon)
    km *= ROAD_FACTOR
    service = float(df_route["service"].sum())
    total = km / AVG_SPEED * 60 + service + SAFETY
    return km, service, total


def balance_groups_by_service(df, k, depot_lat, depot_lon):
    df = df.copy().reset_index(drop=True)
    df["angle"] = df.apply(lambda r: math.atan2(float(r["lat"]) - depot_lat, float(r["lon"]) - depot_lon), axis=1)
    df = df.sort_values("angle").reset_index(drop=True)

    total_service = float(df["service"].sum())
    target_service = total_service / k
    min_points = max(3, int(len(df) / k * 0.55))

    groups = [[] for _ in range(k)]
    g = 0
    current_service = 0

    for idx, row in df.iterrows():
        service = float(row["service"])
        remaining_points = len(df) - idx
        remaining_groups = k - g - 1

        can_close = (
            g < k - 1
            and len(groups[g]) >= min_points
            and current_service + service > target_service * 1.06
            and remaining_points > remaining_groups * min_points
        )
        if can_close:
            g += 1
            current_service = 0

        groups[g].append(idx)
        current_service += service

    for i in range(1, len(groups)):
        if 0 < len(groups[i]) < min_points:
            need = min_points - len(groups[i])
            move = groups[i - 1][-need:]
            groups[i - 1] = groups[i - 1][:-need]
            groups[i] = move + groups[i]

    return df, [g for g in groups if g]


def improve_balance(df, groups):
    groups = [g[:] for g in groups if g]
    for _ in range(10):
        loads = [float(df.loc[g, "service"].sum()) for g in groups]
        if max(loads) - min(loads) < 35:
            break
        hi = loads.index(max(loads))
        lo = loads.index(min(loads))
        if len(groups[hi]) <= 4:
            break
        candidates = groups[hi][-3:] + groups[hi][:3]
        best = min(candidates, key=lambda i: float(df.loc[i, "service"]))
        groups[hi].remove(best)
        groups[lo].append(best)
    return groups


def optimise_balanced(df, k, center_code, depot_lat, depot_lon, mode_optimisation="Distance uniquement", heure_depart="08:00"):
    df_sorted, groups = balance_groups_by_service(df, k, depot_lat, depot_lon)
    groups = improve_balance(df_sorted, groups)
    groups = rebalance_groups_by_duration(df_sorted, groups, depot_lat, depot_lon, target_gap=45)

    groups = sorted(
        groups,
        key=lambda g: (
            float(df_sorted.loc[g, "lat"].mean()),
            float(df_sorted.loc[g, "lon"].mean())
        )
    )

    output = []
    summary = []
    start_minute = time_to_min(heure_depart, default=8 * 60)

    for n, group in enumerate(groups, start=1):
        tournee = f"T{n:02d}"
        part = df_sorted.loc[group].copy()

        if mode_optimisation == "Horaires d'ouverture + distance":
            order, eta_by_idx, status_by_idx = route_order_with_hours(part, depot_lat, depot_lon, start_minute)
        else:
            order = order_nearest(part, depot_lat, depot_lon)
            eta_by_idx = {}
            status_by_idx = {}

        cities = list(part["ville"].astype(str).value_counts().head(3).index)
        nom_tournee = tournee + " - " + " / ".join(cities)

        for ordre, idx in enumerate(order, start=1):
            row = part.loc[idx].to_dict()
            row["tournee"] = tournee
            row["nom_tournee"] = nom_tournee
            row["ordre"] = ordre
            row["eta_min"] = eta_by_idx.get(idx, None)
            row["eta"] = min_to_time(row["eta_min"]) if row["eta_min"] is not None else ""
            row["horaire_status"] = status_by_idx.get(idx, "non_calcule")
            output.append(row)

        km, service, total = stats_route(part, depot_lat, depot_lon)
        closed_estimated = sum(1 for idx in order if status_by_idx.get(idx) == "fermé_estime")
        summary.append({
            "tournee": tournee,
            "nom_tournee": nom_tournee,
            "nb_points": len(part),
            "lockers": int((part["type"] == "Casier / Locker").sum()),
            "points_relais": int((part["type"] == "Point relais").sum()),
            "service_min": round(service, 1),
            "distance_proxy_km": round(km, 1),
            "temps_total_min": round(total, 1),
            "points_fermes_estimes": int(closed_estimated),
        })

    opt = pd.DataFrame(output)
    summ = pd.DataFrame(summary)
    opt.to_csv(DATA_DIR / f"points_optimises_latest_{center_code}.csv", index=False, encoding="utf-8-sig")
    opt.to_excel(DATA_DIR / f"points_optimises_latest_{center_code}.xlsx", index=False)
    summ.to_csv(DATA_DIR / f"tournees_summary_latest_{center_code}.csv", index=False, encoding="utf-8-sig")
    summ.to_excel(DATA_DIR / f"tournees_summary_latest_{center_code}.xlsx", index=False)
    return opt, summ


# =========================
# Carte HTML
# =========================

def build_map_html(df, center_code, depot_lat, depot_lon, depot_addr, username):
    points_json = json.dumps(df.to_dict(orient="records"), ensure_ascii=False)
    template = r"""
<!doctype html><html lang="fr"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__CENTER__ - tournées optimisées</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:#0f172a;--panel:#fff;--muted:#64748b;--border:#e2e8f0}
html,body{height:100%;margin:0;font-family:Inter,system-ui,Segoe UI,Arial,sans-serif;background:#f8fafc;color:#0f172a}
.app{display:grid;grid-template-columns:440px 1fr;height:100vh}.sidebar{background:#fff;border-right:1px solid var(--border);padding:18px;overflow:auto}#map{height:100vh;width:100%}
h1{font-size:20px;margin:0 0 8px}.subtitle{color:var(--muted);font-size:13px;line-height:1.35;margin-bottom:14px}
.card{border:1px solid var(--border);border-radius:16px;padding:12px;background:#fff;margin:10px 0;box-shadow:0 2px 8px rgba(15,23,42,.04)}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:8px}.kpi{background:#f1f5f9;border-radius:12px;padding:10px}.kpi span{font-size:12px;color:#64748b}.kpi b{display:block;font-size:18px}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px;font-weight:700}select,button,input{width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--border);background:#fff;font-weight:600;box-sizing:border-box}
button{cursor:pointer;margin-top:8px;background:#0f172a;color:#fff;border:0}button.secondary{background:#e2e8f0;color:#0f172a}
.route-row{display:flex;align-items:flex-start;gap:8px;border-top:1px solid #f1f5f9;padding:8px 0;font-size:13px}.swatch{width:12px;height:12px;border-radius:99px;flex:0 0 auto;margin-top:4px}
.small{font-size:12px;color:#64748b;line-height:1.35}a{color:#2563eb;text-decoration:none;font-weight:700}.legend-dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px}
.ok{background:#ecfdf5;border-color:#bbf7d0;color:#166534}.metric{display:inline-block;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:4px 7px;margin-top:4px}
.badge{display:inline-block;padding:2px 7px;border-radius:999px;background:#dcfce7;color:#166534;font-size:11px;font-weight:800;margin-left:4px}.bad{background:#fee2e2;color:#991b1b}
.map-legend{position:fixed;right:10px;bottom:10px;z-index:9999;background:rgba(255,255,255,.90);border:1px solid #cbd5e1;border-radius:10px;padding:6px 8px;box-shadow:0 3px 10px rgba(15,23,42,.14);font-size:10px;width:205px;max-height:155px;overflow:auto;line-height:1.15}
.map-legend-row{display:flex;align-items:flex-start;gap:4px;margin:2px 0}.map-legend-swatch{width:7px;height:7px;border-radius:999px;display:inline-block;flex:0 0 auto;margin-top:3px}.map-legend-name{display:inline-block;max-width:178px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom}
@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{height:48vh}#map{height:52vh}}
</style></head><body><div class="app"><aside class="sidebar">
<h1>__CENTER__ — tournées optimisées</h1>
<div class="subtitle">Carte actualisée depuis VintedGo. Les modifications sont sauvegardées par utilisateur et centre, puis réappliquées aux nouveaux batchs grâce au code des points.</div>
<div class="card kpis">
<div class="kpi"><span>Points</span><b id="kpiPoints">-</b></div><div class="kpi"><span>Tournées</span><b id="kpiRoutes">-</b></div>
<div class="kpi"><span>Lockers</span><b id="kpiLockers">-</b></div><div class="kpi"><span>Relais</span><b id="kpiRelais">-</b></div>
</div>
<div class="card"><div><span class="legend-dot" style="background:#2563eb"></span>Casier / Locker — 15 min</div><div><span class="legend-dot" style="background:#f97316"></span>Point relais — 5 min</div><div><span class="legend-dot" style="background:#111827"></span>Dépôt</div></div>
<label>Tournée</label><select id="routeSelect"><option value="ALL">Toutes les tournées</option></select>
<label>Type de point</label><select id="typeSelect"><option value="ALL">Tous</option><option value="Casier / Locker">Casier / Locker</option><option value="Point relais">Point relais</option></select>

<div class="card"><b>Modification manuelle</b><div class="small">Déplace un point d’une tournée vers une autre. Tu peux aussi cliquer directement sur un point de la carte pour le déplacer ou le supprimer de la tournée.</div>
<label>Tournée source</label><select id="manualSourceRoute"></select>
<label>Point à déplacer</label><select id="manualPoint"></select>
<label>Nouvelle tournée</label><select id="manualTargetRoute"></select>
<button onclick="moveSelectedPointManual()">Déplacer le point</button>
<button class="secondary" onclick="saveManualScenario()">Sauvegarder dans le navigateur</button>
<button class="secondary" onclick="resetManualScenario()">Réinitialiser</button>
<button class="secondary" onclick="exportCurrentScenarioCSV()">Exporter scénario CSV</button>
<div id="manualInfo" class="small" style="margin-top:8px">Aucun changement manuel.</div><div class="small" style="margin-top:8px"><b>Sauvegarde :</b> les règles sont conservées par code point. Si un nouveau batch contient les mêmes points, les modifications sont réappliquées automatiquement.</div></div>

<div class="card"><b>Créer une tournée</b><div class="small">Crée une tournée vide puis déplace des points dedans.</div>
<label>Nom de la nouvelle tournée</label><input id="newRouteName" placeholder="Ex : T07 - Tournée perso"/>
<button onclick="createCustomRoute()">Créer la tournée</button></div>

<div class="card"><b>Renommer une tournée</b><label>Tournée à renommer</label><select id="renameRouteSelect"></select>
<label>Nouveau nom</label><input id="renameRouteName" placeholder="Ex : T01 - Nîmes"/>
<button class="secondary" onclick="renameSelectedRoute()">Renommer la tournée</button></div>

<div id="balanceInfo" class="card ok small"></div><div id="routeList" class="card"></div>
</aside><main><div id="map"></div></main></div><div class="map-legend" id="mapLegend"></div>

<script>
const CENTER="__CENTER__";
const USERNAME=__USERNAME__;
const DEPOT_ADDR=__DEPOT_ADDR__; const DEPOT={lat:__DEPOT_LAT__,lon:__DEPOT_LON__}; const AVG_SPEED=50; const ROAD_FACTOR=1.15; const SAFETY=10; const MAX_MIN=420;
const INITIAL_POINTS=__POINTS__; let POINTS=JSON.parse(JSON.stringify(INITIAL_POINTS));
const COLORS=["#2563eb","#16a34a","#f97316","#9333ea","#dc2626","#0891b2","#be123c","#4f46e5","#65a30d","#a16207"];
let EXTRA_ROUTES=[]; let ROUTE_NAMES={};
const STORAGE_KEY="vintedgo_multicenter_"+USERNAME+"_"+CENTER+"_persistent_rules";

function autoSaveScenario(){
    const rules = {};
    POINTS.forEach(p => {
        rules[String(p.code)] = {
            tournee: p.tournee,
            ordre: p.ordre,
            nom_tournee: p.nom_tournee,
            hidden_from_route: p.hidden_from_route || false,
            old_tournee: p.old_tournee || null
        };
    });
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
        rules: rules,
        extra: EXTRA_ROUTES,
        names: ROUTE_NAMES,
        saved_at: new Date().toISOString(),
        center: CENTER,
        user: USERNAME
    }));
}

function applySavedRules(){
    try{
        const saved = localStorage.getItem(STORAGE_KEY);
        if(!saved) return;
        const obj = JSON.parse(saved);

        if(obj.extra) EXTRA_ROUTES = obj.extra;
        if(obj.names) ROUTE_NAMES = obj.names;

        const rules = obj.rules || {};
        let applied = 0;

        POINTS.forEach(p => {
            const rule = rules[String(p.code)];
            if(rule){
                p.tournee = rule.tournee || p.tournee;
                p.nom_tournee = routeDisplayName(p.tournee);
                p.hidden_from_route = rule.hidden_from_route || false;
                p.old_tournee = rule.old_tournee || null;
                if(rule.ordre) p.ordre = rule.ordre;
                applied++;
            }
        });

        // Renumérote proprement chaque tournée après application des règles.
        [...new Set(POINTS.map(p => p.tournee))].forEach(r => renumberRoute(r));

        if(applied > 0){
            setTimeout(() => {
                const el = document.getElementById("manualInfo");
                if(el) el.textContent = applied + " règles sauvegardées ont été réappliquées sur les points retrouvés dans ce batch.";
            }, 200);
        }
    }catch(e){}
}
applySavedRules();

const map=L.map("map").setView([DEPOT.lat,DEPOT.lon],8); L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"}).addTo(map);
let markersLayer=L.layerGroup().addTo(map);
L.marker([DEPOT.lat,DEPOT.lon],{icon:L.divIcon({className:"",html:'<div style="background:#111827;color:#fff;border-radius:999px;padding:7px 9px;font-weight:900;border:2px solid #fff">D</div>'})}).addTo(map).bindPopup("<b>Dépôt</b><br>"+DEPOT_ADDR);

function routeIds(){return [...new Set([...POINTS.map(p=>p.tournee),...EXTRA_ROUTES.map(r=>r.id)])].filter(Boolean).sort();}
function routeColor(r){return COLORS[(parseInt(String(r).replace("T",""))-1)%COLORS.length] || "#64748b";}
function routeDisplayName(r){if(ROUTE_NAMES[r])return ROUTE_NAMES[r];const extra=EXTRA_ROUTES.find(x=>x.id===r);if(extra)return extra.name;const p=POINTS.find(x=>x.tournee===r);return p?.nom_tournee||r;}
function hav(a,b,c,d){const R=6371,toRad=x=>x*Math.PI/180;const A=Math.sin(toRad(c-a)/2)**2+Math.cos(toRad(a))*Math.cos(toRad(c))*Math.sin(toRad(d-b)/2)**2;return 2*R*Math.asin(Math.sqrt(A));}
function serviceSum(pts){return pts.reduce((s,p)=>s+(+p.service||0),0);}
function proxyKm(pts){if(!pts.length)return 0;let km=0,cur={lat:DEPOT.lat,lon:DEPOT.lon};pts.slice().sort((a,b)=>a.ordre-b.ordre).forEach(p=>{km+=hav(cur.lat,cur.lon,p.lat,p.lon);cur={lat:p.lat,lon:p.lon};});km+=hav(cur.lat,cur.lon,DEPOT.lat,DEPOT.lon);return km*ROAD_FACTOR;}
function proxyTotal(pts){return serviceSum(pts)+proxyKm(pts)/AVG_SPEED*60+SAFETY;}
function googleLink(pts){if(!pts.length)return "";const origin=`${DEPOT.lat.toFixed(6)},${DEPOT.lon.toFixed(6)}`;const waypoints=pts.slice().sort((a,b)=>a.ordre-b.ordre).map(p=>`${Number(p.lat).toFixed(6)},${Number(p.lon).toFixed(6)}`).join("|");return "https://www.google.com/maps/dir/?api=1&origin="+encodeURIComponent(origin)+"&destination="+encodeURIComponent(origin)+"&waypoints="+encodeURIComponent(waypoints)+"&travelmode=driving&dir_action=navigate&avoid=tolls";}

function renderSelectors(){["routeSelect","manualSourceRoute","manualTargetRoute","renameRouteSelect"].forEach(id=>{const el=document.getElementById(id);if(!el)return;const old=el.value;el.innerHTML=id==="routeSelect"?'<option value="ALL">Toutes les tournées</option>':"";routeIds().forEach(r=>{const opt=document.createElement("option");opt.value=r;opt.textContent=r+" — "+routeDisplayName(r);el.appendChild(opt);});if([...el.options].some(o=>o.value===old))el.value=old;});const rs=document.getElementById("renameRouteSelect"),ri=document.getElementById("renameRouteName");if(rs&&ri)ri.value=routeDisplayName(rs.value||routeIds()[0]||"");renderManualPoints();renderLegend();}
function renderManualPoints(){const r=document.getElementById("manualSourceRoute").value||routeIds()[0];const el=document.getElementById("manualPoint");el.innerHTML="";const pts=POINTS.filter(p=>p.tournee===r).sort((a,b)=>a.ordre-b.ordre);if(!pts.length){el.innerHTML='<option value="">Aucun point</option>';return;}pts.forEach(p=>{const opt=document.createElement("option");opt.value=p.code;opt.textContent=`${p.ordre} - ${p.code} - ${p.nom} (${p.ville})`;el.appendChild(opt);});}
document.addEventListener("change",e=>{if(e.target.id==="routeSelect"||e.target.id==="typeSelect")renderMarkers();if(e.target.id==="manualSourceRoute")renderManualPoints();if(e.target.id==="renameRouteSelect")document.getElementById("renameRouteName").value=routeDisplayName(e.target.value);});

function renderMarkers(){markersLayer.clearLayers();const rt=document.getElementById("routeSelect").value,typ=document.getElementById("typeSelect").value;const shown=POINTS.filter(p=>(rt==="ALL"||p.tournee===rt)&&(typ==="ALL"||p.type===typ));shown.forEach(p=>{const typeColor=p.type==="Casier / Locker"?"#2563eb":"#f97316";const html=`<div style="background:${routeColor(p.tournee)};color:#fff;border:3px solid ${typeColor};width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;box-shadow:0 1px 5px #0005">${p.ordre}</div>`;const popupRoutes = routeIds().map(r => `<option value="${r}" ${r===p.tournee?'selected':''}>${r} — ${routeDisplayName(r)}</option>`).join("");
    L.marker([p.lat,p.lon],{icon:L.divIcon({className:"",html})}).addTo(markersLayer).bindPopup(`
        <b>${p.nom}</b><br>
        <b>${p.tournee}</b> — ${p.type}<br>
        Code : ${p.code}<br>
        ${p.adresse}<br>
        ${p.ville}<br>
        Service : ${p.service} min<br>
        ${p.eta ? `Heure estimée : <b>${p.eta}</b><br>` : ""}
        ${p.horaire_status && p.horaire_status==="fermé_estime" ? `<span style="color:#dc2626;font-weight:900">Risque fermé à cette heure</span><br>` : ""}
        <hr style="border:none;border-top:1px solid #e2e8f0;margin:8px 0">
        <label style="font-size:11px;color:#64748b;font-weight:700">Déplacer vers</label>
        <select id="popupRoute_${p.code}" style="width:100%;padding:6px;border-radius:8px;border:1px solid #cbd5e1">${popupRoutes}</select>
        <button onclick="movePointFromPopup('${p.code}')" style="width:100%;padding:7px;margin-top:6px;border-radius:8px;background:#0f172a;color:white;border:0;cursor:pointer">Déplacer</button>
        <button onclick="removePointFromPopup('${p.code}')" style="width:100%;padding:7px;margin-top:6px;border-radius:8px;background:#dc2626;color:white;border:0;cursor:pointer">Supprimer de la tournée</button>
        <button onclick="restoreHiddenPoints()" style="width:100%;padding:7px;margin-top:6px;border-radius:8px;background:#e2e8f0;color:#0f172a;border:0;cursor:pointer">Réafficher points supprimés</button>
        <div style="font-size:11px;color:#64748b;margin-top:6px">Les changements sont sauvegardés automatiquement.</div>
        <a target="_blank" href="${p.maps}">Ouvrir le point</a>
    `);});if(rt!=="ALL"&&shown.length)map.fitBounds(shown.map(p=>[p.lat,p.lon]),{padding:[25,25]});renderRouteList();}
function renderRouteList(){document.getElementById("kpiPoints").textContent=POINTS.length;document.getElementById("kpiRoutes").textContent=routeIds().length;document.getElementById("kpiLockers").textContent=POINTS.filter(p=>p.type==="Casier / Locker").length;document.getElementById("kpiRelais").textContent=POINTS.filter(p=>p.type==="Point relais").length;const box=document.getElementById("routeList");box.innerHTML="<b>Résumé des tournées</b>";const totals=[];routeIds().forEach(r=>{const pts=POINTS.filter(p=>p.tournee===r).sort((a,b)=>a.ordre-b.ordre);const lockers=pts.filter(p=>p.type==="Casier / Locker").length;const relais=pts.length-lockers;const total=proxyTotal(pts);totals.push(total);const over=pts.length&&total>MAX_MIN;const url=googleLink(pts);const div=document.createElement("div");div.className="route-row";div.innerHTML=`<span class="swatch" style="background:${routeColor(r)}"></span><div><b>${r}</b> — ${routeDisplayName(r)} ${over?'<span class="badge bad">> 7h</span>':'<span class="badge">≤ 7h</span>'}<br><span class="small">${pts.length} pts · ${lockers} lockers · ${relais} relais · service ${Math.round(serviceSum(pts))} min</span><br><span class="metric">Prévisionnel ${Math.round(total)} min</span> <span class="metric">${proxyKm(pts).toFixed(1)} km proxy</span><br>${url?`<a target="_blank" href="${url}">Google Maps complet sans péage</a>`:""}</div>`;box.appendChild(div);});document.getElementById("balanceInfo").innerHTML=`Tournées : <b>${routeIds().length}</b>. Max prévisionnel : <b>${Math.round(Math.max(...totals))} min</b>. Moyenne : <b>${Math.round(totals.reduce((a,b)=>a+b,0)/totals.length)} min</b>.`;renderLegend();}
function renderLegend(){document.getElementById("mapLegend").innerHTML="<b style=\"font-size:10px\">Légende</b>"+routeIds().map(r=>{const name=routeDisplayName(r);return `<div class="map-legend-row" title="${name}"><span class="map-legend-swatch" style="background:${routeColor(r)}"></span><span class="map-legend-name"><b style="display:inline">${r}</b> — ${name.replace(/^T\d+\s*-\s*/,'')} (${POINTS.filter(p=>p.tournee===r).length})</span></div>`}).join("")+'<div class="small" style="margin-top:4px;font-size:10px">Bleu=locker · orange=relais</div>';}
function renumberRoute(r){POINTS.filter(p=>p.tournee===r).sort((a,b)=>a.ordre-b.ordre).forEach((p,i)=>p.ordre=i+1);}
function moveSelectedPointManual(){const code=document.getElementById("manualPoint").value,target=document.getElementById("manualTargetRoute").value,p=POINTS.find(x=>String(x.code)===String(code));if(!code||!p||!target)return;const source=p.tournee;p.tournee=target;p.nom_tournee=routeDisplayName(target);renumberRoute(source);renumberRoute(target);autoSaveScenario();document.getElementById("manualInfo").textContent=`Point ${code} déplacé de ${source} vers ${target}. Modif sauvegardée sur ce compte.`;renderSelectors();renderMarkers();}
function nextRouteId(){let maxId=0;routeIds().forEach(r=>{const m=String(r).match(/T(\d+)/);if(m)maxId=Math.max(maxId,parseInt(m[1],10));});return "T"+String(maxId+1).padStart(2,"0");}
function createCustomRoute(){const input=document.getElementById("newRouteName");const id=nextRouteId();const name=(input?.value||"").trim()||id+" - Nouvelle tournée";EXTRA_ROUTES.push({id,name});ROUTE_NAMES[id]=name;if(input)input.value="";autoSaveScenario();document.getElementById("manualInfo").textContent=`Tournée ${id} créée. Modif sauvegardée sur ce compte.`;renderSelectors();renderMarkers();}
function renameSelectedRoute(){const id=document.getElementById("renameRouteSelect").value;const name=(document.getElementById("renameRouteName").value||"").trim();if(!id||!name)return;ROUTE_NAMES[id]=name;POINTS.filter(p=>p.tournee===id).forEach(p=>p.nom_tournee=name);let extra=EXTRA_ROUTES.find(x=>x.id===id);if(extra)extra.name=name;autoSaveScenario();document.getElementById("manualInfo").textContent=`Tournée ${id} renommée. Modif sauvegardée sur ce compte.`;renderSelectors();renderMarkers();}

function movePointFromPopup(code){
    const select = document.getElementById("popupRoute_"+code);
    if(!select) return;
    const target = select.value;
    const p = POINTS.find(x => String(x.code) === String(code));
    if(!p || !target) return;
    const source = p.tournee;
    p.tournee = target;
    p.nom_tournee = routeDisplayName(target);
    renumberRoute(source);
    renumberRoute(target);
    autoSaveScenario();
    map.closePopup();
    document.getElementById("manualInfo").textContent = `Point ${code} déplacé de ${source} vers ${target} depuis la carte. Modif sauvegardée.`;
    renderSelectors();
    renderMarkers();
}

function removePointFromPopup(code){
    const p = POINTS.find(x => String(x.code) === String(code));
    if(!p) return;
    const source = p.tournee;
    p.hidden_from_route = true;
    p.old_tournee = source;
    p.tournee = "SUPPR";
    p.nom_tournee = "SUPPR - Points supprimés";
    renumberRoute(source);
    renumberRoute("SUPPR");
    if(!EXTRA_ROUTES.find(x => x.id === "SUPPR")){
        EXTRA_ROUTES.push({id:"SUPPR", name:"SUPPR - Points supprimés"});
        ROUTE_NAMES["SUPPR"] = "SUPPR - Points supprimés";
    }
    autoSaveScenario();
    map.closePopup();
    document.getElementById("manualInfo").textContent = `Point ${code} supprimé de la tournée ${source}. Il reste récupérable via "Réafficher points supprimés".`;
    renderSelectors();
    renderMarkers();
}

function restoreHiddenPoints(){
    POINTS.filter(p => p.hidden_from_route === true).forEach(p => {
        p.tournee = p.old_tournee || "T01";
        p.nom_tournee = routeDisplayName(p.tournee);
        p.hidden_from_route = false;
    });
    routeIds().forEach(r => renumberRoute(r));
    autoSaveScenario();
    map.closePopup();
    document.getElementById("manualInfo").textContent = "Points supprimés réaffichés dans leur ancienne tournée.";
    renderSelectors();
    renderMarkers();
}

function saveManualScenario(){autoSaveScenario();document.getElementById("manualInfo").textContent="Règles sauvegardées par code point : elles seront réappliquées sur les prochains batchs du même centre."}
function resetManualScenario(){localStorage.removeItem(STORAGE_KEY);POINTS=JSON.parse(JSON.stringify(INITIAL_POINTS));EXTRA_ROUTES=[];ROUTE_NAMES={};renderSelectors();renderMarkers();document.getElementById("manualInfo").textContent="Règles sauvegardées supprimées pour ce compte et ce centre."}
function exportCurrentScenarioCSV(){const header=["tournee","ordre","code","nom","adresse","ville","type","lat","lon","service"];const rows=[header.join(";")].concat(POINTS.slice().sort((a,b)=>a.tournee.localeCompare(b.tournee)||a.ordre-b.ordre).map(p=>header.map(h=>String(p[h]??"").replaceAll(";",",")).join(";")));const blob=new Blob([rows.join("\n")],{type:"text/csv;charset=utf-8"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="scenario_"+CENTER+".csv";a.click();}
renderSelectors();renderMarkers();if(POINTS.length)map.fitBounds(POINTS.map(p=>[p.lat,p.lon]),{padding:[25,25]});
</script></body></html>
"""
    return (
        template
        .replace("__CENTER__", center_code)
        .replace("__USERNAME__", json.dumps(username, ensure_ascii=False))
        .replace("__POINTS__", points_json)
        .replace("__DEPOT_ADDR__", json.dumps(depot_addr, ensure_ascii=False))
        .replace("__DEPOT_LAT__", str(depot_lat))
        .replace("__DEPOT_LON__", str(depot_lon))
    )


# =========================
# Streamlit UI
# =========================

st.set_page_config(page_title="VintedGo Multi-centres", layout="wide")
st.title("VintedGo — optimisation distance multi-centres")

allowed_centers = [c for c in st.session_state.allowed_centers if c in CENTERS]
if not allowed_centers:
    st.error("Aucun centre autorisé pour ce compte.")
    st.stop()

with st.sidebar:
    st.success(f"Connecté : {st.session_state.username}")
    if st.button("Se déconnecter"):
        for k in ["authenticated", "username", "allowed_centers", "df", "opt", "summary", "batches_df"]:
            st.session_state.pop(k, None)
        st.rerun()

    st.header("Configuration")
    selected_center = st.selectbox("Centre", allowed_centers, index=0)
    cfg = CENTERS[selected_center]

    st.caption(f"Sorting center ID : {cfg['sorting_center_id']}")
    st.caption(f"Dépôt : {cfg['depot_addr']}")

    depot_lat = st.number_input("Latitude dépôt", value=float(cfg["depot_lat"]), format="%.7f")
    depot_lon = st.number_input("Longitude dépôt", value=float(cfg["depot_lon"]), format="%.7f")
    depot_addr = st.text_input("Adresse dépôt", value=cfg["depot_addr"])

    auto_nb_tournees = st.checkbox("Utiliser le nombre de tournées conseillé automatiquement", value=True)
    target_route_min = st.number_input("Durée cible max par tournée (min)", min_value=300, max_value=600, value=420, step=15)
    max_auto_routes = st.number_input("Maximum de tournées autorisées", min_value=5, max_value=60, value=40, step=1)

    nb_tournees = st.number_input(
        "Nombre de tournées manuel / ajustement",
        min_value=2,
        max_value=60,
        value=int(cfg["default_routes"]),
        help="Décoche l’option automatique si tu veux forcer ce nombre."
    )

    manual_batch = st.text_input("Batch ID manuel (optionnel)", value="")

col1, col2, col3, col4 = st.columns(4)
auto = col1.button("Actualiser dernière tournée", type="primary")
manual = col2.button("Synchroniser Batch ID")
load = col3.button("Charger fichier local")
opt_btn = col4.button("Optimiser équilibré")

for key in ["df", "opt", "summary", "batches_df"]:
    if key not in st.session_state:
        st.session_state[key] = None

if auto:
    try:
        if not TOKEN:
            raise RuntimeError("VINTED_TOKEN manquant dans les secrets.")
        with st.spinner(f"Détection du dernier batch {selected_center}..."):
            latest_id, batches_df = detect_latest_batch(TOKEN, cfg["sorting_center_id"], selected_center)
            st.session_state.batches_df = batches_df
        st.success(f"Dernier batch {selected_center} détecté : {latest_id}")

        with st.spinner(f"Scraping {selected_center} batch {latest_id}..."):
            st.session_state.df = fetch_points(TOKEN, latest_id, selected_center)
            st.session_state.opt = None
            st.session_state.summary = None
        st.success(f"{len(st.session_state.df)} points récupérés pour {selected_center}.")
    except Exception as e:
        st.error(str(e))

if manual:
    try:
        if not TOKEN:
            raise RuntimeError("VINTED_TOKEN manquant dans les secrets.")
        if not manual_batch:
            raise RuntimeError("Indique un Batch ID manuel.")
        with st.spinner(f"Scraping {selected_center} batch {manual_batch}..."):
            st.session_state.df = fetch_points(TOKEN, manual_batch, selected_center)
            st.session_state.opt = None
            st.session_state.summary = None
        st.success(f"{len(st.session_state.df)} points récupérés pour {selected_center}.")
    except Exception as e:
        st.error(str(e))

if load:
    p = DATA_DIR / f"points_vinted_latest_{selected_center}.csv"
    if p.exists():
        st.session_state.df = pd.read_csv(p)
        st.session_state.opt = None
        st.session_state.summary = None
        st.success("Fichier local chargé.")
    else:
        st.error("Aucun fichier local trouvé pour ce centre.")

if st.session_state.batches_df is not None:
    st.subheader("Batchs détectés")
    st.dataframe(st.session_state.batches_df, use_container_width=True, height=180)

df = st.session_state.df

if df is not None and not df.empty:
    st.subheader(f"Points récupérés — {selected_center}")
    a, b, c, d = st.columns(4)
    a.metric("Points", len(df))
    b.metric("Lockers", int((df["type"] == "Casier / Locker").sum()))
    c.metric("Relais", int((df["type"] == "Point relais").sum()))
    d.metric("Batch", str(df["batch_id"].iloc[0]) if "batch_id" in df else "-")

    display_df = df.copy()
    if "hours_json" in display_df.columns and "horaires_ouverture" not in display_df.columns:
        display_df = add_hours_display_columns(display_df)
    preferred_cols = [
        "code", "type", "nom", "adresse", "ville", "code_postal",
        "lat", "lon", "maps"
    ]
    cols = [c for c in preferred_cols if c in display_df.columns] + [c for c in display_df.columns if c not in preferred_cols]
    st.dataframe(display_df[cols], use_container_width=True, height=260)

    if opt_btn:
        try:
            df_for_opt = df.copy()

            with st.spinner("Détection rapide du nombre de tournées..." if auto_nb_tournees else "Préparation optimisation..."):
                if auto_nb_tournees:
                    selected_k, estimated_max = choose_auto_route_count(
                        df_for_opt,
                        depot_lat,
                        depot_lon,
                        target_min=int(target_route_min),
                        max_routes=int(max_auto_routes),
                    )
                    st.info(
                        f"Nombre de tournées conseillé : {selected_k} "
                        f"— estimation rapide max : {round(estimated_max)} min. "
                        f"Pour forcer un autre nombre, décoche l’option automatique."
                    )
                else:
                    selected_k = int(nb_tournees)

            with st.spinner("Optimisation distance équilibrée..."):
                opt, summary = optimise_balanced(
                    df_for_opt,
                    selected_k,
                    selected_center,
                    depot_lat,
                    depot_lon,
                    mode_optimisation="Distance uniquement",
                    heure_depart="08:00",
                )
                st.session_state.opt = opt
                st.session_state.summary = summary
            st.success(f"Optimisation terminée — distance uniquement — {selected_k} tournées.")
        except Exception as e:
            st.error(str(e))

if st.session_state.opt is not None and not st.session_state.opt.empty:
    opt = st.session_state.opt
    summary = st.session_state.summary
    st.subheader("Résumé des tournées")
    if "temps_total_min" in summary.columns and not summary.empty:
        ecart = float(summary["temps_total_min"].max() - summary["temps_total_min"].min())
        st.metric("Écart max/min entre tournées", f"{round(ecart)} min")
        if ecart > 45:
            st.warning("Écart supérieur à 45 min : tu peux augmenter légèrement le nombre de tournées ou ajuster manuellement certains points.")
    st.dataframe(summary, use_container_width=True)

    html = build_map_html(opt, selected_center, depot_lat, depot_lon, depot_addr, st.session_state.username)
    (DATA_DIR / f"carte_{selected_center}_latest.html").write_text(html, encoding="utf-8")

    st.download_button("Télécharger la carte HTML", html, file_name=f"carte_{selected_center}_modifiable.html", mime="text/html")
    st.download_button("Télécharger CSV optimisé", opt.to_csv(index=False, encoding="utf-8-sig"), file_name=f"points_optimises_{selected_center}.csv", mime="text/csv")

    components.html(html, height=720, scrolling=False)
else:
    st.info("Choisis un centre, actualise la dernière tournée, puis clique sur Optimiser équilibré. Version manager : optimisation distance uniquement.")

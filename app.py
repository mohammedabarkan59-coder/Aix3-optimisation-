
import os
import json
import math
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

TOKEN_ENV = os.getenv("VINTED_TOKEN", "")
BATCH_ENV = os.getenv("VINTED_BATCH_ID", "59545")
SORTING_CENTER_ID_ENV = os.getenv("VINTED_SORTING_CENTER_ID", "288")

DEPOT_LAT = float(os.getenv("DEPOT_LAT", "43.6313869"))
DEPOT_LON = float(os.getenv("DEPOT_LON", "4.7799518"))
DEPOT_ADDR = os.getenv("DEPOT_ADDR", "Vinted Go, Bâtiment A, 13310 Saint-Martin-de-Crau")

AVG_SPEED = 50
ROAD_FACTOR = 1.15
SAFETY = 10
MAX_MIN = 420



# ============================================================
# DÉTECTION AUTOMATIQUE DU DERNIER BATCH AIX3
# ============================================================

def detect_latest_batch(token: str, sorting_center_id: str = "288", limit: int = 10):
    """
    Récupère la liste des derniers lots AIX3 puis retourne l'id du plus récent.
    Endpoint récupéré depuis Network :
    /drivers/point_visits_batches?limit=10&sorting_center_id=288
    """
    if not token:
        raise RuntimeError("Token manquant. Mets VINTED_TOKEN dans .env ou colle-le dans la sidebar.")

    url = f"https://carrier.vintedgo.com/drivers/point_visits_batches?limit={limit}&sorting_center_id={sorting_center_id}"

    headers = {
        "accept": "*/*",
        "accept-language": "en",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": "https://admin.vintedgo.com",
        "referer": "https://admin.vintedgo.com/",
        "user-agent": "Mozilla/5.0",
    }

    r = requests.get(url, headers=headers, timeout=40)

    if r.status_code == 401:
        raise RuntimeError("Token expiré ou incorrect. Reprends un nouveau token via Copy as cURL.")
    r.raise_for_status()

    data = r.json()

    with open(DATA_DIR / "debug_batches_response.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # L'API peut renvoyer soit une liste directe, soit un dict avec une clé contenant la liste.
    if isinstance(data, list):
        batches = data
    elif isinstance(data, dict):
        possible_keys = [
            "point_visits_batches",
            "point_visit_batches",
            "batches",
            "data",
            "items",
            "results",
        ]
        batches = []
        for key in possible_keys:
            if isinstance(data.get(key), list):
                batches = data[key]
                break

        # fallback récursif : chercher une liste d'objets qui contiennent id + scheduled_for
        if not batches:
            def find_batch_list(obj):
                if isinstance(obj, list):
                    if obj and all(isinstance(x, dict) for x in obj):
                        if any("scheduled_for" in x and "id" in x for x in obj):
                            return obj
                    for x in obj:
                        found = find_batch_list(x)
                        if found:
                            return found
                elif isinstance(obj, dict):
                    for v in obj.values():
                        found = find_batch_list(v)
                        if found:
                            return found
                return []
            batches = find_batch_list(data)
    else:
        batches = []

    if not batches:
        raise RuntimeError("Aucun batch trouvé dans la réponse. Regarde data/debug_batches_response.json.")

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
            "point_visits_count": b.get("point_visits_count") or b.get("visits_count") or b.get("visits") or b.get("point_count"),
        })

    batches_df = pd.DataFrame(rows)

    # On garde uniquement les lignes avec un ID.
    batches_df = batches_df.dropna(subset=["id"]).copy()

    if batches_df.empty:
        raise RuntimeError("La liste des batchs a été récupérée, mais aucun ID n'a été trouvé.")

    # Tri : le plus récent d'abord. La page Vinted semble déjà triée, mais on sécurise.
    if "scheduled_for" in batches_df.columns:
        batches_df["_date_sort"] = pd.to_datetime(batches_df["scheduled_for"], errors="coerce")
    else:
        batches_df["_date_sort"] = pd.NaT

    batches_df["_id_sort"] = pd.to_numeric(batches_df["id"], errors="coerce")
    batches_df = batches_df.sort_values(["_date_sort", "_id_sort"], ascending=[False, False])

    latest_id = str(int(batches_df.iloc[0]["id"]))

    clean_df = batches_df.drop(columns=["_date_sort", "_id_sort"], errors="ignore")
    clean_df.to_csv(DATA_DIR / "batches_detected_latest.csv", index=False, encoding="utf-8-sig")
    clean_df.to_excel(DATA_DIR / "batches_detected_latest.xlsx", index=False)

    return latest_id, clean_df


# ============================================================
# SCRAPER VINTEDGO
# ============================================================

def fetch_vinted(batch_id: str, token: str) -> pd.DataFrame:
    url = f"https://carrier.vintedgo.com/drivers/point_visits_batches/{batch_id}/route_editor_data"
    headers = {
        "accept": "*/*",
        "accept-language": "en",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": "https://admin.vintedgo.com",
        "referer": "https://admin.vintedgo.com/",
        "user-agent": "Mozilla/5.0",
    }

    r = requests.get(url, headers=headers, timeout=40)

    if r.status_code == 401:
        raise RuntimeError("Token expiré ou incorrect. Reprends un nouveau token via Copy as cURL.")
    r.raise_for_status()

    data = r.json()

    with open(DATA_DIR / "debug_vinted_response.json", "w", encoding="utf-8") as f:
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

        rows.append({
            "batch_id": batch.get("id"),
            "batch_status": batch.get("status"),
            "scheduled_for": batch.get("scheduled_for"),
            "sorting_center_code": batch.get("sorting_center_code"),
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
            "lat": p.get("latitude"),
            "lon": p.get("longitude"),
            "maps": None,
        })

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    df["maps"] = df.apply(lambda r: f"https://www.google.com/maps/search/?api=1&query={r['lat']:.6f},{r['lon']:.6f}", axis=1)

    df.to_csv(DATA_DIR / "points_vinted_latest.csv", index=False, encoding="utf-8-sig")
    df.to_excel(DATA_DIR / "points_vinted_latest.xlsx", index=False)

    return df


# ============================================================
# OPTIMISATION ÉQUILIBRÉE ET RAPIDE
# ============================================================

def hav(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dlat = math.radians(float(lat2) - float(lat1))
    dlon = math.radians(float(lon2) - float(lon1))
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def order_nearest(df_route: pd.DataFrame):
    remaining = list(df_route.index)
    order = []
    cur_lat, cur_lon = DEPOT_LAT, DEPOT_LON

    while remaining:
        best = min(remaining, key=lambda i: hav(cur_lat, cur_lon, df_route.loc[i, "lat"], df_route.loc[i, "lon"]))
        order.append(best)
        cur_lat, cur_lon = df_route.loc[best, "lat"], df_route.loc[best, "lon"]
        remaining.remove(best)

    return order


def stats_route(df_route: pd.DataFrame):
    if df_route.empty:
        return 0, 0, 0

    order = order_nearest(df_route)
    km = 0
    cur_lat, cur_lon = DEPOT_LAT, DEPOT_LON

    for i in order:
        km += hav(cur_lat, cur_lon, df_route.loc[i, "lat"], df_route.loc[i, "lon"])
        cur_lat, cur_lon = df_route.loc[i, "lat"], df_route.loc[i, "lon"]

    km += hav(cur_lat, cur_lon, DEPOT_LAT, DEPOT_LON)
    km *= ROAD_FACTOR

    service = float(df_route["service"].sum())
    total = km / AVG_SPEED * 60 + service + SAFETY

    return km, service, total


def balance_groups_by_service(df: pd.DataFrame, k: int):
    """
    Découpage rapide qui évite les tournées ridicules :
    - tri géographique par angle autour du dépôt ;
    - découpage en k blocs avec charge de service équilibrée ;
    - chaque tournée a un minimum de points raisonnable.
    """
    df = df.copy().reset_index(drop=True)
    df["angle"] = df.apply(lambda r: math.atan2(float(r["lat"]) - DEPOT_LAT, float(r["lon"]) - DEPOT_LON), axis=1)
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

    # si une dernière tournée est trop petite, on la fusionne/rééquilibre avec la précédente
    for i in range(1, len(groups)):
        if 0 < len(groups[i]) < min_points:
            need = min_points - len(groups[i])
            move = groups[i - 1][-need:]
            groups[i - 1] = groups[i - 1][:-need]
            groups[i] = move + groups[i]

    return df, groups


def improve_balance(df: pd.DataFrame, groups):
    """
    Petite amélioration rapide :
    si une tournée est beaucoup plus chargée en service, on déplace un point proche vers une tournée voisine.
    Pas de boucle lourde.
    """
    groups = [g[:] for g in groups if g]

    for _ in range(10):
        loads = [float(df.loc[g, "service"].sum()) for g in groups]
        if max(loads) - min(loads) < 35:
            break

        hi = loads.index(max(loads))
        lo = loads.index(min(loads))

        if len(groups[hi]) <= 4:
            break

        # on déplace le point de bord géographique le plus simple, vers la tournée la plus légère
        candidates = groups[hi][-3:] + groups[hi][:3]
        best = min(candidates, key=lambda i: float(df.loc[i, "service"]))
        groups[hi].remove(best)
        groups[lo].append(best)

    return groups


def optimise_balanced(df: pd.DataFrame, k: int):
    df_sorted, groups = balance_groups_by_service(df, k)
    groups = improve_balance(df_sorted, groups)

    output = []
    summary = []

    # ordre géographique stable
    groups = sorted(
        groups,
        key=lambda g: (
            float(df_sorted.loc[g, "lat"].mean()),
            float(df_sorted.loc[g, "lon"].mean())
        )
    )

    for n, group in enumerate(groups, start=1):
        if not group:
            continue

        tournee = f"T{n:02d}"
        part = df_sorted.loc[group].copy()
        order = order_nearest(part)
        cities = list(part["ville"].astype(str).value_counts().head(3).index)
        nom_tournee = tournee + " - " + " / ".join(cities)

        for ordre, idx in enumerate(order, start=1):
            row = part.loc[idx].to_dict()
            row["tournee"] = tournee
            row["nom_tournee"] = nom_tournee
            row["ordre"] = ordre
            output.append(row)

        km, service, total = stats_route(part)
        summary.append({
            "tournee": tournee,
            "nom_tournee": nom_tournee,
            "nb_points": len(part),
            "lockers": int((part["type"] == "Casier / Locker").sum()),
            "points_relais": int((part["type"] == "Point relais").sum()),
            "service_min": round(service, 1),
            "distance_proxy_km": round(km, 1),
            "temps_total_min": round(total, 1),
        })

    opt = pd.DataFrame(output)
    summ = pd.DataFrame(summary)

    opt.to_csv(DATA_DIR / "points_optimises_latest.csv", index=False, encoding="utf-8-sig")
    opt.to_excel(DATA_DIR / "points_optimises_latest.xlsx", index=False)
    summ.to_csv(DATA_DIR / "tournees_summary_latest.csv", index=False, encoding="utf-8-sig")
    summ.to_excel(DATA_DIR / "tournees_summary_latest.xlsx", index=False)

    return opt, summ


# ============================================================
# CARTE COMME L'ANCIENNE : MODIFICATION, CRÉATION, RENOMMAGE
# ============================================================

def build_map_html(df: pd.DataFrame):
    points_json = json.dumps(df.to_dict(orient="records"), ensure_ascii=False)
    return f"""
<!doctype html><html lang="fr"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AIX3 - tournées optimisées</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{{--bg:#0f172a;--panel:#fff;--muted:#64748b;--border:#e2e8f0}}
html,body{{height:100%;margin:0;font-family:Inter,system-ui,Segoe UI,Arial,sans-serif;background:#f8fafc;color:#0f172a}}
.app{{display:grid;grid-template-columns:440px 1fr;height:100vh}}.sidebar{{background:#fff;border-right:1px solid var(--border);padding:18px;overflow:auto}}#map{{height:100vh;width:100%}}
h1{{font-size:20px;margin:0 0 8px}}.subtitle{{color:var(--muted);font-size:13px;line-height:1.35;margin-bottom:14px}}
.card{{border:1px solid var(--border);border-radius:16px;padding:12px;background:#fff;margin:10px 0;box-shadow:0 2px 8px rgba(15,23,42,.04)}}
.kpis{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.kpi{{background:#f1f5f9;border-radius:12px;padding:10px}}.kpi span{{font-size:12px;color:#64748b}}.kpi b{{display:block;font-size:18px}}
label{{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px;font-weight:700}}select,button,input{{width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--border);background:#fff;font-weight:600;box-sizing:border-box}}
button{{cursor:pointer;margin-top:8px;background:#0f172a;color:#fff;border:0}}button.secondary{{background:#e2e8f0;color:#0f172a}}
.route-row{{display:flex;align-items:flex-start;gap:8px;border-top:1px solid #f1f5f9;padding:8px 0;font-size:13px}}.swatch{{width:12px;height:12px;border-radius:99px;flex:0 0 auto;margin-top:4px}}
.small{{font-size:12px;color:#64748b;line-height:1.35}}a{{color:#2563eb;text-decoration:none;font-weight:700}}.legend-dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px}}
.ok{{background:#ecfdf5;border-color:#bbf7d0;color:#166534}}.warn{{background:#fff7ed;border-color:#fed7aa;color:#9a3412}}.metric{{display:inline-block;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:4px 7px;margin-top:4px}}
.badge{{display:inline-block;padding:2px 7px;border-radius:999px;background:#dcfce7;color:#166534;font-size:11px;font-weight:800;margin-left:4px}}.bad{{background:#fee2e2;color:#991b1b}}
.map-legend{{position:fixed;right:18px;bottom:18px;z-index:9999;background:rgba(255,255,255,.96);border:1px solid #cbd5e1;border-radius:14px;padding:10px 12px;box-shadow:0 4px 14px rgba(15,23,42,.18);font-size:12px;max-width:280px;line-height:1.35}}
.map-legend-row{{display:flex;align-items:center;gap:7px;margin:4px 0}}.map-legend-swatch{{width:12px;height:12px;border-radius:999px;display:inline-block;flex:0 0 auto}}
@media(max-width:900px){{.app{{grid-template-columns:1fr}}.sidebar{{height:48vh}}#map{{height:52vh}}}}
</style></head><body><div class="app"><aside class="sidebar">
<h1>AIX3 — tournées optimisées</h1>
<div class="subtitle">Carte actualisée depuis VintedGo. Contraintes : locker 15 min, point relais 5 min, tournées équilibrées, liens Google Maps sans péage.</div>
<div class="card kpis">
<div class="kpi"><span>Points</span><b id="kpiPoints">-</b></div><div class="kpi"><span>Tournées</span><b id="kpiRoutes">-</b></div>
<div class="kpi"><span>Lockers</span><b id="kpiLockers">-</b></div><div class="kpi"><span>Relais</span><b id="kpiRelais">-</b></div>
</div>
<div class="card"><div><span class="legend-dot" style="background:#2563eb"></span>Casier / Locker — 15 min</div><div><span class="legend-dot" style="background:#f97316"></span>Point relais — 5 min</div><div><span class="legend-dot" style="background:#111827"></span>Dépôt</div></div>
<label>Tournée</label><select id="routeSelect"><option value="ALL">Toutes les tournées</option></select>
<label>Type de point</label><select id="typeSelect"><option value="ALL">Tous</option><option value="Casier / Locker">Casier / Locker</option><option value="Point relais">Point relais</option></select>

<div class="card"><b>Modification manuelle</b><div class="small">Déplace un point d’une tournée vers une autre.</div>
<label>Tournée source</label><select id="manualSourceRoute"></select>
<label>Point à déplacer</label><select id="manualPoint"></select>
<label>Nouvelle tournée</label><select id="manualTargetRoute"></select>
<button onclick="moveSelectedPointManual()">Déplacer le point</button>
<button class="secondary" onclick="saveManualScenario()">Sauvegarder dans le navigateur</button>
<button class="secondary" onclick="resetManualScenario()">Réinitialiser</button>
<button class="secondary" onclick="exportCurrentScenarioCSV()">Exporter scénario CSV</button>
<div id="manualInfo" class="small" style="margin-top:8px">Aucun changement manuel.</div></div>

<div class="card"><b>Créer une tournée</b><div class="small">Crée une tournée vide puis déplace des points dedans.</div>
<label>Nom de la nouvelle tournée</label><input id="newRouteName" placeholder="Ex : T07 - Tournée perso"/>
<button onclick="createCustomRoute()">Créer la tournée</button></div>

<div class="card"><b>Renommer une tournée</b><label>Tournée à renommer</label><select id="renameRouteSelect"></select>
<label>Nouveau nom</label><input id="renameRouteName" placeholder="Ex : T01 - Nîmes"/>
<button class="secondary" onclick="renameSelectedRoute()">Renommer la tournée</button></div>

<div id="balanceInfo" class="card ok small"></div><div id="routeList" class="card"></div>
</aside><main><div id="map"></div></main></div><div class="map-legend" id="mapLegend"></div>

<script>
const DEPOT_ADDR={json.dumps(DEPOT_ADDR)}; const DEPOT={{lat:{DEPOT_LAT},lon:{DEPOT_LON}}}; const AVG_SPEED=50; const ROAD_FACTOR=1.15; const SAFETY=10; const MAX_MIN=420;
const INITIAL_POINTS={points_json}; let POINTS=JSON.parse(JSON.stringify(INITIAL_POINTS));
const COLORS=["#2563eb","#16a34a","#f97316","#9333ea","#dc2626","#0891b2","#be123c","#4f46e5","#65a30d","#a16207"];
let EXTRA_ROUTES=[]; let ROUTE_NAMES={{}};
const STORAGE_KEY="aix3_balanced_map_"+(POINTS[0]?.batch_id||"latest");

try{{const saved=localStorage.getItem(STORAGE_KEY);if(saved){{const obj=JSON.parse(saved);if(obj.points)POINTS=obj.points;if(obj.extra)EXTRA_ROUTES=obj.extra;if(obj.names)ROUTE_NAMES=obj.names;}}}}catch(e){{}}

const map=L.map("map").setView([DEPOT.lat,DEPOT.lon],8); L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",{{maxZoom:19,attribution:"© OpenStreetMap"}}).addTo(map);
let markersLayer=L.layerGroup().addTo(map);
L.marker([DEPOT.lat,DEPOT.lon],{{icon:L.divIcon({{className:"",html:'<div style="background:#111827;color:#fff;border-radius:999px;padding:7px 9px;font-weight:900;border:2px solid #fff">D</div>'}})}}).addTo(map).bindPopup("<b>Dépôt</b><br>"+DEPOT_ADDR);

function routeIds(){{return [...new Set([...POINTS.map(p=>p.tournee),...EXTRA_ROUTES.map(r=>r.id)])].filter(Boolean).sort();}}
function routeColor(r){{return COLORS[(parseInt(String(r).replace("T",""))-1)%COLORS.length] || "#64748b";}}
function routeDisplayName(r){{if(ROUTE_NAMES[r])return ROUTE_NAMES[r];const extra=EXTRA_ROUTES.find(x=>x.id===r);if(extra)return extra.name;const p=POINTS.find(x=>x.tournee===r);return p?.nom_tournee||r;}}
function hav(a,b,c,d){{const R=6371,toRad=x=>x*Math.PI/180;const A=Math.sin(toRad(c-a)/2)**2+Math.cos(toRad(a))*Math.cos(toRad(c))*Math.sin(toRad(d-b)/2)**2;return 2*R*Math.asin(Math.sqrt(A));}}
function serviceSum(pts){{return pts.reduce((s,p)=>s+(+p.service||0),0);}}
function proxyKm(pts){{if(!pts.length)return 0;let km=0,cur={{lat:DEPOT.lat,lon:DEPOT.lon}};pts.slice().sort((a,b)=>a.ordre-b.ordre).forEach(p=>{{km+=hav(cur.lat,cur.lon,p.lat,p.lon);cur={{lat:p.lat,lon:p.lon}};}});km+=hav(cur.lat,cur.lon,DEPOT.lat,DEPOT.lon);return km*ROAD_FACTOR;}}
function proxyTotal(pts){{return serviceSum(pts)+proxyKm(pts)/AVG_SPEED*60+SAFETY;}}
function googleLink(pts){{if(!pts.length)return "";const origin=`${{DEPOT.lat.toFixed(6)}},${{DEPOT.lon.toFixed(6)}}`;const waypoints=pts.slice().sort((a,b)=>a.ordre-b.ordre).map(p=>`${{Number(p.lat).toFixed(6)}},${{Number(p.lon).toFixed(6)}}`).join("|");return "https://www.google.com/maps/dir/?api=1&origin="+encodeURIComponent(origin)+"&destination="+encodeURIComponent(origin)+"&waypoints="+encodeURIComponent(waypoints)+"&travelmode=driving&dir_action=navigate&avoid=tolls";}}

function renderSelectors(){{["routeSelect","manualSourceRoute","manualTargetRoute","renameRouteSelect"].forEach(id=>{{const el=document.getElementById(id);if(!el)return;const old=el.value;el.innerHTML=id==="routeSelect"?'<option value="ALL">Toutes les tournées</option>':"";routeIds().forEach(r=>{{const opt=document.createElement("option");opt.value=r;opt.textContent=r+" — "+routeDisplayName(r);el.appendChild(opt);}});if([...el.options].some(o=>o.value===old))el.value=old;}});const rs=document.getElementById("renameRouteSelect"),ri=document.getElementById("renameRouteName");if(rs&&ri)ri.value=routeDisplayName(rs.value||routeIds()[0]||"");renderManualPoints();renderLegend();}}
function renderManualPoints(){{const r=document.getElementById("manualSourceRoute").value||routeIds()[0];const el=document.getElementById("manualPoint");el.innerHTML="";const pts=POINTS.filter(p=>p.tournee===r).sort((a,b)=>a.ordre-b.ordre);if(!pts.length){{el.innerHTML='<option value="">Aucun point</option>';return;}}pts.forEach(p=>{{const opt=document.createElement("option");opt.value=p.code;opt.textContent=`${{p.ordre}} - ${{p.code}} - ${{p.nom}} (${{p.ville}})`;el.appendChild(opt);}});}}
document.addEventListener("change",e=>{{if(e.target.id==="routeSelect"||e.target.id==="typeSelect")renderMarkers();if(e.target.id==="manualSourceRoute")renderManualPoints();if(e.target.id==="renameRouteSelect")document.getElementById("renameRouteName").value=routeDisplayName(e.target.value);}});

function renderMarkers(){{markersLayer.clearLayers();const rt=document.getElementById("routeSelect").value,typ=document.getElementById("typeSelect").value;const shown=POINTS.filter(p=>(rt==="ALL"||p.tournee===rt)&&(typ==="ALL"||p.type===typ));shown.forEach(p=>{{const typeColor=p.type==="Casier / Locker"?"#2563eb":"#f97316";const html=`<div style="background:${{routeColor(p.tournee)}};color:#fff;border:3px solid ${{typeColor}};width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;box-shadow:0 1px 5px #0005">${{p.ordre}}</div>`;L.marker([p.lat,p.lon],{{icon:L.divIcon({{className:"",html}})}}).addTo(markersLayer).bindPopup(`<b>${{p.nom}}</b><br><b>${{p.tournee}}</b> — ${{p.type}}<br>Code : ${{p.code}}<br>${{p.adresse}}<br>${{p.ville}}<br>Service : ${{p.service}} min<br><a target="_blank" href="${{p.maps}}">Ouvrir le point</a>`);}});if(rt!=="ALL"&&shown.length)map.fitBounds(shown.map(p=>[p.lat,p.lon]),{{padding:[25,25]}});renderRouteList();}}
function renderRouteList(){{document.getElementById("kpiPoints").textContent=POINTS.length;document.getElementById("kpiRoutes").textContent=routeIds().length;document.getElementById("kpiLockers").textContent=POINTS.filter(p=>p.type==="Casier / Locker").length;document.getElementById("kpiRelais").textContent=POINTS.filter(p=>p.type==="Point relais").length;const box=document.getElementById("routeList");box.innerHTML="<b>Résumé des tournées</b>";const totals=[];routeIds().forEach(r=>{{const pts=POINTS.filter(p=>p.tournee===r).sort((a,b)=>a.ordre-b.ordre);const lockers=pts.filter(p=>p.type==="Casier / Locker").length;const relais=pts.length-lockers;const total=proxyTotal(pts);totals.push(total);const over=pts.length&&total>MAX_MIN;const url=googleLink(pts);const div=document.createElement("div");div.className="route-row";div.innerHTML=`<span class="swatch" style="background:${{routeColor(r)}}"></span><div><b>${{r}}</b> — ${{routeDisplayName(r)}} ${{over?'<span class="badge bad">> 7h</span>':'<span class="badge">≤ 7h</span>'}}<br><span class="small">${{pts.length}} pts · ${{lockers}} lockers · ${{relais}} relais · service ${{Math.round(serviceSum(pts))}} min</span><br><span class="metric">Prévisionnel ${{Math.round(total)}} min</span> <span class="metric">${{proxyKm(pts).toFixed(1)}} km proxy</span><br>${{url?`<a target="_blank" href="${{url}}">Google Maps complet sans péage</a>`:""}}</div>`;box.appendChild(div);}});document.getElementById("balanceInfo").innerHTML=`Tournées : <b>${{routeIds().length}}</b>. Max prévisionnel : <b>${{Math.round(Math.max(...totals))}} min</b>. Moyenne : <b>${{Math.round(totals.reduce((a,b)=>a+b,0)/totals.length)}} min</b>.`;renderLegend();}}
function renderLegend(){{document.getElementById("mapLegend").innerHTML="<b>Légende des tournées</b>"+routeIds().map(r=>`<div class="map-legend-row"><span class="map-legend-swatch" style="background:${{routeColor(r)}}"></span><span><b style="display:inline">${{r}}</b> — ${{routeDisplayName(r).replace(/^T\\d+\\s*-\\s*/,'')}} (${{POINTS.filter(p=>p.tournee===r).length}})</span></div>`).join("")+'<div class="small" style="margin-top:6px">Contour bleu = locker · contour orange = point relais</div>';}}
function renumberRoute(r){{POINTS.filter(p=>p.tournee===r).sort((a,b)=>a.ordre-b.ordre).forEach((p,i)=>p.ordre=i+1);}}
function moveSelectedPointManual(){{const code=document.getElementById("manualPoint").value,target=document.getElementById("manualTargetRoute").value,p=POINTS.find(x=>String(x.code)===String(code));if(!code||!p||!target)return;const source=p.tournee;p.tournee=target;p.nom_tournee=routeDisplayName(target);renumberRoute(source);renumberRoute(target);document.getElementById("manualInfo").textContent=`Point ${{code}} déplacé de ${{source}} vers ${{target}}.`;renderSelectors();renderMarkers();}}
function nextRouteId(){{let maxId=0;routeIds().forEach(r=>{{const m=String(r).match(/T(\\d+)/);if(m)maxId=Math.max(maxId,parseInt(m[1],10));}});return "T"+String(maxId+1).padStart(2,"0");}}
function createCustomRoute(){{const input=document.getElementById("newRouteName");const id=nextRouteId();const name=(input?.value||"").trim()||id+" - Nouvelle tournée";EXTRA_ROUTES.push({{id,name}});ROUTE_NAMES[id]=name;if(input)input.value="";document.getElementById("manualInfo").textContent=`Tournée ${{id}} créée.`;renderSelectors();renderMarkers();}}
function renameSelectedRoute(){{const id=document.getElementById("renameRouteSelect").value;const name=(document.getElementById("renameRouteName").value||"").trim();if(!id||!name)return;ROUTE_NAMES[id]=name;POINTS.filter(p=>p.tournee===id).forEach(p=>p.nom_tournee=name);let extra=EXTRA_ROUTES.find(x=>x.id===id);if(extra)extra.name=name;document.getElementById("manualInfo").textContent=`Tournée ${{id}} renommée.`;renderSelectors();renderMarkers();}}
function saveManualScenario(){{localStorage.setItem(STORAGE_KEY,JSON.stringify({{points:POINTS,extra:EXTRA_ROUTES,names:ROUTE_NAMES}}));document.getElementById("manualInfo").textContent="Scénario sauvegardé."}}
function resetManualScenario(){{localStorage.removeItem(STORAGE_KEY);POINTS=JSON.parse(JSON.stringify(INITIAL_POINTS));EXTRA_ROUTES=[];ROUTE_NAMES={{}};renderSelectors();renderMarkers();document.getElementById("manualInfo").textContent="Réinitialisé."}}
function exportCurrentScenarioCSV(){{const header=["tournee","ordre","code","nom","adresse","ville","type","lat","lon","service"];const rows=[header.join(";")].concat(POINTS.slice().sort((a,b)=>a.tournee.localeCompare(b.tournee)||a.ordre-b.ordre).map(p=>header.map(h=>String(p[h]??"").replaceAll(";",",")).join(";")));const blob=new Blob([rows.join("\\n")],{{type:"text/csv;charset=utf-8"}});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="scenario_AIX3.csv";a.click();}}
renderSelectors();renderMarkers();if(POINTS.length)map.fitBounds(POINTS.map(p=>[p.lat,p.lon]),{{padding:[25,25]}});
</script></body></html>
"""


# ============================================================
# STREAMLIT APP
# ============================================================

st.set_page_config(page_title="AIX3 équilibré", layout="wide")
st.title("AIX3 — optimisation équilibrée + carte modifiable")

st.caption("Version basée sur la cartographie que tu as envoyée : actualisation automatique du dernier batch AIX3, création/renommage/déplacement de tournées + Google Maps sans péage.")

with st.sidebar:
    st.header("Configuration")
    token = st.text_input("Token VintedGo", value=TOKEN_ENV, type="password")
    batch_id = st.text_input("Batch ID", value=BATCH_ENV)
    sorting_center_id = st.text_input("Sorting center ID", value=SORTING_CENTER_ID_ENV)
    nb_tournees = st.number_input("Nombre de tournées", min_value=2, max_value=12, value=6)
    st.caption("Règles : locker 15 min, point relais 5 min, max cible 7h.")

col1, col2, col3, col4 = st.columns(4)
auto_sync = col1.button("Actualiser dernière tournée AIX3", type="primary")
sync = col2.button("Synchroniser Batch ID")
load = col3.button("Charger dernier fichier local")
opt_btn = col4.button("Optimiser équilibré")

if "df" not in st.session_state:
    st.session_state.df = None
if "opt" not in st.session_state:
    st.session_state.opt = None
if "summary" not in st.session_state:
    st.session_state.summary = None
if "batches_df" not in st.session_state:
    st.session_state.batches_df = None
if "detected_batch_id" not in st.session_state:
    st.session_state.detected_batch_id = None

if auto_sync:
    try:
        with st.spinner("Détection du dernier batch AIX3..."):
            latest_id, batches_df = detect_latest_batch(token, sorting_center_id=sorting_center_id)
            st.session_state.detected_batch_id = latest_id
            st.session_state.batches_df = batches_df

        st.success(f"Dernier batch détecté : {latest_id}")

        with st.spinner(f"Scraping VintedGo du batch {latest_id}..."):
            st.session_state.df = fetch_vinted(latest_id, token)
            st.session_state.opt = None

        st.success(f"{len(st.session_state.df)} points récupérés pour le batch {latest_id}.")

    except Exception as e:
        st.error(str(e))

if sync:
    try:
        with st.spinner("Scraping VintedGo..."):
            st.session_state.df = fetch_vinted(batch_id, token)
            st.session_state.opt = None
        st.success(f"{len(st.session_state.df)} points récupérés.")
    except Exception as e:
        st.error(str(e))

if load:
    p = DATA_DIR / "points_vinted_latest.csv"
    if p.exists():
        st.session_state.df = pd.read_csv(p)
        st.session_state.opt = None
        st.success("Dernier fichier local chargé.")
    else:
        st.error("Aucun fichier local trouvé.")

if st.session_state.batches_df is not None:
    st.subheader("Derniers batchs détectés")
    st.dataframe(st.session_state.batches_df, use_container_width=True, height=180)

df = st.session_state.df

if df is not None and not df.empty:
    st.subheader("Points récupérés")
    a, b, c, d = st.columns(4)
    a.metric("Points", len(df))
    b.metric("Lockers", int((df["type"] == "Casier / Locker").sum()))
    c.metric("Relais", int((df["type"] == "Point relais").sum()))
    d.metric("Centre", str(df["sorting_center_code"].iloc[0]) if "sorting_center_code" in df else "-")

    st.dataframe(df, use_container_width=True, height=230)

    if opt_btn:
        with st.spinner("Optimisation équilibrée rapide..."):
            opt, summary = optimise_balanced(df, int(nb_tournees))
            st.session_state.opt = opt
            st.session_state.summary = summary
        st.success("Optimisation terminée.")

if st.session_state.opt is not None and not st.session_state.opt.empty:
    opt = st.session_state.opt
    summary = st.session_state.summary

    st.subheader("Résumé des tournées")
    st.dataframe(summary, use_container_width=True)

    html = build_map_html(opt)
    (DATA_DIR / "carte_AIX3_latest.html").write_text(html, encoding="utf-8")

    st.download_button("Télécharger la carte HTML", html, file_name="carte_AIX3_modifiable.html", mime="text/html")
    st.download_button("Télécharger CSV optimisé", opt.to_csv(index=False, encoding="utf-8-sig"), file_name="points_optimises_AIX3.csv", mime="text/csv")

    components.html(html, height=720, scrolling=False)
else:
    st.info("Synchronise VintedGo, puis clique sur Optimiser équilibré.")

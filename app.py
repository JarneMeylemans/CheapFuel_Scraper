"""Tankroute: goedkoopste tankstation langs een route.

Route: OSRM (met alternatieven + tussenpunten) | Adressen: Nominatim
Stations/prijzen: Carbu.com (PRICE_SOURCE=carbu), demo, of eigen JSON-feed
Start:  uvicorn app:app --port 8099
"""
import asyncio, hashlib, json, math, os, re, time
from contextlib import asynccontextmanager
import httpx
import carbu
import fuel as fuel_tracker
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

UA = {"User-Agent": "tankroute/0.2 (persoonlijk gebruik)"}
FUELS = {"diesel", "e10", "e5", "lpg"}  # e10 = super 95, e5 = super 98
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "carbu")  # "carbu", "demo" of URL naar JSON
CACHE_TTL = 1800
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Tankroute")
_cache: dict = {}
app.include_router(fuel_tracker.router)


def cached(key):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


def store(key, val):
    _cache[key] = (time.time(), val)
    return val


@asynccontextmanager
async def client():
    try:
        async with httpx.AsyncClient(headers=UA, timeout=20) as c:
            yield c
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"{e.request.url.host} gaf fout {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Verbinding mislukt: {type(e).__name__} {e}")


# ---------- geometrie ----------
def xy(lat, lon, lat0):
    r = 6371.0088
    return math.radians(lon) * math.cos(math.radians(lat0)) * r, math.radians(lat) * r


def dist_to_line_km(lat, lon, line_xy, lat0):
    px, py = xy(lat, lon, lat0)
    best = 1e9
    for (ax, ay), (bx, by) in zip(line_xy, line_xy[1:]):
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0 if l2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / l2))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def haversine_m(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


# ---------- adressen ----------
LL = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_ll(s):
    m = LL.match(s)
    return (float(m.group(1)), float(m.group(2))) if m else None


async def geocode_many(c, q, limit=5):
    r = await c.get("https://nominatim.openstreetmap.org/search",
                    params={"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 0,
                            "countrycodes": "be,nl,fr,lu,de"})
    r.raise_for_status()
    return [{"label": x["display_name"], "lat": float(x["lat"]), "lon": float(x["lon"])} for x in r.json()]


async def geocode(c, q):
    if (ll := parse_ll(q)):
        return ll
    key = ("geo", q.lower())
    if (v := cached(key)) is not None:
        return v
    res = await geocode_many(c, q, 1)
    if not res:
        raise HTTPException(404, f"Adres niet gevonden: {q}")
    return store(key, (res[0]["lat"], res[0]["lon"]))


# ---------- routes ----------
def decimate(line, max_pts=1500):
    step = max(1, len(line) // max_pts)
    return line[::step] + ([line[-1]] if (len(line) - 1) % step else [])


async def plan_routes(c, van, naar, via=""):
    a, b = await asyncio.gather(geocode(c, van), geocode(c, naar))
    vias = [ll for v in via.split(";") if v.strip() and (ll := parse_ll(v))]
    pts = [a] + vias + [b]
    coords = ";".join(f"{lo},{la}" for la, lo in pts)
    r = await c.get(f"https://router.project-osrm.org/route/v1/driving/{coords}",
                    params={"overview": "full", "geometries": "geojson",
                            "alternatives": "false" if vias else "true"})
    r.raise_for_status()
    j = r.json()
    if not j.get("routes"):
        raise HTTPException(404, "Geen route gevonden")
    routes = [{"km": round(x["distance"] / 1000, 1), "min": round(x["duration"] / 60),
               "line": decimate([(la, lo) for lo, la in x["geometry"]["coordinates"]])} for x in j["routes"]]
    return {"a": a, "b": b, "vias": vias, "routes": routes}


# ---------- stations + prijzen ----------
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]


async def get_stations(c, line, radius_m):
    pad = radius_m / 1000 / 111.0 + 0.005
    south, north = min(p[0] for p in line) - pad, max(p[0] for p in line) + pad
    west, east = min(p[1] for p in line) - pad / 0.62, max(p[1] for p in line) + pad / 0.62
    q = f'[out:json][timeout:25];nwr["amenity"="fuel"]({south:.4f},{west:.4f},{north:.4f},{east:.4f});out center tags;'
    elements, last = None, "onbekend"
    for url in OVERPASS:
        try:
            r = await c.post(url, data={"data": q}, timeout=40)
            r.raise_for_status()
            elements = r.json()["elements"]
            break
        except (httpx.HTTPError, ValueError) as e:
            last = f"{url.split('/')[2]}: {type(e).__name__} {e}"
    if elements is None:
        raise HTTPException(502, f"Alle Overpass-servers faalden. Laatste fout: {last}")
    out = []
    for e in elements:
        la = e.get("lat") or e.get("center", {}).get("lat")
        lo = e.get("lon") or e.get("center", {}).get("lon")
        if la is None:
            continue
        t = e.get("tags", {})
        out.append({"id": f'{e["type"][0]}{e["id"]}', "lat": la, "lon": lo,
                    "name": t.get("name") or t.get("brand") or "Tankstation", "brand": t.get("brand", ""),
                    "address": " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber"),
                                                      t.get("addr:city")]))})
    return out


async def get_prices(c, stations, fuel):
    """Voor demo en eigen JSON-feed: {station_id: (prijs, bijgewerkt)}."""
    if PRICE_SOURCE == "demo":
        base = {"diesel": 1.62, "e10": 1.66, "e5": 1.78, "lpg": 0.88}[fuel]
        return {s["id"]: (round(base + (int(hashlib.md5(s["id"].encode()).hexdigest(), 16) % 25) / 100 - 0.08, 3), None)
                for s in stations}
    r = await c.get(PRICE_SOURCE, timeout=20)
    r.raise_for_status()
    feed = [f for f in r.json() if fuel in f.get("prices", {})]
    res = {}
    for s in stations:
        best = min(feed, key=lambda f: haversine_m(s["lat"], s["lon"], f["lat"], f["lon"]), default=None)
        if best and haversine_m(s["lat"], s["lon"], best["lat"], best["lon"]) < 150:
            res[s["id"]] = (best["prices"][fuel], best.get("updated"))
    return res


def nearest_idx(line, p):
    return min(range(len(line)), key=lambda i: (line[i][0] - p[0]) ** 2 + (line[i][1] - p[1]) ** 2)


async def exact_detour(c, line, vias, st, base_km, base_min):
    """Echte extra afstand/tijd: route A -> (tussenpunten) -> station -> B minus de gekozen route."""
    si = nearest_idx(line, (st["lat"], st["lon"]))
    before = [v for v in vias if nearest_idx(line, v) <= si]
    after = [v for v in vias if nearest_idx(line, v) > si]
    pts = [line[0]] + before + [(st["lat"], st["lon"])] + after + [line[-1]]
    coords = ";".join(f"{lo},{la}" for la, lo in pts)
    try:
        r = await c.get(f"https://router.project-osrm.org/route/v1/driving/{coords}", params={"overview": "false"})
        r.raise_for_status()
        rt = r.json()["routes"][0]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    return (max(0.0, rt["distance"] / 1000 - base_km),
            max(0.0, rt["duration"] / 60 - base_min) if base_min is not None else None)


def price_row(r, detour_km, liters, consumption, exact, detour_min=None):
    """Kost = tanken + brandstof die je extra verbruikt voor de omweg (aan dezelfde literprijs)."""
    fuel_cost = r["price"] * liters
    detour_cost = detour_km * consumption / 100 * r["price"]
    r.update(detour_km=round(detour_km, 1), detour_min=None if detour_min is None else round(detour_min),
             detour_exact=exact, fuel_cost=round(fuel_cost, 2), detour_cost=round(detour_cost, 2),
             total_cost=round(fuel_cost + detour_cost, 2))


REFINE = 6  # zoveel beste kandidaten rekenen we exact na via het wegennet


async def compute(line, fuel, max_detour, liters, consumption, vias=None, base_km=None, base_min=None):
    """Rangschik stations langs één gekozen route op totaalkost (tanken + omweg)."""
    if fuel not in FUELS:
        raise HTTPException(400, f"fuel moet een van {sorted(FUELS)} zijn")
    if len(line) < 2:
        raise HTTPException(400, "Route is leeg")
    if not (0.2 <= max_detour <= 10 and 1 <= liters <= 200 and 1 <= consumption <= 30):
        raise HTTPException(400, "Ongeldige waarde: omweg 0,2-10 km, liters 1-200, verbruik 1-30 l/100 km")
    vias = [tuple(v) for v in (vias or [])]
    key = ("c", hashlib.md5(json.dumps([line, vias]).encode()).hexdigest(), fuel, max_detour, liters, consumption,
           base_km, base_min, PRICE_SOURCE)
    if (v := cached(key)) is not None:
        return v
    warnings = []
    async with client() as c:
        if PRICE_SOURCE == "carbu":
            stations, warnings = await carbu.carbu_stations(c, line, fuel)
            prices = {s["id"]: (s["price"], s["updated"]) for s in stations}
        else:
            stations = await get_stations(c, line, int(max_detour * 1000))
            prices = await get_prices(c, stations, fuel)
    fuel_tracker.remember(stations)  # voor het herkennen van tankbeurten
    lat0 = sum(p[0] for p in line) / len(line)
    line_xy = [xy(la, lo, lat0) for la, lo in line]
    rows = []
    for s in stations:
        if s["id"] not in prices:
            continue
        price, updated = prices[s["id"]]
        d = dist_to_line_km(s["lat"], s["lon"], line_xy, lat0)
        if d > max_detour:
            continue
        r = {**s, "price": price, "updated": updated, "distance_to_route_m": round(d * 1000)}
        price_row(r, 2 * d * 1.3, liters, consumption, False)  # eerste schatting: rechte lijn x2 x1,3
        rows.append(r)
    rows.sort(key=lambda r: r["total_cost"])
    if base_km is not None and rows:  # verfijn de beste kandidaten met de echte route
        async with client() as c:
            sem = asyncio.Semaphore(3)

            async def one(r):
                async with sem:
                    return await exact_detour(c, line, vias, r, base_km, base_min)
            res = await asyncio.gather(*[one(r) for r in rows[:REFINE]])
        failed = 0
        for r, x in zip(rows[:REFINE], res):
            if x:
                price_row(r, x[0], liters, consumption, True, x[1])
            else:
                failed += 1
        if failed:
            warnings.append(f"Omweg van {failed} station(s) geschat (routedienst gaf geen antwoord)")
        rows.sort(key=lambda r: r["total_cost"])
    for r in rows:
        r["extra_vs_best"] = round(r["total_cost"] - rows[0]["total_cost"], 2)
    return store(key, {"demo": PRICE_SOURCE == "demo", "fuel": fuel, "warnings": warnings,
                       "stations": rows, "best": rows[0] if rows else None})


# ---------- opgeslagen routes ----------
SAVED_FILE = os.path.join(os.getenv("DATA_DIR", HERE), "saved_routes.json")


def read_saved():
    try:
        with open(SAVED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_saved(d):
    tmp = SAVED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SAVED_FILE)


class Pt(BaseModel):
    lat: float
    lon: float
    label: str = ""


class SavedRoute(BaseModel):
    name: str
    a: Pt
    b: Pt
    vias: list[list[float]] = []
    km: float | None = None
    fuel: str = "diesel"
    max_detour: float = 2.0
    liters: float = 40
    consumption: float = 6.0


@app.get("/api/saved")
async def api_saved_list():
    return sorted(read_saved().values(), key=lambda x: x["name"].lower())


@app.post("/api/saved")
async def api_saved_save(r: SavedRoute):
    name = r.name.strip()
    if not 1 <= len(name) <= 60:
        raise HTTPException(400, "Naam moet 1 tot 60 tekens zijn")
    if r.fuel not in FUELS:
        raise HTTPException(400, "Onbekende brandstof")
    d = read_saved()
    d[name] = {**r.model_dump(), "name": name}
    write_saved(d)
    return {"saved": name}


@app.delete("/api/saved")
async def api_saved_delete(name: str):
    d = read_saved()
    if name not in d:
        raise HTTPException(404, "Route niet gevonden")
    del d[name]
    write_saved(d)
    return {"deleted": name}


# ---------- API ----------
@app.get("/api/geocode")
async def api_geocode(q: str = Query(..., min_length=3)):
    key = ("gm", q.lower())
    if (v := cached(key)) is not None:
        return v
    async with client() as c:
        return store(key, await geocode_many(c, q))


@app.get("/api/reverse")
async def api_reverse(lat: float, lon: float):
    async with client() as c:
        r = await c.get("https://nominatim.openstreetmap.org/reverse",
                        params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18})
        r.raise_for_status()
        return {"label": r.json().get("display_name", f"{lat:.4f}, {lon:.4f}")}


@app.get("/api/routes")
async def api_routes(van: str, naar: str, via: str = ""):
    async with client() as c:
        return await plan_routes(c, van, naar, via)


class StationsReq(BaseModel):
    line: list[list[float]]
    fuel: str = "diesel"
    max_detour: float = 2.0
    liters: float = 40
    consumption: float = 6.0
    vias: list[list[float]] = []
    base_km: float | None = None
    base_min: float | None = None


@app.post("/api/stations")
async def api_stations(req: StationsReq):
    return await compute([tuple(p) for p in req.line], req.fuel, req.max_detour, req.liters, req.consumption,
                         req.vias, req.base_km, req.base_min)


async def _search(van, naar, via, route, fuel, max_detour, liters, consumption, saved):
    km = None
    if saved:
        sv = read_saved().get(saved)
        if not sv:
            raise HTTPException(404, f"Opgeslagen route '{saved}' bestaat niet")
        van, naar = f"{sv['a']['lat']},{sv['a']['lon']}", f"{sv['b']['lat']},{sv['b']['lon']}"
        via = ";".join(f"{v[0]},{v[1]}" for v in sv["vias"])
        km = sv.get("km")
        fuel = fuel or sv["fuel"]
        max_detour = max_detour if max_detour is not None else sv["max_detour"]
        liters = liters if liters is not None else sv["liters"]
        consumption = consumption if consumption is not None else sv["consumption"]
    if not van or not naar:
        raise HTTPException(400, "Geef van en naar op, of een opgeslagen route (saved=naam)")
    fuel, max_detour = fuel or "diesel", 2.0 if max_detour is None else max_detour
    liters, consumption = 40 if liters is None else liters, 6.0 if consumption is None else consumption
    async with client() as c:
        plan = await plan_routes(c, van, naar, via)
    routes = plan["routes"]
    if km is not None and not plan["vias"]:  # alternatief herkennen aan de afstand
        rt = min(routes, key=lambda r: abs(r["km"] - km))
    else:
        rt = routes[min(route, len(routes) - 1)]
    res = await compute(rt["line"], fuel, max_detour, liters, consumption, plan["vias"], rt["km"], rt["min"])
    return {**res, "route_km": rt["km"], "route_min": rt["min"], "route": rt["line"]}


@app.get("/api/search")
async def api_search(van: str = "", naar: str = "", via: str = "", route: int = 0, saved: str = "",
                     fuel: str | None = None, max_detour: float | None = None, liters: float | None = None,
                     consumption: float | None = None):
    return await _search(van, naar, via, route, fuel, max_detour, liters, consumption, saved)


@app.get("/api/best")
async def api_best(van: str = "", naar: str = "", via: str = "", route: int = 0, saved: str = "",
                   fuel: str | None = None, max_detour: float | None = None, liters: float | None = None,
                   consumption: float | None = None):
    """Compact antwoord voor Home Assistant. Gebruik saved=<naam> voor een opgeslagen route."""
    r = await _search(van, naar, via, route, fuel, max_detour, liters, consumption, saved)
    b = r["best"]
    if not b:
        return {"found": False, "demo": r["demo"]}
    return {"found": True, "demo": r["demo"], "name": b["name"], "address": b["address"], "price": b["price"],
            "distance_to_route_m": b["distance_to_route_m"], "detour_km": b["detour_km"],
            "detour_cost": b["detour_cost"], "fuel_cost": b["fuel_cost"], "total_cost": b["total_cost"], "lat": b["lat"], "lon": b["lon"], "updated": b["updated"],
            "alternatives": [{"name": s["name"], "price": s["price"]} for s in r["stations"][1:4]]}


@app.get("/api/debug/carbu")
async def debug_carbu(postcode: str = "2800", fuel: str = "diesel"):
    async with client() as c:
        return await carbu.debug(c, postcode, fuel)


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))

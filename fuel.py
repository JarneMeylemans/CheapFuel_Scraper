"""Brandstofniveau automatisch schatten, zonder sensor in de auto en zonder handmatige invoer.

- Kilometers: afstand tussen locatiepings van je telefoon, alleen zolang je in een voertuig zit.
- Tankbeurten: je stopt 2-30 min bij een tankstation (en de schatting stond onder 75%) -> tank weer vol.
"""
import asyncio, json, math, os, time
import httpx
from fastapi import APIRouter
from pydantic import BaseModel

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(DATA_DIR, "fuel_state.json")
f = lambda k, d: float(os.getenv(k, d))
CAPACITY0, CONSUMPTION0 = f("TANK_CAPACITY", 50), f("CONSUMPTION", 6.0)
START_FILL = f("START_FILL", 0.7)      # aanname bij de eerste start; corrigeert zich bij de eerste tankbeurt
ROAD_FACTOR = f("ROAD_FACTOR", 1.15)   # rechte lijn tussen pings is korter dan de echte weg
MIN_DWELL, MAX_DWELL = f("MIN_DWELL", 120), f("MAX_DWELL", 1800)  # seconden stilstaan bij het station
STATION_RADIUS_M = f("STATION_RADIUS_M", 70)
REFUEL_BELOW = f("REFUEL_BELOW", 0.75)  # boven dit niveau tellen we een stop niet als tankbeurt
MAX_GAP_S, MAX_SEG_KM = 2700, 80        # grenzen voor één ritsegment tussen twee pings
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

router = APIRouter()
_lock = asyncio.Lock()


def new_state():
    return {"capacity": CAPACITY0, "consumption": CONSUMPTION0, "liters": round(CAPACITY0 * START_FILL, 1),
            "km_since_refuel": 0.0, "km_total": 0.0, "last": None, "parked": None,
            "last_refuel": None, "history": [], "stations": []}


def load():
    s = new_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            s.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return s


def save():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


_state = load()


def hav_km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def remember(stations):
    """Onthoud stations die Tankroute al zag; zo hoeven we zelden Overpass te vragen."""
    known = {s["id"]: s for s in _state["stations"]}
    for s in stations:
        known[s["id"]] = {"id": s["id"], "lat": s["lat"], "lon": s["lon"], "name": s.get("name", "")}
    _state["stations"] = list(known.values())[-3000:]
    save()


async def near_station(lat, lon):
    for s in _state["stations"]:
        if hav_km(lat, lon, s["lat"], s["lon"]) * 1000 <= STATION_RADIUS_M:
            return s["name"] or "Tankstation"
    q = f'[out:json][timeout:15];nwr["amenity"="fuel"](around:{STATION_RADIUS_M:.0f},{lat},{lon});out tags center;'
    for url in OVERPASS:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, data={"data": q})
                r.raise_for_status()
                els = r.json()["elements"]
            return (els[0].get("tags", {}).get("name") or "Tankstation") if els else None
        except (httpx.HTTPError, ValueError, KeyError):
            continue
    return None


async def process(lat, lon, veh, ts):
    async with _lock:
        s, ev, prev = _state, None, _state["last"]
        if prev and ts < prev["ts"]:
            return {"ignored": "ping in verkeerde volgorde"}
        if prev:
            if prev["veh"]:
                d = hav_km(prev["lat"], prev["lon"], lat, lon)
                if ts - prev["ts"] <= MAX_GAP_S and d <= MAX_SEG_KM:
                    km = d * ROAD_FACTOR
                    s["km_since_refuel"] += km
                    s["km_total"] += km
                    s["liters"] = max(0.0, s["liters"] - km * s["consumption"] / 100)
                if not veh:  # net gestopt of uitgestapt
                    s["parked"] = {"lat": lat, "lon": lon, "ts": ts}
        if veh and s["parked"]:  # je rijdt weer weg: was de stop een tankbeurt?
            pk, s["parked"] = s["parked"], None
            dwell = ts - pk["ts"]
            if MIN_DWELL <= dwell <= MAX_DWELL and s["liters"] < REFUEL_BELOW * s["capacity"]:
                name = await near_station(pk["lat"], pk["lon"])
                if name:
                    ev = {"ts": ts, "station": name, "lat": pk["lat"], "lon": pk["lon"],
                          "liters_before": round(s["liters"], 1), "km_since_previous": round(s["km_since_refuel"], 1),
                          "dwell_min": round(dwell / 60, 1)}
                    s["liters"], s["km_since_refuel"] = s["capacity"], 0.0
                    s["last_refuel"] = ev
                    s["history"] = (s["history"] + [ev])[-30:]
        s["last"] = {"lat": lat, "lon": lon, "ts": ts, "veh": veh}
        save()
        return {"refuel_detected": ev}


def status():
    s = _state
    cap, cons = s["capacity"], s["consumption"]
    age = None if not s["last"] else round((time.time() - s["last"]["ts"]) / 60)
    return {"liters": round(s["liters"], 1), "capacity": cap, "percent": round(s["liters"] / cap * 100),
            "range_km": round(s["liters"] / cons * 100), "consumption": cons,
            "km_since_refuel": round(s["km_since_refuel"], 1), "km_total_tracked": round(s["km_total"]),
            "last_refuel": s["last_refuel"], "last_ping_min_ago": age,
            "tracking_ok": age is not None and age < 24 * 60, "estimate": True}


class Ping(BaseModel):
    lat: float
    lon: float
    in_vehicle: bool = False
    ts: float | None = None


class Config(BaseModel):
    capacity: float | None = None
    consumption: float | None = None
    liters: float | None = None  # optionele correctie als de schatting ooit ver afwijkt


@router.post("/api/fuel/ping")
async def api_ping(p: Ping):
    return await process(p.lat, p.lon, p.in_vehicle, p.ts or time.time())


@router.get("/api/fuel")
async def api_fuel():
    return status()


@router.post("/api/fuel/config")
async def api_config(c: Config):
    async with _lock:
        for k in ("capacity", "consumption", "liters"):
            if getattr(c, k) is not None:
                _state[k] = float(getattr(c, k))
        _state["liters"] = min(_state["liters"], _state["capacity"])
        save()
    return status()

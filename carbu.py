"""Carbu.com-scraper: stations + actuele prijzen langs een route (België).

Werkwijze: punt elke ~8 km op de route -> postcode (Nominatim) -> Carbu-gebied-id
-> lijstpagina van Carbu (per brandstof) -> stations met prijs, update-datum en GPS.
"""
import asyncio, html as htmllib, json, os, re, time
from urllib.parse import quote
import httpx
from fastapi import HTTPException

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Accept-Language": "nl-BE,nl;q=0.9"}
NOMINATIM_UA = {"User-Agent": "tankroute/0.1 (persoonlijk gebruik)"}
FUEL_LABEL = {"diesel": "Diesel (B7)", "e10": "Super 95 (E10)", "e5": "Super 98 (E5)", "lpg": "LPG"}
CACHE_FILE = os.path.join(os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__))), "carbu_cache.json")
PAGE_TTL = 3600  # Carbu-pagina's maximaal 1x per uur per gebied/brandstof ophalen
STEP_KM = 8

# gekend gebied-id (uit een echte Carbu-URL); de rest wordt automatisch opgezocht
_cache = {"areas": {"2800": {"town": "Mechelen", "area": "BE_a_424"}}, "rev": {}}
_pages: dict = {}
try:
    with open(CACHE_FILE, encoding="utf-8") as f:
        _loaded = json.load(f)
        _cache["areas"].update(_loaded.get("areas", {}))
        _cache["rev"].update(_loaded.get("rev", {}))
except (OSError, ValueError):
    pass


def _save():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ---------- parsing ----------
GPS = re.compile(r"daddr=(-?\d+\.\d+),(-?\d+\.\d+)")
STATION = re.compile(r"/station/([^/\"'?#]+)/([^/\"'?#]+)/(\d{4})/(\d+)")
PRICE = re.compile(r"(\d[.,]\d{3})\s*(?:&euro;|€)\s*/\s*L", re.I)
DATE = re.compile(r"(\d{2})/(\d{2})/(\d{2})")
ANCHOR = re.compile(r"<a[^>]+href=\"[^\"]*?/station/[^\"]+\"[^>]*>(.*?)</a>", re.S | re.I)
BOLD = re.compile(r"<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>", re.S | re.I)


SCRIPTS = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)


def _text(s):
    s = SCRIPTS.sub(" ", s)  # inline JavaScript/CSS in de link mag niet in naam of adres belanden
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_list(page):
    """Haal per station id, naam, adres, prijs, datum en coördinaten uit een Carbu-lijstpagina."""
    out, prev = {}, 0
    for g in GPS.finditer(page):
        chunk, prev = page[prev:g.start()], g.end()
        ids = STATION.findall(chunk)
        if not ids:
            continue
        brand, town, pc, sid = ids[-1]
        m = PRICE.search(chunk)
        if not m:
            continue  # station zonder prijs voor deze brandstof
        name = address = ""
        for a in ANCHOR.finditer(chunk):
            t = _text(a.group(1))
            if not t:
                continue
            b = BOLD.search(a.group(1))
            name = _text(b.group(1)) if b else t
            address = t[len(name):].strip() if t.startswith(name) else ""
            if len(address) > 100 or "function" in address or "$(" in address:
                address = ""
            break
        d = DATE.search(chunk, m.end())
        out[sid] = {"id": f"c{sid}", "lat": float(g.group(1)), "lon": float(g.group(2)),
                    "name": name or brand.replace("-", " ").title(), "brand": brand, "address": address,
                    "price": float(m.group(1).replace(",", ".")),
                    "updated": f"20{d.group(3)}-{d.group(2)}-{d.group(1)}" if d else None}
    return list(out.values())


# ---------- route -> postcodes ----------
def sample_points(line, step_km=STEP_KM):
    import math
    pts, acc, nxt = [line[0]], 0.0, step_km
    for (a, b), (c, d) in zip(line, line[1:]):
        p = math.pi / 180
        h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
        acc += 12742 * math.asin(math.sqrt(h))
        if acc >= nxt:
            pts.append((c, d))
            nxt += step_km
    pts.append(line[-1])
    return pts


async def reverse(client, lat, lon):
    key = f"{lat:.2f},{lon:.2f}"
    if key in _cache["rev"]:
        return _cache["rev"][key]
    await asyncio.sleep(1.1)  # Nominatim: max 1 vraag per seconde
    r = await client.get("https://nominatim.openstreetmap.org/reverse", headers=NOMINATIM_UA,
                         params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14, "addressdetails": 1})
    r.raise_for_status()
    a = r.json().get("address", {})
    pc = a.get("postcode", "")
    town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""
    res = [pc, town] if re.fullmatch(r"\d{4}", pc) and a.get("country_code") == "be" else None
    _cache["rev"][key] = res
    _save()
    return res


async def resolve_area(client, postcode, town):
    if postcode in _cache["areas"]:
        return _cache["areas"][postcode]
    try:
        r = await client.get(f"https://carbu.com/commonFunctions/getlocation/controller.getlocation_JSON.php"
                             f"?location={postcode}", headers=HEADERS)
        ids = re.findall(r"BE_[a-z]+_\d+", r.text)
    except httpx.HTTPError:
        return None
    if not ids:
        return None
    area = {"town": town, "area": next((i for i in ids if i.startswith("BE_a_")), ids[0])}
    _cache["areas"][postcode] = area
    _save()
    return area


async def fetch_list(client, fuel, postcode, area):
    key = (fuel, postcode, area["area"])
    hit = _pages.get(key)
    if hit and time.time() - hit[0] < PAGE_TTL:
        return hit[1]
    url = (f"https://carbu.com/belgie//index.php/liste-stations-service/{quote(FUEL_LABEL[fuel], safe='()')}/"
           f"{quote(area['town'])}/{postcode}/{area['area']}")
    r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=25)
    r.raise_for_status()
    res = parse_list(r.text)
    _pages[key] = (time.time(), res)
    return res


async def carbu_stations(client, line, fuel):
    areas, warnings, seen = [], [], set()
    for lat, lon in sample_points(line):
        loc = await reverse(client, lat, lon)
        if not loc or loc[0] in seen:
            continue
        seen.add(loc[0])
        area = await resolve_area(client, loc[0], loc[1])
        if area:
            areas.append((loc[0], area))
        else:
            warnings.append(f"Carbu-gebied voor {loc[0]} {loc[1]} niet gevonden")
    if not areas:
        raise HTTPException(502, "Geen Carbu-gebieden gevonden voor deze route. Open /api/debug/carbu?postcode=2800 "
                                 "om te zien wat er misgaat, of zet PRICE_SOURCE=demo.")
    merged = {}
    for pc, area in areas:
        try:
            for s in await fetch_list(client, fuel, pc, area):
                merged[s["id"]] = s
        except httpx.HTTPError as e:
            warnings.append(f"Carbu {pc}: {type(e).__name__}")
        await asyncio.sleep(0.5)
    if not merged and not warnings:
        warnings.append("Carbu gaf pagina's zonder herkenbare prijzen (pagina-opbouw gewijzigd?)")
    return list(merged.values()), warnings


async def debug(client, postcode, fuel):
    area = await resolve_area(client, postcode, "")
    out = {"area": area}
    try:
        r = await client.get(f"https://carbu.com/commonFunctions/getlocation/controller.getlocation_JSON.php"
                             f"?location={postcode}", headers=HEADERS)
        out["lookup_status"], out["lookup_head"] = r.status_code, r.text[:400]
    except httpx.HTTPError as e:
        out["lookup_error"] = repr(e)
    if area:
        town = area["town"] or "x"
        url = (f"https://carbu.com/belgie//index.php/liste-stations-service/{quote(FUEL_LABEL[fuel], safe='()')}/"
               f"{quote(town)}/{postcode}/{area['area']}")
        r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=25)
        st = parse_list(r.text)
        i = r.text.find("daddr=")
        out.update(url=url, status=r.status_code, html_length=len(r.text), parsed=len(st), first=st[:3],
                   html_around_first_gps=r.text[max(0, i - 1200):i + 100] if i >= 0 else None)
    return out

<<<<<<< HEAD
# CheapFuel_Scraper
scapes all fuel stations on your route and selects the cheapest
=======
# Tankroute

Goedkoopste tankstation langs je route, met kaart en een eenvoudig endpoint voor Home Assistant.

## Starten
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8099      # open http://localhost:8099
of met Docker: `docker build -t tankroute . && docker run -d -p 8099:8099 --restart unless-stopped tankroute`

## Prijsbron (belangrijk)
Standaard `PRICE_SOURCE=demo`: stations zijn echt (OpenStreetMap), **prijzen zijn nep**.
Voor echte prijzen zet je `PRICE_SOURCE=https://.../prijzen.json` met dit formaat:

    [{"lat": 51.03, "lon": 4.48, "prices": {"diesel": 1.59, "e10": 1.67}, "updated": "2026-10-05T07:00"}]

Elk station uit die lijst wordt aan het dichtstbijzijnde OSM-station (<150 m) gekoppeld.
Die JSON kan een klein scriptje maken dat Carbu.com uitleest.

## Routes opslaan en gebruiken in Home Assistant
Maak in de kaart je route (adressen, tussenpunten, gekozen alternatief), geef een naam en klik op **Opslaan**.
Routes staan in `saved_routes.json` naast `app.py` (in Docker: mount een map en zet `DATA_DIR=/data`).

## Home Assistant (configuration.yaml)
    rest:
      - resource: "http://TANKROUTE_IP:8099/api/best?saved=Werk%20heen"
        scan_interval: 3600
        timeout: 60
        sensor:
          - name: "Tankstation heenrit"
            value_template: "{{ value_json.name if value_json.found else 'geen' }}"
            json_attributes: [price, distance_to_route_m, detour_km, total_cost, lat, lon, address, alternatives]
          - name: "Diesel prijs heenrit"
            value_template: "{{ value_json.price | default(none) }}"
            unit_of_measurement: "€/l"

Andere brandstof voor dezelfde route: voeg `&fuel=e10` toe. Zonder opgeslagen route kan het ook met
`/api/best?van=<adres of lat,lon>&naar=...&via=lat,lon;lat,lon&fuel=diesel`.
Kaart in het dashboard: iframe-kaart met `url: http://TANKROUTE_IP:8099/`.

## Brandstofniveau automatisch schatten (geen knoppen, geen km invoeren)
Home Assistant stuurt je telefoonlocatie (+ "in voertuig" of niet) naar `POST /api/fuel/ping`.
Tankroute telt zelf de kilometers en herkent een tankbeurt: stoppen (2-30 min) bij een tankstation terwijl de
schatting onder 75% staat = tank weer vol. Resultaat: `GET /api/fuel` (liters, percent, range_km, ...).
Instellingen (eenmalig, via omgevingsvariabelen): `TANK_CAPACITY`, `CONSUMPTION`, optioneel `START_FILL`, `ROAD_FACTOR`.
Optionele correctie: `POST /api/fuel/config {"liters": 30}`.
>>>>>>> b272aef (Initial commit)

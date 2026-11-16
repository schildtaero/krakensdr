# Lokale Karte und Offlinebestand

Die Karten-API arbeitet beim ersten Start im **Offline-Modus**. In diesem Zustand werden keinerlei neue Kachelanfragen an OpenStreetMap gesendet. Ein fehlender Hintergrund bedeutet eine fehlende lokale Kachel und wird mit HTTP 404 sowie `X-Offline-Cache: miss` ausdrücklich kenntlich gemacht. Positionen und Peillinien können unabhängig vom Kartenhintergrund weiter angezeigt werden.

Einbindung im Backend:

```python
from pathlib import Path
from krakendf.maps import create_map_router
app.include_router(create_map_router(Path("data/map-cache")))
```

## API

- `GET /api/map/status`: Offlinezustand, Anzahl lokal gespeicherter Einzelkacheln, MBTiles-Status/-Anzahl, Attribution als reiner Text sowie optionale Ausdehnung und Zoomstufen.
- `POST /api/map/settings` mit `{"offline": false}`: Onlineabruf ausdrücklich einschalten; `true` schaltet ihn wieder aus. Die Einstellung gilt für diesen Host und wird im Cacheverzeichnis gespeichert.
- `GET /api/map/tiles/{z}/{x}/{y}.png`: XYZ-Rasterkachel. Zuerst MBTiles, dann lokaler Einzelkachelcache. Antworten können `image/png` oder `image/jpeg` enthalten. Raster-Koordinaten werden begrenzt und geprüft.
- `PUT /api/map/mbtiles`: MBTiles-Datei als rohe Binärdaten mit `Content-Type: application/octet-stream`, höchstens 512 MiB. Keine Multipartdatei und kein lokaler Dateipfad. Die Datei wird begrenzt gestreamt, als SQLite geprüft und erst nach erfolgreicher Prüfung atomar aktiviert. Eine ungültige Datei ersetzt die vorherige Karte nicht.

Die Dateinamen auf dem Server sind intern festgelegt; HTTP-Clients können keine beliebigen lokalen Dateien auswählen. Unterstützt werden MBTiles mit `tiles` und `metadata`, PNG-/JPEG-Rasterdaten, TMS-Standardzeilen oder ausdrücklich ausgewiesenem XYZ-Schema. Vektor-MBTiles/PBF und komprimierte Vektorkacheln sind nicht unterstützt. Fehlende oder ungültige Quellenangaben der importierten Datei werden sichtbar benannt; Metadaten werden niemals als HTML ausgegeben.

## Bedarfsweiser Onlineabruf

Online wird ausschließlich die gerade angeforderte OSM-Kachel bis Zoom 19 geladen. Es gibt **keinen Gebietsdownload, Hintergrund-Vorabruf oder Bulkcache**. Der Client sendet einen identifizierbaren User-Agent und nutzt den HTTPS-Standardkachelserver. OpenStreetMap-Attribution muss in der Oberfläche sichtbar bleiben. Der Servercache hält heruntergeladene Kacheln mindestens sieben Tage frisch, berücksichtigt längere Cachezeiten und revalidiert danach mit ETag beziehungsweise Last-Modified. Offline bleiben auch ältere lokal vorhandene Kacheln nutzbar. Bei Netzfehlern wird eine vorhandene alte Kachel als `X-Offline-Cache: stale` geliefert.

Browserantworten werden revalidiert (`Cache-Control: no-cache`), damit ein MBTiles-Wechsel sofort sichtbar werden kann. Diese Abfrage trifft zunächst den lokalen Backendcache und erzwingt keinen OSM-Abruf. Der sieben Tage dauernde Upstreamcache bleibt dabei erhalten.

Für einen verlässlich vorbereiteten Offlinebereich eine passende, rechtmäßig verfügbare Raster-MBTiles-Datei importieren. Das bloße Betrachten einzelner Zoomstufen garantiert keine vollständige Offlineabdeckung. MBTiles und Einzelkacheln werden zusammen dargestellt; deren Quellenangaben stehen im Status getrennt zur Verfügung.

## Ausgeführte Prüfung

Automatische Tests prüfen Offlinezugriff ohne Netzaufruf, TMS→XYZ, JPEG, Attributionsbereinigung, atomare Fehlerbehandlung beim Import, Schema-/Größen-/Koordinatenprüfungen, persistente Einstellungen, Cachetreffer, Netzfehler-Fallback und HTTP-Cache-/User-Agent-Verhalten. Die HTTP-Tests verwenden ausschließlich MockTransport; es wurde kein echter OSM-Kachelabruf und kein Bulkdownload ausgeführt.

"""Local-first XYZ tiles and bounded MBTiles import for the demo map.

OSM is accessed only on demand after an explicit online switch. This module
does not prefetch areas, follow user URLs or accept user-provided server paths.
"""
from __future__ import annotations

import asyncio
import contextlib
import email.utils
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, StrictBool

MAX_TILE_BYTES = 2 * 1024 * 1024
MAX_MBTILES_BYTES = 512 * 1024 * 1024
MIN_CACHE_SECONDS = 7 * 86400
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_USER_AGENT = "KrakenDFDemo/0.1 (local demo; on-demand tile cache)"


class MapSettings(BaseModel):
    offline: StrictBool


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _text_attribution(value: str) -> str:
    # Metadata is data, never HTML. Bound length and remove control characters.
    value = html.unescape(value)
    value = re.sub(r"<[^>]*>", "", value)
    value = "".join(c for c in value if c.isprintable() or c.isspace())
    return " ".join(value.split())[:1000]


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    deadline = time.monotonic() + 5
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return connection


def inspect_mbtiles(path: Path) -> dict[str, Any]:
    """Validate schema and tile bounds before atomically activating an upload."""
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\0":
            raise ValueError("Die Datei ist keine SQLite-/MBTiles-Datei")
    try:
        with contextlib.closing(_sqlite_readonly(path)) as db:
            if db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise ValueError("Die MBTiles-Datei ist beschädigt")
            tile_columns = {row[1] for row in db.execute("PRAGMA table_info(tiles)")}
            metadata_columns = {row[1] for row in db.execute("PRAGMA table_info(metadata)")}
            if not {"zoom_level", "tile_column", "tile_row", "tile_data"}.issubset(tile_columns):
                raise ValueError("MBTiles benötigt tiles mit zoom_level, tile_column, tile_row und tile_data")
            if not {"name", "value"}.issubset(metadata_columns):
                raise ValueError("MBTiles benötigt metadata mit name und value")
            metadata = {}
            for name, value in db.execute("SELECT substr(name,1,100), substr(value,1,10000) FROM metadata LIMIT 1000"):
                if isinstance(name, str) and isinstance(value, str):
                    metadata[name[:100]] = value[:10000]
            if metadata.get("format", "png").lower() not in ("png", "jpg", "jpeg"):
                raise ValueError("Unterstützt werden Raster-MBTiles mit PNG oder JPEG, keine Vektorkacheln")
            if metadata.get("scheme", "tms").lower() not in ("tms", "xyz"):
                raise ValueError("Unbekanntes MBTiles-Kachelschema")
            bad = db.execute("""SELECT 1 FROM tiles WHERE
                typeof(zoom_level) != 'integer' OR typeof(tile_column) != 'integer'
                OR typeof(tile_row) != 'integer' OR typeof(tile_data) != 'blob'
                OR zoom_level < 0 OR zoom_level > 24 OR tile_column < 0 OR tile_row < 0
                OR tile_column >= (1 << zoom_level) OR tile_row >= (1 << zoom_level)
                OR length(tile_data) > ? OR length(tile_data) < 3 LIMIT 1""", (MAX_TILE_BYTES,)).fetchone()
            if bad is not None:
                raise ValueError("MBTiles enthält ungültige Koordinaten oder übergroße Kacheln")
            count, min_zoom, max_zoom = db.execute("SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
            if not count:
                raise ValueError("Die MBTiles-Datei enthält keine Kacheln")
            sample = db.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()[0]
            if _image_type(sample) is None:
                raise ValueError("MBTiles enthält keine unterstützten PNG-/JPEG-Bilddaten")
            bounds = None
            try:
                parsed = [float(v) for v in metadata.get("bounds", "").split(",")]
                if (len(parsed) == 4 and all(math.isfinite(v) for v in parsed)
                    and -180 <= parsed[0] < parsed[2] <= 180
                    and -90 <= parsed[1] < parsed[3] <= 90):
                    bounds = parsed
            except ValueError:
                pass
            return {
                "name": _text_attribution(metadata.get("name", "Lokale MBTiles-Karte")),
                "attribution": _text_attribution(metadata.get("attribution", "Lokale MBTiles-Daten; Quellenangabe nicht hinterlegt")),
                "scheme": metadata.get("scheme", "tms").lower(), "tile_count": count,
                "min_zoom": min_zoom, "max_zoom": max_zoom, "bounds": bounds,
            }
    except sqlite3.Error as exc:
        raise ValueError("Ungültige oder zu aufwendige MBTiles-SQLite-Struktur") from exc


class MapCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.tiles = self.directory / "tiles"
        self.mbtiles_path = self.directory / "offline.mbtiles"
        self.settings_path = self.directory / "settings.json"
        self.offline = True
        try:
            saved = json.loads(self.settings_path.read_text())
            if isinstance(saved.get("offline"), bool):
                self.offline = saved["offline"]
        except (OSError, ValueError, AttributeError):
            pass
        self.mbtiles_info = None
        if self.mbtiles_path.is_file():
            try:
                self.mbtiles_info = inspect_mbtiles(self.mbtiles_path)
            except (OSError, ValueError):
                pass
        self._download_lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        info = self.mbtiles_info or {}
        return {
            "offline": self.offline,
            "cache_tile_count": sum(1 for _ in self.tiles.glob("*/*/*.tile")),
            "mbtiles_loaded": self.mbtiles_info is not None,
            "mbtiles_tile_count": info.get("tile_count", 0),
            "mbtiles_name": info.get("name"),
            "attribution": info.get("attribution", OSM_ATTRIBUTION),
            "online_attribution": OSM_ATTRIBUTION,
            "bounds": info.get("bounds"),
            "min_zoom": info.get("min_zoom", 0), "max_zoom": info.get("max_zoom", 19),
        }

    async def set_offline(self, offline: bool) -> dict[str, Any]:
        async with self._settings_lock:
            self.offline = offline
            # Fixed internal filename; callers cannot select a path.
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"offline": offline}), encoding="utf-8")
            os.replace(temporary, self.settings_path)
        return await asyncio.to_thread(self.status)

    def _from_mbtiles(self, z: int, x: int, y: int) -> tuple[bytes, str] | None:
        if self.mbtiles_info is None:
            return None
        row = y if self.mbtiles_info["scheme"] == "xyz" else (1 << z) - 1 - y
        try:
            with contextlib.closing(_sqlite_readonly(self.mbtiles_path)) as db:
                result = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1", (z, x, row)).fetchone()
            if result and len(result[0]) <= MAX_TILE_BYTES:
                kind = _image_type(result[0])
                if kind:
                    return result[0], kind
        except (OSError, sqlite3.Error):
            return None
        return None

    def _cache_paths(self, z: int, x: int, y: int) -> tuple[Path, Path]:
        image = self.tiles / str(z) / str(x) / f"{y}.tile"
        return image, image.with_suffix(".json")

    def _from_cache(self, z: int, x: int, y: int) -> tuple[bytes, str, dict[str, Any]] | None:
        image_path, metadata_path = self._cache_paths(z, x, y)
        try:
            if image_path.stat().st_size > MAX_TILE_BYTES:
                return None
            data = image_path.read_bytes()
            kind = _image_type(data)
            if not kind:
                return None
            try:
                metadata = json.loads(metadata_path.read_text())
                if not isinstance(metadata, dict):
                    metadata = {}
            except (OSError, ValueError):
                metadata = {}
            return data, kind, metadata
        except OSError:
            return None

    def _save(self, z: int, x: int, y: int, data: bytes, metadata: dict[str, Any]) -> None:
        image, meta = self._cache_paths(z, x, y)
        image.parent.mkdir(parents=True, exist_ok=True)
        for target, content in [(image, data), (meta, json.dumps(metadata).encode())]:
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, target)

    async def _fetch_remote(self, z: int, x: int, y: int, previous: dict[str, Any]) -> tuple[bytes | None, dict[str, Any]]:
        headers = {"User-Agent": OSM_USER_AGENT, "Accept": "image/png,image/jpeg"}
        for key, header in [("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")]:
            if isinstance(previous.get(key), str):
                headers[header] = previous[key]
        # No user URLs, proxies, redirects to another service or bulk requests.
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            async with client.stream("GET", f"https://tile.openstreetmap.org/{z}/{x}/{y}.png", headers=headers) as response:
                if response.status_code not in (200, 304):
                    raise ValueError(f"Kachelserver antwortet mit HTTP {response.status_code}")
                ttl = MIN_CACHE_SECONDS
                max_age = re.search(r"(?:^|,)\s*max-age=(\d+)", response.headers.get("cache-control", ""))
                if max_age:
                    ttl = max(ttl, min(int(max_age[1]), 365 * 86400))
                elif response.headers.get("expires"):
                    with contextlib.suppress(ValueError, TypeError, OverflowError):
                        ttl = max(ttl, email.utils.parsedate_to_datetime(response.headers["expires"]).timestamp() - time.time())
                metadata = {**previous, "expires_at": time.time() + ttl,
                            "etag": response.headers.get("etag", previous.get("etag")),
                            "last_modified": response.headers.get("last-modified", previous.get("last_modified"))}
                if response.status_code == 304:
                    return None, metadata
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_TILE_BYTES:
                    raise ValueError("Die Kachel ist größer als 2 MiB")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_TILE_BYTES:
                        raise ValueError("Die Kachel ist größer als 2 MiB")
                if _image_type(data) is None:
                    raise ValueError("Kachelserver lieferte keine PNG-/JPEG-Kachel")
                return bytes(data), metadata

    async def tile(self, z: int, x: int, y: int) -> Response:
        if not 0 <= z <= 24 or not 0 <= x < (1 << z) or not 0 <= y < (1 << z):
            raise HTTPException(400, "Ungültige XYZ-Kachelkoordinaten")
        embedded = await asyncio.to_thread(self._from_mbtiles, z, x, y)
        if embedded:
            return self._response(embedded[0], embedded[1], "mbtiles")
        cached = await asyncio.to_thread(self._from_cache, z, x, y)
        if cached and (self.offline or self._fresh(cached[2])):
            return self._response(cached[0], cached[1], "hit")
        if self.offline or z > 19:
            if cached:
                return self._response(cached[0], cached[1], "stale")
            raise self._missing()
        async with self._download_lock:
            # Recheck after waiting: settings may have switched offline and a
            # simultaneous request may already have populated this tile.
            cached = await asyncio.to_thread(self._from_cache, z, x, y)
            if self.offline:
                if cached:
                    return self._response(cached[0], cached[1], "hit")
                raise self._missing()
            if cached and self._fresh(cached[2]):
                return self._response(cached[0], cached[1], "hit")
            try:
                data, metadata = await self._fetch_remote(z, x, y, cached[2] if cached else {})
                if data is None:
                    if cached is None:
                        raise ValueError("Kachelserver meldet 304 ohne lokale Kachel")
                    data = cached[0]
                await asyncio.to_thread(self._save, z, x, y, data, metadata)
                return self._response(data, _image_type(data) or "image/png", "downloaded")
            except (httpx.HTTPError, ValueError, OSError):
                if cached:
                    return self._response(cached[0], cached[1], "stale")
                raise self._missing()

    @staticmethod
    def _fresh(metadata: dict[str, Any]) -> bool:
        try:
            expiration = float(metadata.get("expires_at", 0))
            return math.isfinite(expiration) and expiration > time.time()
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _response(data: bytes, kind: str, source: str) -> Response:
        return Response(data, media_type=kind,
                        headers={"X-Offline-Cache": source, "Cache-Control": "no-cache"})

    @staticmethod
    def _missing() -> HTTPException:
        return HTTPException(404, "Keine Kartenkachel im Offlinebestand verfügbar", headers={"X-Offline-Cache": "miss", "Cache-Control": "no-store"})

    async def import_mbtiles(self, request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in ("application/octet-stream", "application/x-sqlite3", "application/vnd.sqlite3"):
            raise HTTPException(415, "MBTiles als binäre Datei mit application/octet-stream hochladen")
        declared = request.headers.get("content-length")
        try:
            if declared and int(declared) > MAX_MBTILES_BYTES:
                raise HTTPException(413, "MBTiles-Upload ist auf 512 MiB begrenzt")
        except ValueError:
            raise HTTPException(400, "Ungültige Uploadlänge")
        async with self._import_lock:
            handle, name = tempfile.mkstemp(prefix="upload-", suffix=".mbtiles", dir=self.directory)
            temporary = Path(name)
            try:
                with os.fdopen(handle, "wb") as output:
                    total = 0
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > MAX_MBTILES_BYTES:
                            raise HTTPException(413, "MBTiles-Upload ist auf 512 MiB begrenzt")
                        output.write(chunk)
                try:
                    info = await asyncio.to_thread(inspect_mbtiles, temporary)
                except (OSError, ValueError) as exc:
                    raise HTTPException(422, str(exc)) from exc
                os.replace(temporary, self.mbtiles_path)
                self.mbtiles_info = info
            finally:
                temporary.unlink(missing_ok=True)
        return await asyncio.to_thread(self.status)


def create_map_router(cache_dir: Path) -> APIRouter:
    """Create one per-host map service, offline by default (persisted setting)."""
    router = APIRouter(prefix="/api/map", tags=["map"])
    cache = MapCache(Path(cache_dir))
    router.map_cache = cache  # Introspection for diagnostics/tests; not an HTTP path.

    @router.get("/tiles/{z}/{x}/{y}.png")
    async def get_tile(z: int, x: int, y: int):
        return await cache.tile(z, x, y)

    @router.get("/status")
    async def get_status():
        return await asyncio.to_thread(cache.status)

    @router.post("/settings")
    async def set_settings(settings: MapSettings):
        return await cache.set_offline(settings.offline)

    @router.put("/mbtiles")
    async def upload_mbtiles(request: Request):
        return await cache.import_mbtiles(request)

    return router

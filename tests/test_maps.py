import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from krakendf.maps import MAX_MBTILES_BYTES, MAX_TILE_BYTES, MapCache, create_map_router


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
JPEG = b"\xff\xd8\xff\xe0JPEG-test-tile"


def mbtiles(directory, tile=PNG, *, scheme="tms", attribution='<a href="https://example.test">Test &amp; Partner</a>', bad_schema=False, bad_coordinate=False):
    path = Path(directory) / "fixture.mbtiles"
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        if bad_schema:
            db.execute("CREATE TABLE tiles (wrong TEXT)")
        else:
            db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
            # XYZ z=2 x=1 y=0 maps to TMS row 3.
            db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (2, 1, 99 if bad_coordinate else (0 if scheme == "xyz" else 3), tile))
        db.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("format", "jpg" if tile.startswith(b"\xff") else "png"),
            ("scheme", scheme), ("attribution", attribution),
            ("name", "Testkarte"), ("bounds", "5,45,15,55")])
    return path.read_bytes()


class MapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.router = create_map_router(self.directory / "cache")
        self.cache = self.router.map_cache
        app = FastAPI()
        app.include_router(self.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def upload(self, data, **headers):
        return self.client.put("/api/map/mbtiles", content=data, headers={"Content-Type": "application/octet-stream", **headers})

    def test_default_offline_missing_tile_never_fetches(self):
        with patch.object(MapCache, "_fetch_remote", new_callable=AsyncMock) as remote:
            status = self.client.get("/api/map/status").json()
            self.assertTrue(status["offline"])
            self.assertEqual(status["cache_tile_count"], 0)
            self.assertFalse(status["mbtiles_loaded"])
            result = self.client.get("/api/map/tiles/2/1/0.png")
            self.assertEqual(result.status_code, 404)
            self.assertEqual(result.headers["x-offline-cache"], "miss")
            remote.assert_not_awaited()

    def test_mbtiles_tms_to_xyz_and_plain_attribution(self):
        response = self.upload(mbtiles(self.directory))
        self.assertEqual(response.status_code, 200, response.text)
        info = response.json()
        self.assertTrue(info["mbtiles_loaded"])
        self.assertEqual(info["mbtiles_tile_count"], 1)
        self.assertEqual(info["bounds"], [5, 45, 15, 55])
        self.assertEqual(info["attribution"], "Test & Partner")
        image = self.client.get("/api/map/tiles/2/1/0.png")
        self.assertEqual(image.content, PNG)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertEqual(image.headers["x-offline-cache"], "mbtiles")
        self.assertEqual(self.client.get("/api/map/tiles/2/1/3.png").status_code, 404)

    def test_xyz_scheme_and_jpeg_are_supported(self):
        self.assertEqual(self.upload(mbtiles(self.directory, JPEG, scheme="xyz")).status_code, 200)
        result = self.client.get("/api/map/tiles/2/1/0.png")
        self.assertEqual(result.content, JPEG)
        self.assertEqual(result.headers["content-type"], "image/jpeg")

    def test_invalid_import_does_not_replace_good_map(self):
        self.assertEqual(self.upload(mbtiles(self.directory)).status_code, 200)
        for data in [b"not sqlite", mbtiles(self.directory, bad_schema=True), mbtiles(self.directory, bad_coordinate=True)]:
            self.assertEqual(self.upload(data).status_code, 422)
            self.assertEqual(self.client.get("/api/map/tiles/2/1/0.png").content, PNG)
        self.assertEqual(list((self.directory / "cache").glob("upload-*")), [])

    def test_upload_size_content_type_and_no_path_api(self):
        too_big = self.upload(b"x", **{"Content-Length": str(MAX_MBTILES_BYTES + 1)})
        self.assertEqual(too_big.status_code, 413)
        # Streamed length is checked independently of Content-Length.
        with patch("krakendf.maps.MAX_MBTILES_BYTES", 16):
            self.assertEqual(self.upload(b"a" * 17).status_code, 413)
        self.assertEqual(self.client.put("/api/map/mbtiles", json={"path": "/etc/passwd"}).status_code, 415)
        self.assertEqual(self.client.get("/api/map/mbtiles", params={"path": "/etc/passwd"}).status_code, 405)
        self.assertEqual(self.client.get("/api/map/tiles/../../etc/passwd").status_code, 404)

    def test_invalid_coordinates_and_settings(self):
        for route in ["-1/0/0", "25/0/0", "0/1/0", "2/0/4", "2/-1/0"]:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(f"/api/map/tiles/{route}.png").status_code, 400)
        self.assertEqual(self.client.post("/api/map/settings", json={"offline": "false"}).status_code, 422)

    def test_online_download_is_cached_then_works_offline_and_persists(self):
        self.assertFalse(self.client.post("/api/map/settings", json={"offline": False}).json()["offline"])
        remote = AsyncMock(return_value=(PNG, {"expires_at": time.time() + 7 * 86400}))
        with patch.object(self.cache, "_fetch_remote", remote):
            first = self.client.get("/api/map/tiles/2/1/0.png")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers["x-offline-cache"], "downloaded")
            self.assertEqual(self.client.get("/api/map/tiles/2/1/0.png").content, PNG)
            self.assertEqual(remote.await_count, 1)
            self.client.post("/api/map/settings", json={"offline": True})
            self.assertEqual(self.client.get("/api/map/tiles/2/1/0.png").content, PNG)
            self.assertEqual(self.client.get("/api/map/tiles/2/2/0.png").status_code, 404)
            self.assertEqual(remote.await_count, 1)
        self.assertTrue(MapCache(self.directory / "cache").offline)
        self.assertEqual(self.client.get("/api/map/status").json()["cache_tile_count"], 1)

    def test_offline_uses_expired_tiles_and_online_failure_falls_back(self):
        self.cache._save(2, 1, 0, PNG, {"expires_at": 0})
        with patch.object(self.cache, "_fetch_remote", new_callable=AsyncMock, side_effect=httpx.ConnectError("no internet")) as remote:
            self.assertEqual(self.client.get("/api/map/tiles/2/1/0.png").content, PNG)
            remote.assert_not_awaited()
            self.client.post("/api/map/settings", json={"offline": False})
            result = self.client.get("/api/map/tiles/2/1/0.png")
            self.assertEqual(result.content, PNG)
            self.assertEqual(result.headers["x-offline-cache"], "stale")
            self.assertEqual(remote.await_count, 1)

    def test_invalid_cached_expiration_is_handled(self):
        self.cache._save(2, 1, 0, PNG, {"expires_at": "corrupt"})
        self.client.post("/api/map/settings", json={"offline": False})
        with patch.object(self.cache, "_fetch_remote", new_callable=AsyncMock, side_effect=ValueError("upstream error")):
            self.assertEqual(self.client.get("/api/map/tiles/2/1/0.png").content, PNG)

    def test_http_fetch_uses_identified_user_agent_and_seven_day_cache(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, content=PNG, headers={"Cache-Control": "max-age=3600", "ETag": '"tile-1"'})
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient
        def client_factory(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)
        self.client.post("/api/map/settings", json={"offline": False})
        with patch("krakendf.maps.httpx.AsyncClient", side_effect=client_factory):
            response = self.client.get("/api/map/tiles/2/1/0.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(captured[0].url), "https://tile.openstreetmap.org/2/1/0.png")
        self.assertIn("KrakenDFDemo/0.1", captured[0].headers["user-agent"])
        metadata = json.loads((self.directory / "cache/tiles/2/1/0.json").read_text())
        self.assertGreater(metadata["expires_at"], time.time() + 6.99 * 86400)

    def test_http_oversize_tile_not_stored(self):
        def handler(request):
            return httpx.Response(200, content=PNG, headers={"Content-Length": str(MAX_TILE_BYTES + 1)})
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient
        self.client.post("/api/map/settings", json={"offline": False})
        with patch("krakendf.maps.httpx.AsyncClient", side_effect=lambda *a, **kw: real_client(*a, transport=transport, **kw)):
            self.assertEqual(self.client.get("/api/map/tiles/2/1/0.png").status_code, 404)
        self.assertEqual(self.client.get("/api/map/status").json()["cache_tile_count"], 0)


if __name__ == "__main__":
    unittest.main()

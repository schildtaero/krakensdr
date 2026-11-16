"""Single local HTTP/WebSocket application; no hosted service required."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import os
from pathlib import Path
import re

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import load_presets
from .decoders import decoder_capabilities
from .diagnostics import build_diagnostic_archive, diagnostic_snapshot
from .dsp import statistics
from .hardware import HardwareError, MANUAL_GAINS_DB
from .maps import create_map_router
from .service import DemoService, save_json


class SourceRequest(BaseModel):
    mode: str
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    managed: bool | None = None


class RecordRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    kind: str = "measurement"


class ReplayRequest(BaseModel):
    session_id: str
    speed: float = Field(default=1, ge=.1, le=20, allow_inf_nan=False)


class HeadingRequest(BaseModel):
    callsign: str


class FitRequest(BaseModel):
    training_session_id: str
    validation_session_id: str


class ApplyRequest(BaseModel):
    calibration_id: str | None = None


class SymbolRequest(BaseModel):
    sidc: str = Field(pattern=r"^[A-Z0-9-]{15}$")
    label: str = Field(default="", max_length=80)

    @field_validator("sidc")
    @classmethod
    def legacy_warfighting_symbol(cls, value):
        if value[0] != "S" or value[1] not in "PUAFNSHGWMDLJK" or value[2] not in "PAGSUFXZ" or value[3] not in "APCDXF":
            raise ValueError("APP-6(B)-SIDC benötigt einen gültigen Warfighting-Code, Zuordnung, Dimension und Status.")
        return value


def create_app(data_dir: Path | str | None = None, initial_mode: str | None = None):
    data_dir = Path(data_dir or os.environ.get("KRAKENDF_DATA", Path.home() / ".krakendf"))
    service = DemoService(data_dir)

    @asynccontextmanager
    async def lifespan(app):
        await service.start(initial_mode or os.environ.get("KRAKENDF_SOURCE", "live"))
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="KrakenDF", version=__version__, lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Aufnahme oder Datei nicht gefunden."})

    @app.exception_handler(HardwareError)
    async def hardware_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        context = f"HTTP {request.method} {request.url.path}: {type(exc).__name__}"
        service.event(context, "error")
        service.diagnostic_log.exception(context, exc)
        return JSONResponse(status_code=500, content={
            "detail": "Interner Fehler. Diagnosepaket unter Status herunterladen."})

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": __version__, "source_mode": service.source["mode"]}

    @app.get("/api/state")
    async def state():
        return service.snapshot()

    @app.get("/api/config")
    async def config():
        return service.effective_config()

    @app.patch("/api/config")
    async def configure(patch: dict = Body(...)):
        async with service.control:
            await service.update_config(patch)
            return service.snapshot()

    @app.get("/api/arrays")
    async def arrays():
        return {"presets": load_presets()}

    @app.get("/api/decoders")
    async def decoders():
        return decoder_capabilities()

    @app.get("/api/diagnostics/download")
    async def diagnostics_download():
        snapshot = diagnostic_snapshot(service)
        content = await asyncio.to_thread(build_diagnostic_archive, data_dir, snapshot, list(service.events))
        return StreamingResponse(io.BytesIO(content), media_type="application/zip",
                                 headers={"Content-Disposition": 'attachment; filename="KrakenDF-Diagnose.zip"',
                                          "Cache-Control": "no-store"})

    @app.get("/api/hardware/gains")
    async def gains():
        return {"gains_db": MANUAL_GAINS_DB}

    @app.get("/api/gps/ports")
    async def gps_ports():
        try:
            from serial.tools import list_ports
            return {"model": "ARCELI VK-162", "ports": [{"device": p.device, "description": p.description} for p in list_ports.comports()], "serial_available": True}
        except ImportError:
            return {"model": "ARCELI VK-162", "ports": [], "serial_available": False,
                    "message": "USB-GPS benötigt pyserial: pip install '.[gps]'"}

    @app.post("/api/source")
    async def source(request: SourceRequest):
        async with service.control:
            await service.set_source(request.mode, request.host, request.port, request.managed)
            return service.snapshot()

    @app.post("/api/heading")
    async def heading(request: HeadingRequest):
        async with service.control:
            return await service.align_heading(request.callsign.upper())

    @app.post("/api/record/start")
    async def record_start(request: RecordRequest):
        async with service.control:
            return await service.start_recording(request.name, request.kind)

    @app.post("/api/record/stop")
    async def record_stop():
        async with service.control:
            return await service.stop_recording()

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": service.store.list_sessions()}

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        return {**service.store.get(session_id), "stats": statistics(service.store.observations(session_id))}

    @app.post("/api/replay/start")
    async def replay_start(request: ReplayRequest):
        async with service.control:
            await service.start_replay(request.session_id, request.speed)
            return service.snapshot()

    @app.post("/api/replay/stop")
    async def replay_stop():
        async with service.control:
            await service.stop_replay()
            return service.snapshot()

    @app.get("/api/calibrations")
    async def calibrations():
        return {"models": service.models, "active_id": service.active_calibration_id}

    @app.post("/api/calibration/fit")
    async def calibration_fit(request: FitRequest):
        async with service.control:
            return await service.fit(request.training_session_id, request.validation_session_id)

    @app.post("/api/calibration/apply")
    async def calibration_apply(request: ApplyRequest):
        async with service.control:
            service.apply_model(request.calibration_id)
            return service.snapshot()

    @app.post("/api/measurements/reset")
    async def reset():
        async with service.control:
            if service.recording or service.replay:
                raise ValueError("Aufnahme oder Replay vor dem Zurücksetzen stoppen.")
            service.reset_measurements()
            return service.snapshot()

    @app.get("/api/symbols")
    async def symbols():
        return {"symbols": service.symbols}

    @app.patch("/api/symbols/{callsign}")
    async def symbol(callsign: str, request: SymbolRequest):
        callsign = callsign.upper()
        if not re.fullmatch(r"[A-Z0-9/-]{1,16}", callsign):
            raise ValueError("Ungültiges Rufzeichen.")
        service.symbols[callsign] = request.model_dump()
        save_json(data_dir / "symbols.json", service.symbols)
        return {"symbols": service.symbols}

    @app.get("/api/export.xlsx")
    async def export(session_id: str | None = Query(default=None)):
        if session_id:
            session_config = service.store.get(session_id)["config"]
            observations = service.store.observations(session_id)
        else:
            session_config = service.effective_config()
            observations = list(service.observations)
        content = await asyncio.to_thread(service.store.export_xlsx, session_id=session_id,
                                          observations=observations, config=session_config,
                                          stats=statistics(observations), calibrations=service.models)
        return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": 'attachment; filename="KrakenDF-Messung.xlsx"'})

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        origin = socket.headers.get("origin")
        # Local UI uses same-origin websocket, without cross-origin control.
        if origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != socket.headers.get("host"):
                await socket.close(code=1008)
                return
        await socket.accept()
        try:
            while True:
                await socket.send_json({"type": "state", "data": service.snapshot()})
                await asyncio.sleep(.5)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            return

    app.include_router(create_map_router(data_dir / "map-cache"))
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html")

    return app

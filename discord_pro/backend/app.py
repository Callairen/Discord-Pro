"""FastAPI application: REST API, WebSocket protocol and dashboard hosting."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discord_pro import __version__, paths
from discord_pro.backend.activity import Activity, validate_client_id
from discord_pro.backend.config import ConfigStore, RpcSettings
from discord_pro.backend.errors import AppError, OperationResult
from discord_pro.backend.manager import RpcManager
from discord_pro.backend.presets import PresetInput, PresetStore
from discord_pro.backend.rpc import PypresenceAdapter, RpcAdapter, RpcThread
from discord_pro.backend.security import (
    CSRF_HEADER,
    SECURITY_HEADERS,
    SESSION_COOKIE,
    SESSION_TTL,
    AuthStore,
    RateLimiter,
    SessionManager,
    bearer_token,
    host_allowed,
    is_loopback,
    origin_allowed,
    peer_allowed,
)
from discord_pro.backend.service import PresenceService, validation_details
from discord_pro.backend.state import EventBus, StateManager

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_WS_MESSAGE = 64 * 1024
MAX_WS_INFLIGHT = 8
WS_PROTOCOL_VERSION = 1


# ----------------------------------------------------------------- schemas
class PresenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity: Activity
    apply: bool = True
    client_id: str | None = None


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enable: bool = False


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_code: str = Field(min_length=1, max_length=64)


class SetupValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = ""
    activity: dict[str, Any] | None = None


class SetupTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str


class SetupCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str
    activity: Activity
    test_activation: bool = False


# ---------------------------------------------------------------- context
@dataclass
class Backend:
    config: ConfigStore
    presets: PresetStore
    auth: AuthStore
    sessions: SessionManager
    bus: EventBus
    state: StateManager
    manager: RpcManager
    service: PresenceService
    rpc_thread: RpcThread | None
    request_shutdown: Callable[[], None] | None
    login_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(5, 300))
    command_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(30, 10))
    started: float = field(default_factory=time.time)

    @property
    def allow_lan(self) -> bool:
        # The *running* policy, not a pending (restart required) change.
        return bool(self.state.snapshot()["server"]["lan_active"])


@dataclass
class Principal:
    kind: str  # "token" or "session"
    peer: str

    @property
    def source(self) -> str:
        """Human-readable origin of a command for the event log."""
        return "browser" if self.kind == "session" else "desktop"


def _error(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


RESULT_STATUS = {
    "configuration_invalid": 422,
    "invalid_client_id": 422,
    "handshake_rejected": 422,
    "activity_rejected": 422,
    "presence_active": 409,
    "stopping": 503,
    "reconnect_exhausted": 503,
    "discord_not_found": 503,
    "rpc_timeout": 503,
    "timeout": 503,
    "handshake_failed": 503,
    "connection_interrupted": 503,
    "rpc_unavailable": 503,
    "rpc_server_error": 503,
}


def _result_response(backend: Backend, result: OperationResult) -> JSONResponse:
    status = 200 if result.success else RESULT_STATUS.get(result.code, 503 if result.pending else 502)
    return JSONResponse({"result": result.to_dict(), "state": backend.state.snapshot()}, status_code=status)


def create_app(
    *,
    adapter_factory: Callable[[], RpcAdapter] | None = None,
    request_shutdown: Callable[[], None] | None = None,
    health_interval: float = 2.0,
    rpc_settings: RpcSettings | None = None,
) -> FastAPI:
    config = ConfigStore(paths.config_path())
    presets = PresetStore(paths.presets_path())
    auth = AuthStore(paths.auth_path())
    bus = EventBus()
    cfg = config.config
    state = StateManager(bus, version=__version__, host=cfg.server.bind_host(), port=cfg.server.port,
                         allow_lan=cfg.server.allow_lan)

    rpc_thread: RpcThread | None = None
    if adapter_factory is None:
        rpc_thread = RpcThread()
        thread = rpc_thread
        adapter_factory = lambda: PypresenceAdapter(thread)  # noqa: E731

    service_ref: dict[str, PresenceService] = {}

    async def on_change(status):  # forwards RPC status into the canonical state
        await service_ref["svc"].on_rpc_status(status)

    manager = RpcManager(adapter_factory, rpc_settings or cfg.rpc, on_change, health_interval=health_interval)
    service = PresenceService(config, presets, state, manager)
    service_ref["svc"] = service
    backend = Backend(config, presets, auth, SessionManager(), bus, state, manager, service, rpc_thread,
                      request_shutdown)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        service.start()
        await service.refresh_config_state()
        for warning in config.warnings + presets.warnings:
            log.warning("startup: %s", warning)
        log.info("backend ready (version %s)", __version__)
        startup_task = None
        if config.config.active_app_id is not None:
            startup_task = asyncio.create_task(service.restore_app())
        elif config.config.application.start_with_presence_enabled and not service.configuration_problems():
            startup_task = asyncio.create_task(service.enable(source="startup"))
        try:
            yield
        finally:
            log.info("backend shutting down")
            if startup_task is not None and not startup_task.done():
                startup_task.cancel()
            await state.update(application={"running": False})
            await service.stop()
            await manager.stop()
            if rpc_thread is not None:
                rpc_thread.stop()
            log.info("backend stopped")

    app = FastAPI(title="Discord-Pro", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/v1/openapi.json")
    app.state.backend = backend

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def network_policy(request: Request, call_next):
        peer = request.client.host if request.client else None
        if not peer_allowed(peer, allow_lan=backend.allow_lan):
            return _error(403, "forbidden_network", "Requests from this network are not allowed")
        if not host_allowed(request.headers.get("host"), allow_lan=backend.allow_lan):
            return _error(400, "invalid_host", "Invalid Host header")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_BODY_BYTES:
                    return _error(413, "payload_too_large", "Request body is too large")
            except ValueError:
                return _error(400, "bad_request", "Invalid Content-Length")
        elif request.method in ("POST", "PUT", "PATCH") and request.headers.get("transfer-encoding"):
            return _error(411, "length_required", "Content-Length is required")
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]} for e in exc.errors()]
        return _error(422, "validation_error", "Request is invalid", details)

    @app.exception_handler(ValidationError)
    async def _pyd_error(request: Request, exc: ValidationError):
        return _error(422, "validation_error", "Request is invalid", validation_details(exc))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(500, "internal_error", "Internal server error")

    # ------------------------------------------------------------------ auth
    def principal(request: Request) -> Principal:
        peer = request.client.host if request.client else ""
        token = bearer_token(request.headers)
        if token is not None:
            if auth.check_token(token):
                return Principal("token", peer)
            raise AppError("unauthorized", "Invalid API token", status_code=401)
        if backend.sessions.valid(request.cookies.get(SESSION_COOKIE)):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get(CSRF_HEADER) != "1" or not origin_allowed(
                        request.headers.get("origin"), request.headers.get("host")):
                    raise AppError("csrf_failed", "Cross-site request rejected", status_code=403)
            return Principal("session", peer)
        raise AppError("unauthorized", "Authentication required", status_code=401)

    def token_only(request: Request) -> Principal:
        p = principal(request)
        if p.kind != "token":
            raise AppError("forbidden", "This operation requires the local API token", status_code=403)
        return p

    def limited(request: Request, p: Principal = Depends(principal)) -> Principal:
        if not backend.command_limiter.hit(p.peer or "?"):
            raise AppError("rate_limited", "Too many commands; slow down", status_code=429)
        return p

    # -------------------------------------------------------------- routes
    api = "/api/v1"

    @app.get(f"{api}/health")
    async def health():
        snap = state.snapshot()
        return {"status": "ok", "version": __version__, "running": snap["application"]["running"],
                "uptime": round(time.time() - backend.started, 1)}

    @app.get(f"{api}/state")
    async def get_state(p: Principal = Depends(principal)):
        return state.snapshot()

    @app.get(f"{api}/config")
    async def get_config(p: Principal = Depends(principal)):
        return config.config.public_dict()

    @app.put(f"{api}/config")
    async def put_config(patch: dict[str, Any] = Body(...), p: Principal = Depends(limited)):
        cfg = await service.update_config(patch, source=p.source)
        return cfg.public_dict()

    @app.post(f"{api}/presence/enable")
    async def enable(p: Principal = Depends(limited)):
        return _result_response(backend, await service.enable(source=p.source))

    @app.post(f"{api}/presence/disable")
    async def disable(p: Principal = Depends(limited)):
        return _result_response(backend, await service.disable(source=p.source))

    @app.put(f"{api}/presence")
    async def update_presence(body: PresenceUpdate, p: Principal = Depends(limited)):
        return _result_response(backend, await service.update_activity(
            body.activity, apply=body.apply, client_id=body.client_id, source=p.source))

    @app.post(f"{api}/presence/apply")
    async def apply_presence(p: Principal = Depends(limited)):
        return _result_response(backend, await service.apply_saved(source=p.source))

    @app.get(f"{api}/presets")
    async def list_presets(p: Principal = Depends(principal)):
        return [x.model_dump(mode="json") for x in presets.list()]

    @app.post(f"{api}/presets", status_code=201)
    async def create_preset(body: PresetInput, p: Principal = Depends(limited)):
        try:
            return (await service.create_preset(body, source=p.source)).model_dump(mode="json")
        except ValueError as exc:
            raise AppError("preset_limit", str(exc), status_code=409) from exc

    @app.put(f"{api}/presets/{{preset_id}}")
    async def update_preset(preset_id: str, body: PresetInput, p: Principal = Depends(limited)):
        return (await service.update_preset(preset_id, body, source=p.source)).model_dump(mode="json")

    @app.delete(f"{api}/presets/{{preset_id}}", status_code=204)
    async def delete_preset(preset_id: str, p: Principal = Depends(limited)):
        await service.delete_preset(preset_id, source=p.source)
        return Response(status_code=204)

    @app.post(f"{api}/presets/{{preset_id}}/duplicate", status_code=201)
    async def duplicate_preset(preset_id: str, p: Principal = Depends(limited)):
        return (await service.duplicate_preset(preset_id, source=p.source)).model_dump(mode="json")

    @app.post(f"{api}/presets/{{preset_id}}/activate")
    async def activate_preset(preset_id: str, body: ActivateRequest | None = None, p: Principal = Depends(limited)):
        enable_after = body.enable if body else False
        return _result_response(backend, await service.activate_preset(preset_id, enable=enable_after, source=p.source))

    @app.post(f"{api}/apps/{{preset_id}}/toggle")
    async def toggle_app(preset_id: str, p: Principal = Depends(limited)):
        return _result_response(backend, await service.toggle_app(preset_id, source=p.source))

    @app.post(f"{api}/setup/validate")
    async def setup_validate(body: SetupValidateRequest, p: Principal = Depends(principal)):
        errors: list[dict[str, str]] = []
        try:
            validate_client_id(body.client_id, allow_empty=False)
        except ValueError as exc:
            errors.append({"field": "client_id", "message": str(exc)})
        if body.activity is not None:
            try:
                activity = Activity.model_validate(body.activity)
                if not activity.has_content():
                    errors.append({"field": "activity", "message": "Add details, state or a large image"})
            except ValidationError as exc:
                errors.extend({"field": f"activity.{d['field']}", "message": d["message"]} for d in validation_details(exc))
        return {"valid": not errors, "errors": errors}

    @app.post(f"{api}/setup/test-connection")
    async def setup_test(body: SetupTestRequest, p: Principal = Depends(limited)):
        try:
            client_id = validate_client_id(body.client_id, allow_empty=False)
        except ValueError as exc:
            raise AppError("validation_error", str(exc), status_code=422) from exc
        result = await service.test_connection(client_id)
        return _result_response(backend, result)

    @app.post(f"{api}/setup/complete")
    async def setup_complete(body: SetupCompleteRequest, p: Principal = Depends(limited)):
        try:
            client_id = validate_client_id(body.client_id, allow_empty=False)
        except ValueError as exc:
            raise AppError("validation_error", str(exc), status_code=422) from exc
        if not body.activity.has_content():
            raise AppError("validation_error", "Activity needs details, state or a large image", status_code=422)
        await service.update_config({
            "application": {"client_id": client_id, "setup_completed": True},
            "presence": body.activity.model_dump(mode="json"),
            "presence_client_id": "",
            "active_preset_id": None,
            "active_app_id": None,
        }, source=p.source)
        if body.test_activation:
            return _result_response(backend, await service.enable(source=p.source))
        return _result_response(backend, OperationResult.ok("Setup saved"))

    @app.get(f"{api}/diagnostics")
    async def diagnostics(p: Principal = Depends(principal)):
        snap = state.snapshot()
        out = {
            "version": __version__,
            "uptime": round(time.time() - backend.started, 1),
            "rpc": {
                "state": snap["presence"]["connection_state"],
                "connected": snap["discord"]["rpc_connected"],
                "last_successful_update": snap["presence"]["last_successful_update"],
                "last_error": snap["discord"]["last_error"],
                "reconnect_attempt": snap["discord"]["reconnect_attempt"],
            },
            "clients": {"websocket": bus.count},
            "recent_errors": [e for e in snap["recent_events"] if not e["success"]][-10:],
            "warnings": snap["configuration"]["warnings"],
        }
        if is_loopback(p.peer):  # host details only for the local machine
            import pypresence

            out["host"] = {"python": sys.version.split()[0], "platform": platform.platform(),
                           "pypresence": getattr(pypresence, "__version__", "?"), "data_dir": str(paths.app_data_dir())}
        return out

    # ---------------------------------------------------------- auth routes
    def _set_session_cookie(response: Response) -> None:
        sid = backend.sessions.create_session()
        response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="strict", path="/")

    @app.get(f"{api}/auth/session")
    async def auth_session(request: Request):
        return {"authenticated": backend.sessions.valid(request.cookies.get(SESSION_COOKIE))}

    @app.post(f"{api}/auth/login")
    async def login(request: Request, body: LoginRequest):
        peer = request.client.host if request.client else "?"
        if not origin_allowed(request.headers.get("origin"), request.headers.get("host")):
            raise AppError("csrf_failed", "Cross-site request rejected", status_code=403)
        if not backend.login_limiter.hit(peer):
            log.warning("login rate limit hit from %s", peer)
            raise AppError("rate_limited", "Too many login attempts; wait a few minutes", status_code=429)
        if not auth.check_access_code(body.access_code):
            log.warning("failed dashboard login from %s", peer)
            raise AppError("invalid_access_code", "Access code is incorrect", status_code=401)
        backend.login_limiter.reset(peer)
        response = JSONResponse({"authenticated": True})
        _set_session_cookie(response)
        log.info("dashboard login from %s", peer)
        return response

    @app.post(f"{api}/auth/logout")
    async def logout(request: Request):
        backend.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post(f"{api}/auth/login-link")
    async def login_link(request: Request, p: Principal = Depends(token_only)):
        code = backend.sessions.create_login_code()
        return {"path": f"/auth/redeem?code={code}", "expires_in": 120}

    @app.get(f"{api}/auth/access-code")
    async def get_access_code(p: Principal = Depends(token_only)):
        return {"access_code": auth.access_code}

    @app.post(f"{api}/auth/access-code/rotate")
    async def rotate_access_code(p: Principal = Depends(token_only)):
        code = auth.rotate_access_code()
        backend.sessions.revoke_all()
        await state.record_event("auth.rotate", "Access code changed; browser sessions signed out", source="token")
        return {"access_code": code}

    @app.post(f"{api}/shutdown")
    async def shutdown(p: Principal = Depends(token_only)):
        if backend.request_shutdown is None:
            raise AppError("unsupported", "Shutdown is not available in this mode", status_code=501)
        log.info("shutdown requested via API")
        asyncio.get_running_loop().call_later(0.2, backend.request_shutdown)
        return {"stopping": True}

    @app.get("/auth/redeem")
    async def redeem(code: str = ""):
        if not code or not backend.sessions.redeem_login_code(code):
            return RedirectResponse("/?login=expired", status_code=303)
        response = RedirectResponse("/", status_code=303)
        _set_session_cookie(response)
        return response

    # ------------------------------------------------------------- websocket
    @app.websocket(f"{api}/ws")
    async def websocket_endpoint(ws: WebSocket):
        peer = ws.client.host if ws.client else None
        headers = ws.headers
        if not peer_allowed(peer, allow_lan=backend.allow_lan) or not host_allowed(
                headers.get("host"), allow_lan=backend.allow_lan):
            await ws.close(code=4403)  # refused before the handshake: nothing is exposed
            return
        if not origin_allowed(headers.get("origin"), headers.get("host")):
            await ws.close(code=4403)
            return
        token = bearer_token(headers)
        if token is not None:
            authed = auth.check_token(token)
        else:
            authed = backend.sessions.valid(ws.cookies.get(SESSION_COOKIE))
        if not authed:
            # Accept and immediately close with 4401 so browsers can see *why*
            # (a pre-handshake rejection only surfaces as code 1006). No data is sent.
            await ws.accept()
            await ws.close(code=4401, reason="authentication required")
            return
        await ws.accept()
        await WsConnection(backend, ws, peer or "?").run()

    # --------------------------------------------------------------- static
    web = paths.web_dir()
    if (web / "index.html").exists():
        app.mount("/static", StaticFiles(directory=web), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(web / "index.html")

    return app


class WsConnection:
    """One WebSocket client. Commands go through the same PresenceService
    used by REST; events come from the shared EventBus."""

    def __init__(self, backend: Backend, ws: WebSocket, peer: str):
        self.b = backend
        self.ws = ws
        self.peer = peer
        self._send_lock = asyncio.Lock()
        self._results: OrderedDict[str, asyncio.Future] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    async def send(self, event: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_text(json.dumps(event, default=str))

    async def run(self) -> None:
        queue = self.b.bus.subscribe()
        self.b.state.clients_changed()
        log.info("websocket client connected (%s)", self.peer)
        sender: asyncio.Task | None = None
        try:
            # Subscribe first, then send the snapshot, then start forwarding:
            # nothing is lost and the snapshot always precedes updates.
            await self.send({"type": "hello", "request_id": None, "payload": {"protocol": WS_PROTOCOL_VERSION,
                                                                                "version": __version__}})
            await self.send(self._snapshot_event())
            sender = asyncio.create_task(self._pump(queue))
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None and message.get("bytes") is not None:
                    await self._reject(None, "invalid_message", "Binary messages are not supported")
                    continue
                if text is None:
                    continue
                if len(text) > MAX_WS_MESSAGE:
                    await self._reject(None, "message_too_large", "Message is too large")
                    continue
                await self._handle_text(text)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if sender is not None:
                sender.cancel()
            for task in list(self._tasks):
                task.cancel()
            self.b.bus.unsubscribe(queue)
            self.b.state.clients_changed()
            log.info("websocket client disconnected (%s)", self.peer)

    def _snapshot_event(self) -> dict[str, Any]:
        return {"type": "state.snapshot", "request_id": None, "payload": {
            "state": self.b.state.snapshot(),
            "config": self.b.config.config.public_dict(),
            "presets": [p.model_dump(mode="json") for p in self.b.presets.list()],
        }}

    async def _pump(self, queue: asyncio.Queue) -> None:
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "_overflow":
                    await self.ws.close(code=4408)
                    return
                await self.send({"request_id": None, **event})
        except Exception:
            return

    async def _reject(self, request_id: str | None, code: str, message: str) -> None:
        await self.send({"type": "operation.result", "request_id": request_id,
                         "payload": {"success": False, "pending": False, "error": {"code": code, "message": message}}})

    async def _handle_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            await self._reject(None, "invalid_json", "Message is not valid JSON")
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            await self._reject(None, "invalid_message", "Message must be an object with a 'type'")
            return
        request_id = msg.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not 0 < len(request_id) <= 64):
            await self._reject(None, "invalid_request_id", "request_id must be a string of 1-64 characters")
            return
        payload = msg.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            await self._reject(request_id, "invalid_payload", "payload must be an object")
            return
        kind = msg["type"]
        if kind == "ping":
            await self.send({"type": "pong", "request_id": request_id, "payload": {}})
            return
        if kind == "state.get":
            await self.send({**self._snapshot_event(), "request_id": request_id})
            return
        if kind not in COMMANDS:
            await self._reject(request_id, "unknown_command", f"Unknown command '{kind[:40]}'")
            return
        if request_id is not None and request_id in self._results:  # duplicate: same result, no re-execution
            future = self._results[request_id]
            self._spawn(self._reply_when_done(request_id, future))
            return
        if len(self._tasks) >= MAX_WS_INFLIGHT or not self.b.command_limiter.hit(self.peer):
            await self._reject(request_id, "rate_limited", "Too many commands; slow down")
            return
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        if request_id is not None:
            self._results[request_id] = future
            while len(self._results) > 256:
                self._results.popitem(last=False)
        self._spawn(self._execute(kind, payload, request_id, future))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reply_when_done(self, request_id: str, future: asyncio.Future) -> None:
        result = await asyncio.shield(future)
        await self.send({"type": "operation.result", "request_id": request_id, "payload": {**result, "duplicate": True}})

    async def _execute(self, kind: str, payload: dict[str, Any], request_id: str | None, future: asyncio.Future) -> None:
        try:
            result = await COMMANDS[kind](self.b.service, payload)
            body = result.to_dict()
        except AppError as exc:
            body = {"success": False, "pending": False, "error": exc.to_dict()}
        except ValidationError as exc:
            body = {"success": False, "pending": False,
                    "error": {"code": "validation_error", "message": "Command is invalid",
                              "details": validation_details(exc)}}
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("websocket command %s failed", kind)
            body = {"success": False, "pending": False, "error": {"code": "internal_error", "message": "Internal error"}}
        if not future.done():
            future.set_result(body)
        try:
            await self.send({"type": "operation.result", "request_id": request_id, "payload": body})
        except Exception:
            pass


# -------------------------------------------------------- websocket commands
async def _cmd_enable(svc: PresenceService, p: dict) -> OperationResult:
    return await svc.enable(source="websocket")


async def _cmd_disable(svc: PresenceService, p: dict) -> OperationResult:
    return await svc.disable(source="websocket")


async def _cmd_update(svc: PresenceService, p: dict) -> OperationResult:
    body = PresenceUpdate.model_validate(p)
    return await svc.update_activity(body.activity, apply=body.apply, client_id=body.client_id, source="websocket")


async def _cmd_apply(svc: PresenceService, p: dict) -> OperationResult:
    return await svc.apply_saved(source="websocket")


def _preset_id(p: dict) -> str:
    preset_id = p.get("id")
    if not isinstance(preset_id, str) or not preset_id:
        raise AppError("validation_error", "payload.id is required")
    return preset_id


async def _cmd_preset_activate(svc: PresenceService, p: dict) -> OperationResult:
    return await svc.activate_preset(_preset_id(p), enable=bool(p.get("enable", False)), source="websocket")


async def _cmd_app_toggle(svc: PresenceService, p: dict) -> OperationResult:
    return await svc.toggle_app(_preset_id(p), source="websocket")


async def _cmd_preset_create(svc: PresenceService, p: dict) -> OperationResult:
    preset = await svc.create_preset(PresetInput.model_validate(p.get("preset") or {}), source="websocket")
    return OperationResult.ok("Preset created", preset=preset.model_dump(mode="json"))


async def _cmd_preset_update(svc: PresenceService, p: dict) -> OperationResult:
    preset = await svc.update_preset(_preset_id(p), PresetInput.model_validate(p.get("preset") or {}), source="websocket")
    return OperationResult.ok("Preset updated", preset=preset.model_dump(mode="json"))


async def _cmd_preset_delete(svc: PresenceService, p: dict) -> OperationResult:
    await svc.delete_preset(_preset_id(p), source="websocket")
    return OperationResult.ok("Preset deleted")


async def _cmd_preset_duplicate(svc: PresenceService, p: dict) -> OperationResult:
    preset = await svc.duplicate_preset(_preset_id(p), source="websocket")
    return OperationResult.ok("Preset duplicated", preset=preset.model_dump(mode="json"))


async def _cmd_config_update(svc: PresenceService, p: dict) -> OperationResult:
    patch = p.get("patch")
    if not isinstance(patch, dict):
        raise AppError("validation_error", "payload.patch must be an object")
    await svc.update_config(patch, source="websocket")
    return OperationResult.ok("Settings saved")


COMMANDS: dict[str, Callable[[PresenceService, dict], Any]] = {
    "presence.enable": _cmd_enable,
    "presence.disable": _cmd_disable,
    "presence.update": _cmd_update,
    "presence.apply": _cmd_apply,
    "preset.activate": _cmd_preset_activate,
    "preset.create": _cmd_preset_create,
    "preset.update": _cmd_preset_update,
    "preset.delete": _cmd_preset_delete,
    "preset.duplicate": _cmd_preset_duplicate,
    "app.toggle": _cmd_app_toggle,
    "config.update": _cmd_config_update,
}

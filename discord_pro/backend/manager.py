"""RPC connection state machine.

The manager is the only component that owns an RPC adapter, so there can never
be two competing Discord connections. Commands set a *desired* state; a single
worker task reconciles the actual connection towards it:

    desired.enabled = False  ->  clear activity, close pipe, DISCONNECTED
    desired.enabled = True   ->  connect (if needed), publish activity, CONNECTED

Failures are classified: transient ones are retried with bounded exponential
backoff plus jitter (RECONNECTING); permanent ones (invalid Application ID) and
exhausted retries move to ERROR and wait for a new user command.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from discord_pro.backend.config import RpcSettings
from discord_pro.backend.errors import OperationResult
from discord_pro.backend.rpc import RpcAdapter, RpcError

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 2.0
COMMAND_TIMEOUT = 20.0


class ConnState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"
    STOPPING = "STOPPING"


@dataclass
class Desired:
    enabled: bool
    client_id: str = ""
    activity: dict[str, Any] | None = None


@dataclass
class RpcStatus:
    state: ConnState = ConnState.DISCONNECTED
    connected_client_id: str | None = None
    published: dict[str, Any] | None = None
    last_successful_update: float | None = None
    last_error: dict[str, Any] | None = None
    reconnect_attempt: int = 0
    next_retry_in: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "connected_client_id": self.connected_client_id,
            "published": self.published,
            "last_successful_update": self.last_successful_update,
            "last_error": self.last_error,
            "reconnect_attempt": self.reconnect_attempt,
            "next_retry_in": self.next_retry_in,
        }


def backoff_delay(attempt: int, base: float, maximum: float, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with +/-20% jitter, capped at ``maximum``."""
    if attempt <= 0:
        return 0.0
    raw = min(maximum, base * (2 ** (attempt - 1)))
    return max(0.0, min(maximum, raw * (0.8 + 0.4 * rng())))


@dataclass
class _Waiter:
    version: int
    future: asyncio.Future = field(repr=False)


class RpcManager:
    def __init__(
        self,
        adapter_factory: Callable[[], RpcAdapter],
        settings: RpcSettings,
        on_change: Callable[[RpcStatus], Awaitable[None]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        health_interval: float = HEALTH_CHECK_INTERVAL,
    ):
        self._factory = adapter_factory
        self.settings = settings
        self._on_change = on_change
        self._clock = clock
        self._rng = rng
        self._health_interval = health_interval

        self.status = RpcStatus()
        self._adapter: RpcAdapter | None = None
        self._desired = Desired(enabled=False)
        self._version = 0
        self._published_version = -1
        self._published_key: tuple[str, Any] | None = None
        self._failed_version = -1
        self._attempt_now = False
        self._retry_at: float | None = None
        self._last_send = float("-inf")
        self._throttle_until: float | None = None
        self._waiters: list[_Waiter] = []
        self._tests: list[tuple[str, asyncio.Future]] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="rpc-manager")

    async def stop(self, timeout: float = 5.0) -> None:
        """Clear the activity, close the pipe and stop the worker."""
        if self._task is None:
            return
        self._stopping = True
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    @property
    def desired(self) -> Desired:
        return self._desired

    async def apply(self, desired: Desired, *, timeout: float = COMMAND_TIMEOUT) -> OperationResult:
        """Set the desired state and wait until the worker has acted on it."""
        if self._stopping:
            return OperationResult.fail("stopping", "Backend is shutting down")
        self._version += 1
        self._desired = desired
        self._attempt_now = True
        future = asyncio.get_running_loop().create_future()
        self._waiters.append(_Waiter(self._version, future))
        self._wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            return OperationResult.fail("timeout", "Discord did not respond in time; still trying", pending=True)

    async def test_connection(self, client_id: str, *, timeout: float = COMMAND_TIMEOUT) -> OperationResult:
        """Verify that a handshake with ``client_id`` succeeds (serialized with
        all other RPC work, so it never races the live connection)."""
        future = asyncio.get_running_loop().create_future()
        self._tests.append((client_id, future))
        self._wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            return OperationResult.fail("timeout", "Connection test timed out")

    # --------------------------------------------------------------- worker
    async def _run(self) -> None:
        try:
            while True:
                delay = self._next_wakeup()
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                if self._stopping:
                    await self._shutdown()
                    return
                try:
                    await self._process_tests()
                    await self._reconcile()
                except Exception:  # never let the worker die
                    log.exception("RPC manager iteration failed")
                    await asyncio.sleep(1)
        finally:
            self._fail_all_waiters()

    def _next_wakeup(self) -> float | None:
        candidates = []
        now = self._clock()
        if self._retry_at is not None:
            candidates.append(max(0.0, self._retry_at - now))
        if self._throttle_until is not None:
            candidates.append(max(0.0, self._throttle_until - now))
        if self._adapter is not None:
            candidates.append(self._health_interval)
        return min(candidates) if candidates else None

    async def _emit(self) -> None:
        if self._retry_at is not None:
            self.status.next_retry_in = round(max(0.0, self._retry_at - self._clock()), 1)
        else:
            self.status.next_retry_in = None
        if self._on_change is not None:
            try:
                await self._on_change(self.status)
            except Exception:
                log.exception("status listener failed")

    def _resolve(self, version: int, result: OperationResult) -> None:
        remaining = []
        for waiter in self._waiters:
            if waiter.version <= version:
                if not waiter.future.done():
                    waiter.future.set_result(result)
            else:
                remaining.append(waiter)
        self._waiters = remaining

    def _fail_all_waiters(self) -> None:
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_result(OperationResult.fail("stopping", "Backend is shutting down"))
        self._waiters = []
        for _, fut in self._tests:
            if not fut.done():
                fut.set_result(OperationResult.fail("stopping", "Backend is shutting down"))
        self._tests = []

    def _set_error(self, err: RpcError | None) -> None:
        self.status.last_error = None if err is None else {
            "code": err.code,
            "message": err.message,
            "kind": err.kind,
            "at": time.time(),
        }

    async def _close_adapter(self) -> None:
        adapter, self._adapter = self._adapter, None
        self.status.connected_client_id = None
        self.status.published = None
        self._published_version = -1
        self._published_key = None
        if adapter is not None:
            try:
                await adapter.close()
            except Exception:
                log.debug("adapter close failed", exc_info=True)

    async def _process_tests(self) -> None:
        tests, self._tests = self._tests, []
        for client_id, fut in tests:
            if fut.done():
                continue
            if self._adapter is not None and self._adapter.client_id == client_id and await self._adapter.is_alive():
                fut.set_result(OperationResult.ok("Connected to Discord", connected=True))
                continue
            if self._desired.enabled:
                fut.set_result(OperationResult.fail(
                    "presence_active",
                    "Presence is enabled with another Application ID; disable it before testing a different ID",
                ))
                continue
            adapter = self._factory()
            try:
                await adapter.connect(client_id)
                fut.set_result(OperationResult.ok("Connected to Discord", connected=True))
            except RpcError as err:
                fut.set_result(OperationResult.fail(err.code, err.message))
            finally:
                try:
                    await adapter.close()
                except Exception:
                    pass

    async def _reconcile(self) -> None:
        d = self._desired
        version = self._version

        if not d.enabled:
            self._retry_at = None
            self._throttle_until = None
            self.status.reconnect_attempt = 0
            if self._adapter is not None:
                result = await self._clear_and_close()
            else:
                result = OperationResult.ok("Presence is off (nothing was published)", cleared="none")
            self.status.state = ConnState.DISCONNECTED
            if result.success:
                self._set_error(None)
            await self._emit()
            self._resolve(version, result)
            return

        # ---- enabled: make sure we are connected with the right Application ID
        if self._adapter is not None and self._adapter.client_id != d.client_id:
            await self._clear_and_close()
        if self._adapter is not None and not await self._adapter.is_alive():
            log.warning("Discord RPC connection lost")
            await self._close_adapter()
            self._set_error(RpcError("transient", "connection_interrupted", "Connection to Discord was lost"))
            self.status.reconnect_attempt = 0
            self._retry_at = self._clock()  # reconnect right away
            self.status.state = ConnState.RECONNECTING

        if self._adapter is None:
            if self.status.state == ConnState.ERROR and not self._attempt_now:
                return  # permanent error / exhausted: wait for a new command
            now = self._clock()
            if not self._attempt_now and self._retry_at is not None and now < self._retry_at:
                return
            self._attempt_now = False
            if self.status.state != ConnState.RECONNECTING:
                self.status.state = ConnState.CONNECTING
            await self._emit()
            adapter = self._factory()
            try:
                await adapter.connect(d.client_id)
            except RpcError as err:
                await self._handle_connect_failure(err, version)
                return
            log.info("connected to Discord RPC")
            self._adapter = adapter
            self.status.connected_client_id = d.client_id
            self.status.reconnect_attempt = 0
            self._retry_at = None
            self._set_error(None)
        self._attempt_now = False

        # ---- connected: publish if the desired activity differs
        key = (d.client_id, d.activity)
        if self._published_key == key and self._published_version >= 0:
            self._published_version = version
            self.status.state = ConnState.CONNECTED
            await self._emit()
            self._resolve(version, OperationResult.ok("Activity already published"))
            return
        now = self._clock()
        ready_at = self._last_send + self.settings.min_update_interval
        if now < ready_at:
            self._throttle_until = ready_at  # coalesce: publish the newest desired state later
            if self.status.state != ConnState.CONNECTED:
                self.status.state = ConnState.CONNECTED
                await self._emit()
            return
        self._throttle_until = None
        try:
            self._last_send = now
            await self._adapter.set_activity(d.activity or {})  # type: ignore[union-attr]
        except RpcError as err:
            if err.kind == "payload":
                self._set_error(err)
                self._published_version = version
                self._published_key = None
                self.status.state = ConnState.CONNECTED
                await self._emit()
                self._resolve(version, OperationResult.fail(err.code, err.message))
                return
            await self._close_adapter()
            await self._handle_connect_failure(err, version)
            return
        self._published_version = version
        self._published_key = key
        self.status.published = d.activity
        self.status.last_successful_update = time.time()
        self.status.state = ConnState.CONNECTED
        self._set_error(None)
        await self._emit()
        self._resolve(version, OperationResult.ok("Activity published"))

    async def _handle_connect_failure(self, err: RpcError, version: int) -> None:
        self._set_error(err)
        if err.permanent:
            log.error("RPC connection failed permanently: %s", err.message)
            self.status.state = ConnState.ERROR
            self._retry_at = None
            await self._emit()
            self._resolve(version, OperationResult.fail(err.code, err.message))
            return
        self.status.reconnect_attempt += 1
        attempt = self.status.reconnect_attempt
        if attempt >= self.settings.reconnect_max_attempts:
            log.error("giving up after %d reconnect attempts: %s", attempt, err.message)
            self.status.state = ConnState.ERROR
            self._retry_at = None
            self.status.last_error = {**(self.status.last_error or {}), "code": "reconnect_exhausted",
                                      "message": f"Gave up after {attempt} attempts: {err.message}"}
            await self._emit()
            self._resolve(version, OperationResult.fail("reconnect_exhausted", self.status.last_error["message"]))
            return
        delay = backoff_delay(attempt, self.settings.reconnect_base_delay, self.settings.reconnect_max_delay, self._rng)
        self._retry_at = self._clock() + delay
        self.status.state = ConnState.RECONNECTING
        log.info("RPC unavailable (%s); retry %d in %.1fs", err.code, attempt, delay)
        await self._emit()
        self._resolve(version, OperationResult.fail(err.code, err.message, pending=True))

    async def _clear_and_close(self) -> OperationResult:
        adapter = self._adapter
        if adapter is None:
            return OperationResult.ok("Nothing to clear", cleared="none")
        try:
            await adapter.clear()
            result = OperationResult.ok("Activity cleared", cleared="clear")
        except RpcError as err:
            # Discord drops an RPC client's activity when its pipe closes, so a
            # closed connection still removes the presence; report how.
            log.warning("clear failed (%s); closing connection instead", err.code)
            result = OperationResult.ok(
                "Connection closed; Discord removes the activity when the RPC connection ends",
                cleared="disconnect",
            )
        await self._close_adapter()
        return result

    async def _shutdown(self) -> None:
        self.status.state = ConnState.STOPPING
        await self._emit()
        if self._adapter is not None:
            await self._clear_and_close()
        self.status.state = ConnState.DISCONNECTED
        await self._emit()

"""Discord local RPC adapter.

Uses ``pypresence`` (a maintained client for Discord's local IPC/RPC protocol)
to talk to the Discord desktop client. All pypresence calls run on one
dedicated thread with its own asyncio loop so that:

* only one coroutine at a time touches the IPC pipe;
* the loop runs continuously, letting the transport notice when Discord
  closes the pipe (used for disconnect detection);
* on Windows the loop is a ProactorEventLoop, which named pipes require,
  independent of the event loop uvicorn picks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class RpcError(Exception):
    """Classified RPC failure.

    kind:
      ``transient`` - may succeed later (Discord closed, pipe busy, timeout);
      ``permanent`` - retrying will not help (invalid Application ID);
      ``payload``   - Discord rejected the activity contents.
    """

    def __init__(self, kind: str, code: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message

    @property
    def permanent(self) -> bool:
        return self.kind == "permanent"


class RpcAdapter(ABC):
    """Interface the RPC manager uses; tests substitute a fake."""

    client_id: str | None = None

    @abstractmethod
    async def connect(self, client_id: str) -> None: ...

    @abstractmethod
    async def set_activity(self, kwargs: dict[str, Any]) -> None: ...

    @abstractmethod
    async def clear(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def is_alive(self) -> bool: ...


class RpcThread:
    """A background thread running a private asyncio event loop."""

    def __init__(self) -> None:
        if sys.platform == "win32":
            self.loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
        else:
            self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="discord-rpc", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def call(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Run ``factory()`` on the RPC loop and await its result from another loop."""
        future = asyncio.run_coroutine_threadsafe(_invoke(factory), self.loop)
        return await asyncio.wrap_future(future)

    def stop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3)


async def _invoke(factory: Callable[[], Awaitable[T]]) -> T:
    return await factory()


def classify(exc: BaseException, *, during: str) -> RpcError:
    """Map pypresence / OS exceptions to RpcError categories."""
    try:
        from pypresence import exceptions as pe
    except ImportError:  # pragma: no cover - dependency missing
        return RpcError("permanent", "library_missing", "The 'pypresence' library is not installed")

    if isinstance(exc, RpcError):
        return exc
    if isinstance(exc, pe.DiscordNotFound):
        return RpcError("transient", "discord_not_found", "Discord desktop client not detected (is Discord running?)")
    if isinstance(exc, pe.InvalidID):
        return RpcError("permanent", "invalid_client_id", "Discord rejected the Application ID (check the Developer Portal)")
    if isinstance(exc, pe.ServerError):
        if during == "update":
            return RpcError("payload", "activity_rejected", f"Discord rejected the activity: {exc}")
        return RpcError("transient", "rpc_server_error", f"Discord RPC error: {exc}")
    if isinstance(exc, pe.DiscordError):
        return RpcError("permanent", "handshake_rejected", f"Discord refused the RPC handshake: {exc}")
    if isinstance(exc, (pe.ConnectionTimeout, pe.ResponseTimeout, asyncio.TimeoutError, TimeoutError)):
        return RpcError("transient", "rpc_timeout", "Timed out waiting for Discord")
    if isinstance(exc, pe.InvalidPipe):
        return RpcError("transient", "handshake_failed", "RPC handshake failed (Discord pipe not ready)")
    if isinstance(exc, (pe.PipeClosed, BrokenPipeError, ConnectionResetError, EOFError)):
        return RpcError("transient", "connection_interrupted", "Connection to Discord was interrupted")
    if isinstance(exc, (OSError, AssertionError)):
        return RpcError("transient", "rpc_unavailable", f"Discord RPC pipe unavailable: {exc}")
    return RpcError("transient", "rpc_unexpected", f"Unexpected RPC failure: {type(exc).__name__}")


class PypresenceAdapter(RpcAdapter):
    """Real adapter backed by ``pypresence.AioPresence`` on an RpcThread."""

    def __init__(self, thread: RpcThread, *, timeout: float = 5.0):
        self._thread = thread
        self._timeout = timeout
        self._rpc: Any = None
        self.client_id = None

    async def connect(self, client_id: str) -> None:
        async def _do() -> None:
            from pypresence import AioPresence

            rpc = AioPresence(
                client_id,
                loop=self._thread.loop,
                connection_timeout=self._timeout,
                response_timeout=self._timeout,
            )
            try:
                await asyncio.wait_for(rpc.connect(), self._timeout * 2)
            except BaseException:
                _close_transport(rpc)
                raise
            self._rpc = rpc

        try:
            await self._thread.call(_do)
        except Exception as exc:
            raise classify(exc, during="connect") from exc
        self.client_id = client_id

    async def set_activity(self, kwargs: dict[str, Any]) -> None:
        async def _do() -> None:
            if self._rpc is None:
                raise RpcError("transient", "not_connected", "Not connected to Discord")
            await self._rpc.update(pid=os.getpid(), **kwargs)

        try:
            await self._thread.call(_do)
        except Exception as exc:
            raise classify(exc, during="update") from exc

    async def clear(self) -> None:
        async def _do() -> None:
            if self._rpc is None:
                raise RpcError("transient", "not_connected", "Not connected to Discord")
            await self._rpc.clear(pid=os.getpid())

        try:
            await self._thread.call(_do)
        except Exception as exc:
            raise classify(exc, during="clear") from exc

    async def close(self) -> None:
        async def _do() -> None:
            rpc, self._rpc = self._rpc, None
            if rpc is None:
                return
            try:
                rpc.send_data(2, {"v": 1, "client_id": rpc.client_id})  # op 2 = CLOSE
            except Exception:
                pass
            _close_transport(rpc)
            await asyncio.sleep(0)

        try:
            await self._thread.call(_do)
        finally:
            self.client_id = None

    async def is_alive(self) -> bool:
        async def _do() -> bool:
            rpc = self._rpc
            reader = getattr(rpc, "sock_reader", None)
            return bool(rpc is not None and reader is not None and not reader.at_eof())

        try:
            return await self._thread.call(_do)
        except Exception:
            return False


def _close_transport(rpc: Any) -> None:
    # pypresence's own close() also closes the event loop, which we must keep
    # running, so the transport is closed directly.
    writer = getattr(rpc, "sock_writer", None)
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass

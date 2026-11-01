"""The TeslaMate bridge: connections, fan-out and the HTTP view.

TeslaMate's ``TESLA_WSS_HOST`` points at a server speaking Tesla's legacy
streaming protocol. The vehicle already streams to this integration, decoded
per signal, so serving that protocol is a fan-out table over the coordinator's
data rather than a second data path. ``stream_format.py`` does the encoding;
this module owns the sockets.

The view carries no credential of its own: the reverse proxy in front of it
injects ``X-Tesla-Proxy-Secret`` — the same secret the vehicle endpoint uses —
so the TeslaMate hop needs neither TLS nor a secret of its own. HA's own port
serves this view too, which is why the header check is not optional.

The ``token`` a client sends in ``data:subscribe_oauth`` is ignored. It is a
real Tesla access token, and validating it would mean spending Fleet API calls
to learn something the proxy secret already answered.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from ..const import HEADER_PROXY_SECRET, proxy_secret_matches
from ..coordinator import SignalSample
from .protocol import DEFAULT_COLUMNS, SOURCE_SIGNALS, StreamRow

if TYPE_CHECKING:
    from ..coordinator import TeslaTelemetryCoordinator

_LOGGER = logging.getLogger(__name__)

# How recently the vehicle must have sent something for its cached signals to
# be safe to reuse. The car streams continuously while awake, so a quiet
# minute means it is asleep or HA has just restarted and the cache predates a
# journey — either way the cached location may no longer be where the car is.
SEED_MAX_AGE_SECONDS = 60

# TeslaMate appends "/streaming/" to TESLA_WSS_HOST, so this path is what it
# asks for verbatim and the proxy can forward it without rewriting the path.
STREAM_PATH = "/api/tesla_telemetry/streaming/"

# How often an idle connection repeats its control:hello. TeslaMate drops a
# connection it has heard nothing from for 30 s, and a parked car streams
# nothing, so the heartbeat is what keeps the subscription alive.
STREAM_HEARTBEAT_SECONDS = 10

# Outbound frames queued per connection before new ones are dropped. A local
# TeslaMate drains this instantly; the cap only stops a stalled reader from
# growing the queue without bound.
_QUEUE_MAX = 64


# eq=False keeps the default identity hash: clients are tracked in a set and
# two connections are never interchangeable, even with identical fields.
@dataclass(eq=False)
class _Client:
    """One TeslaMate connection."""

    ws: web.WebSocketResponse
    vin: str
    row: StreamRow
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(_QUEUE_MAX))
    writer: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None


class TeslaMateStreamHub:
    """Fans decoded signals out to the TeslaMate connections for that VIN."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._clients: dict[str, set[_Client]] = defaultdict(set)

    def register(self, client: _Client) -> None:
        self._clients[client.vin].add(client)
        client.writer = self.hass.async_create_task(self._drain(client))
        client.heartbeat = self.hass.async_create_task(self._heartbeat(client))
        _LOGGER.info(
            "tesla_telemetry: teslamate subscribed to vin=%s columns=%s",
            client.vin,
            ",".join(client.row.columns),
        )

    def unregister(self, client: _Client) -> None:
        clients = self._clients.get(client.vin)
        # close_vin removes the client itself, and the view unregisters again
        # from its finally block once the socket closes. The second call has
        # nothing left to do — without this it also logged a second
        # "disconnected" for the same connection.
        if clients is None or client not in clients:
            return
        clients.discard(client)
        if not clients:
            del self._clients[client.vin]
        for task in (client.writer, client.heartbeat):
            if task is not None:
                task.cancel()
        _LOGGER.info("tesla_telemetry: teslamate disconnected vin=%s", client.vin)

    def close_vin(self, vin: str) -> None:
        """Drop every connection for a vehicle whose entry has gone away."""
        for client in tuple(self._clients.get(vin, ())):
            self.unregister(client)
            self.hass.async_create_task(client.ws.close())

    @callback
    def publish(self, vin: str, name: str, sample: SignalSample) -> None:
        """Called for every datum the vehicle sends."""
        for client in tuple(self._clients.get(vin, ())):
            if client.row.apply(name, sample.value):
                self._send(client, client.row.frame(sample.payload_created_at))

    def _send(self, client: _Client, payload: str) -> None:
        try:
            client.queue.put_nowait(payload)
        except asyncio.QueueFull:
            _LOGGER.debug(
                "tesla_telemetry: teslamate queue full for vin=%s, dropping frame",
                client.vin,
            )

    async def _drain(self, client: _Client) -> None:
        """Write queued frames in order.

        One writer per connection, so a position can never overtake the one
        before it — TeslaMate stores them as they land.
        """
        try:
            while True:
                payload = await client.queue.get()
                await client.ws.send_str(payload)
        except (ConnectionResetError, RuntimeError) as err:
            _LOGGER.debug(
                "tesla_telemetry: teslamate write failed vin=%s: %s",
                client.vin,
                err,
            )

    async def _heartbeat(self, client: _Client) -> None:
        """Repeat ``control:hello`` so an idle car doesn't look like a dead
        connection: TeslaMate closes after 30 s of silence.

        The first one goes out at once — TeslaMate arms a 10 s timer the
        moment it subscribes, and a car parked with nothing to report would
        otherwise lose that race and reconnect.
        """
        hello = json.dumps(
            {
                "msg_type": "control:hello",
                "connection_timeout": STREAM_HEARTBEAT_SECONDS * 3000,
            }
        )
        while True:
            self._send(client, hello)
            await asyncio.sleep(STREAM_HEARTBEAT_SECONDS)


class TeslaTelemetryStreamView(HomeAssistantView):
    """HA HTTP view serving TeslaMate's streaming protocol."""

    url = STREAM_PATH
    name = "api:tesla_telemetry:streaming"
    requires_auth = False  # gated by the proxy secret header instead

    def __init__(
        self,
        hub: TeslaMateStreamHub,
        secrets_by_vin: dict[str, str],
        vins_by_vehicle_id: dict[str, str],
        enabled_vins: set[str],
        coordinators_by_vin: dict[str, TeslaTelemetryCoordinator],
    ) -> None:
        self._hub = hub
        # Shared with __init__ and kept current as entries load and unload.
        self._secrets = secrets_by_vin
        self._vins_by_vehicle_id = vins_by_vehicle_id
        self._enabled = enabled_vins
        self._coordinators = coordinators_by_vin

    async def get(self, request: web.Request) -> web.StreamResponse:
        secret = request.headers.get(HEADER_PROXY_SECRET, "")
        # One answer for every rejection, so a caller can't probe which VINs
        # are configured. A client may only subscribe to a vehicle whose own
        # entry secret it presented.
        allowed = {
            vin
            for vin, configured in self._secrets.items()
            if proxy_secret_matches(secret, configured)
        }
        # The view outlives the option that registers it — HA cannot
        # unregister one without a restart — so the switch is re-checked here
        # on every connection. Turning it off stops serving immediately.
        allowed &= self._enabled
        if not allowed:
            # The header name is spelled out rather than passed as the
            # constant: CodeQL reads a variable called HEADER_PROXY_SECRET in
            # a log call as a leaked secret, and the vehicle endpoint's
            # rejection is worded the same way.
            _LOGGER.warning(
                "tesla_telemetry: rejecting teslamate connection — bad "
                "X-Tesla-Proxy-Secret header"
            )
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30, compress=False)
        await ws.prepare(request)

        client: _Client | None = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if client is None:
                    client = self._subscribe(ws, msg.data, allowed)
                    if client is None:
                        await ws.send_str(
                            json.dumps(
                                {
                                    "msg_type": "error",
                                    "error_detail": "unknown or unsupported "
                                    "subscription",
                                }
                            )
                        )
                        break
                    self._hub.register(client)
                elif _is_unsubscribe(msg.data):
                    break
        finally:
            if client is not None:
                self._hub.unregister(client)
        return ws

    def _subscribe(
        self, ws: web.WebSocketResponse, raw: Any, allowed: set[str]
    ) -> _Client | None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(frame, dict):
            return None

        # ``data:subscribe_all`` asks for raw fleet-telemetry frames, a
        # different contract than the CSV rows this view emits.
        if frame.get("msg_type") not in ("data:subscribe_oauth", "data:subscribe"):
            return None

        tag = str(frame.get("tag") or "")
        vin = self._vins_by_vehicle_id.get(tag, tag)
        # ``allowed`` is the set the connection was accepted with; the entry
        # can unload between the upgrade and this message, so the switch is
        # re-read here rather than trusting that snapshot.
        if vin not in allowed or vin not in self._enabled:
            _LOGGER.warning(
                "tesla_telemetry: teslamate asked for unknown vehicle tag=%s",
                tag[:64].replace("\r", "").replace("\n", ""),
            )
            return None

        columns = tuple(
            column for column in str(frame.get("value") or "").split(",") if column
        )
        row = StreamRow(columns=columns or DEFAULT_COLUMNS, tag=tag)
        self._seed(row, vin)
        return _Client(ws=ws, vin=vin, row=row)

    def _seed(self, row: StreamRow, vin: str) -> None:
        """Prime a new connection's row from the coordinator's cache.

        A frame needs a location, and a parked car only streams location when
        it changes — which it does not do while standing still. Without this,
        a client that connects while the car is parked stays silent until the
        car moves, even as gear and charging power arrive. Seeding means the
        next such change is enough to emit a complete frame.

        The cache is only reused while the stream is live: after an HA restart
        the last known location can predate a journey, and handing TeslaMate a
        stale position would put a wrong point in its history.
        """
        coordinator = self._coordinators.get(vin)
        if coordinator is None:
            return
        samples = {
            name: sample
            for name in SOURCE_SIGNALS
            if (sample := coordinator.get(name)) is not None
        }
        if not samples:
            return
        if time.time() - max(s.received_at for s in samples.values()) > (
            SEED_MAX_AGE_SECONDS
        ):
            return
        for name, sample in samples.items():
            row.apply(name, sample.value)


def _is_unsubscribe(raw: Any) -> bool:
    try:
        frame = json.loads(raw)
    except ValueError:
        return False
    return isinstance(frame, dict) and frame.get("msg_type") == "data:unsubscribe"

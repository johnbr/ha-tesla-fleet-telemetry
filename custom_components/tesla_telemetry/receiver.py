"""WebSocket endpoint for the Tesla Fleet Telemetry stream.

nginx (host-side) terminates TLS + mTLS for the public-facing FQDN, then
proxies the WS upgrade to ``/api/tesla_telemetry/ws`` on HA's HTTP server
with two custom headers:

  * ``X-Tesla-Proxy-Secret``  — shared secret only nginx knows; rejects any
    caller that didn't go through the proxy.
  * ``X-Tesla-Verified-Vin``  — cert subject DN from the validated client
    cert. The CN is the vehicle's device id (typically the VIN).

The view dispatches each received signal to a ``TeslaTelemetryCoordinator``
keyed by VIN; entities subscribe to the coordinator separately.
"""
from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

import flatbuffers
from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView

from .const import HEADER_PROXY_SECRET, HEADER_VERIFIED_VIN, WS_PATH
from .proto import tesla_envelope_generated as fbgen
from .proto import vehicle_data_pb2 as vdp

if TYPE_CHECKING:
    from .coordinator import TeslaTelemetryCoordinator

_LOGGER = logging.getLogger(__name__)


class TeslaTelemetryView(HomeAssistantView):
    """HA HTTP view that handles inbound vehicle WebSocket connections."""

    url = WS_PATH
    name = "api:tesla_telemetry:ws"
    requires_auth = False  # gated by the proxy secret header instead

    def __init__(
        self,
        coordinators_by_vin: dict[str, "TeslaTelemetryCoordinator"],
        proxy_secret: str,
    ) -> None:
        self._coordinators = coordinators_by_vin
        self._proxy_secret = proxy_secret

    async def get(self, request: web.Request) -> web.StreamResponse:
        secret = request.headers.get(HEADER_PROXY_SECRET, "")
        if not self._proxy_secret or not hmac.compare_digest(
            secret, self._proxy_secret
        ):
            _LOGGER.warning(
                "tesla_telemetry: rejecting ws connection — bad %s header",
                HEADER_PROXY_SECRET,
            )
            return web.Response(status=401, text="unauthorized")

        verified_dn = request.headers.get(HEADER_VERIFIED_VIN, "")
        vin = _vin_from_subject_dn(verified_dn)
        if not vin:
            _LOGGER.warning(
                "tesla_telemetry: cannot extract VIN from subject dn %r",
                verified_dn,
            )
            return web.Response(status=400, text="missing vin")

        coordinator = self._coordinators.get(vin)
        if coordinator is None:
            _LOGGER.warning(
                "tesla_telemetry: no coordinator configured for vin=%s — "
                "rejecting connection",
                vin,
            )
            return web.Response(status=403, text="vin not configured")

        # NOTE: compress=False is mandatory. Hermes/1.16.9 (vehicle_device)
        # advertises permessage-deflate in the upgrade request but closes
        # the TCP connection with no frames sent if the server agrees to
        # compression. Declining the extension makes it work.
        ws = web.WebSocketResponse(heartbeat=30, compress=False)
        await ws.prepare(request)
        _LOGGER.info("tesla_telemetry: vehicle connected vin=%s", vin)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    ack = _process_envelope(msg.data, coordinator)
                    if ack is not None:
                        await ws.send_bytes(ack)
                elif msg.type == WSMsgType.ERROR:
                    _LOGGER.error(
                        "tesla_telemetry: ws error vin=%s err=%s",
                        vin,
                        ws.exception(),
                    )
                    break
        finally:
            _LOGGER.info(
                "tesla_telemetry: vehicle disconnected vin=%s close_code=%s",
                vin,
                ws.close_code,
            )
        return ws


def _vin_from_subject_dn(dn: str) -> str:
    """Pull the CN out of a subject DN. Falls back to the whole string."""
    for part in dn.split(","):
        key, _, value = part.strip().partition("=")
        if key.upper() == "CN" and value:
            return value
    return dn.strip()


def _process_envelope(
    wire: bytes, coordinator: "TeslaTelemetryCoordinator"
) -> bytes | None:
    """Parse a vehicle->server envelope, dispatch its data, return an ack."""
    try:
        env = fbgen.FlatbuffersEnvelope.GetRootAs(wire, 0)
    except Exception as err:  # noqa: BLE001 — never raise from the WS loop
        _LOGGER.error("tesla_telemetry: malformed flatbuffer: %s", err)
        return None

    txid = bytes(env.Txid(i) for i in range(env.TxidLength()))
    topic_bytes = bytes(env.Topic(i) for i in range(env.TopicLength()))
    topic = topic_bytes.decode("ascii", errors="replace")

    if env.MessageType() != fbgen.Message.FlatbuffersStream:
        _LOGGER.debug(
            "tesla_telemetry: unexpected envelope type=%s topic=%s",
            env.MessageType(),
            topic,
        )
        return _build_ack(txid, topic_bytes)

    union_tbl = env.Message()
    if union_tbl is None:
        return _build_ack(txid, topic_bytes)
    stream = fbgen.FlatbuffersStream()
    stream.Init(union_tbl.Bytes, union_tbl.Pos)

    inner = bytes(stream.Payload(i) for i in range(stream.PayloadLength()))

    if topic == "V":
        _dispatch_vehicle_data(inner, coordinator)
    else:
        # alerts / errors / connectivity / metrics — out of scope-1; ack and
        # drop until the corresponding entity classes land.
        _LOGGER.debug(
            "tesla_telemetry: topic %s not handled (size=%d)", topic, len(inner)
        )

    return _build_ack(txid, topic_bytes)


def _dispatch_vehicle_data(
    inner: bytes, coordinator: "TeslaTelemetryCoordinator"
) -> None:
    try:
        payload = vdp.Payload()
        payload.ParseFromString(inner)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("tesla_telemetry: bad V payload: %s", err)
        return

    created_at: float | None = None
    if payload.HasField("created_at"):
        created_at = (
            payload.created_at.seconds + payload.created_at.nanos / 1e9
        )

    if payload.vin and payload.vin != coordinator.vin:
        _LOGGER.warning(
            "tesla_telemetry: payload vin %s != cert vin %s",
            payload.vin,
            coordinator.vin,
        )

    for datum in payload.data:
        try:
            name = vdp.Field.Name(datum.key)
        except ValueError:
            _LOGGER.debug("tesla_telemetry: unknown field id %s", datum.key)
            continue
        coordinator.async_publish(name, datum.value, created_at)


def _build_ack(txid: bytes, topic: bytes) -> bytes:
    """Construct an ack envelope echoing the incoming txid + topic."""
    b = flatbuffers.Builder(0)
    fbgen.FlatbuffersStreamAckStart(b)
    ack_body = fbgen.FlatbuffersStreamAckEnd(b)
    txid_off = b.CreateByteVector(txid)
    topic_off = b.CreateByteVector(topic)
    fbgen.FlatbuffersEnvelopeStart(b)
    fbgen.FlatbuffersEnvelopeAddTxid(b, txid_off)
    fbgen.FlatbuffersEnvelopeAddTopic(b, topic_off)
    fbgen.FlatbuffersEnvelopeAddMessageType(
        b, fbgen.Message.FlatbuffersStreamAck
    )
    fbgen.FlatbuffersEnvelopeAddMessage(b, ack_body)
    env = fbgen.FlatbuffersEnvelopeEnd(b)
    b.Finish(env)
    return bytes(b.Output())

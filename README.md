# Tesla Fleet Telemetry for Home Assistant

A custom Home Assistant integration that consumes Tesla's
[Fleet Telemetry](https://developer.tesla.com/docs/fleet-api/fleet-telemetry)
push stream (mTLS WebSocket, Protobuf-in-FlatBuffers wire format) and surfaces
the location, route and trip entities directly in Home Assistant.

This integration is intended to **complement** — not replace — the official
[`tesla_fleet`](https://www.home-assistant.io/integrations/tesla_fleet/)
integration. `tesla_fleet` continues to handle climate, charging, doors and
commands; this integration replaces the polled location / route entities with
real-time push data from the vehicle.

> Status: pre-release. This repository is being prepared for HACS submission.
> The integration is currently running in production for the author's own
> Model S, but the public install / configuration story is still being
> documented.

## Entities provided

Six entities, scoped to a single vehicle:

| Entity | Type |
| --- | --- |
| `device_tracker.<vehicle>_location_telemetry` | `device_tracker` |
| `device_tracker.<vehicle>_route_telemetry` | `device_tracker` |
| `sensor.<vehicle>_speed_telemetry` | `sensor` |
| `sensor.<vehicle>_distance_to_arrival_telemetry` | `sensor` |
| `sensor.<vehicle>_time_to_arrival_telemetry` | `sensor` |
| `sensor.<vehicle>_traffic_delay_telemetry` | `sensor` |

## Architecture overview

```
┌──────────┐  mTLS WSS   ┌────────┐  HTTP (X-Tesla-* headers)  ┌────────────────┐
│  Tesla   │ ──────────► │ nginx  │ ─────────────────────────► │ Home Assistant │
│ Vehicle  │             │ (mTLS  │                            │ /api/tesla_    │
│ (Hermes) │             │  term) │                            │ telemetry/ws   │
└──────────┘             └────────┘                            └────────────────┘
```

* **No sidecar service.** nginx terminates mTLS, then forwards the WebSocket
  upgrade to Home Assistant's own HTTP server. The integration registers an
  `HomeAssistantView` at `/api/tesla_telemetry/ws`.
* **OAuth via `application_credentials`.** Home Assistant Core handles the
  Tesla OAuth token lifecycle. The same partner `client_id` / `client_secret`
  used by `tesla_fleet` is reused here.
* **`Tesla.SS256` JWT signing.** Tesla's `fleet_telemetry_config_jws` endpoint
  requires a JWT signed with RFC 8235 Schnorr over P-256 with RFC 6979
  deterministic nonces. The signer lives in `crypto.py`.
* **FlatBuffers + Protobuf decoding.** Tesla wraps its Protobuf payloads in a
  FlatBuffers envelope. Generated bindings (and the reconstructed `.fbs`) live
  in `custom_components/tesla_telemetry/proto/`.

## Requirements

* Home Assistant 2024.12 or later
* A Tesla developer account with a registered partner application
* A publicly reachable nginx (or equivalent) endpoint that:
  * terminates mTLS using the Tesla-issued client certificate chain
  * proxies the WebSocket upgrade to Home Assistant
  * injects `X-Tesla-Proxy-Secret` and `X-Tesla-Verified-Vin` headers
* `cryptography>=42`, `protobuf>=4.25,<8`, `flatbuffers>=24` (declared in
  `manifest.json`; HA installs them automatically)

## Installation (HACS — once published)

1. In HACS → Integrations → ⋯ → **Custom repositories**, add
   `https://github.com/johnbr/ha-tesla-fleet-telemetry` as type *Integration*.
2. Install **Tesla Fleet Telemetry** from the HACS list.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Tesla Fleet Telemetry**
   and follow the OAuth flow.

## Manual installation

Copy `custom_components/tesla_telemetry` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

Detailed configuration (nginx config snippet, partner-account bootstrap,
OAuth credentials, proxy-secret rotation) will be added before the first
tagged release.

## License

TBD — a license will be added before the first tagged release.

## Disclaimer

This integration is not affiliated with, endorsed by, or sponsored by Tesla,
Inc. "Tesla" is a trademark of Tesla, Inc.

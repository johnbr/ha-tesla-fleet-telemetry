# Vendored Tesla Fleet Telemetry wire schemas

Tesla's telemetry stream uses a two-layer wire format:

1. **Outer envelope** — FlatBuffers `FlatbuffersEnvelope { txid, topic, message: Message (union of FlatbuffersStream | FlatbuffersStreamAck), message_id }`. Tesla does **not** publish the `.fbs` source; only the generated Go bindings are public. `schemas/tesla_envelope.fbs` is reconstructed from those bindings (see commit pin below).
2. **Inner payload** — Protobuf. The concrete message type depends on the envelope `topic`: `"V"` → `telemetry.vehicle_data.Payload`, `"alerts"` → `telemetry.vehicle_alerts.VehicleAlerts`, etc. The `.proto` files come unmodified from the upstream repo.

## Source provenance

| Source                                | Commit (pinned)                            |
| ------------------------------------- | ------------------------------------------ |
| `schemas/vehicle_*.proto`             | `teslamotors/fleet-telemetry@20df968d7a09e8ec4e8b0175e0cc1a6153c34b2e` (`protos/`) |
| `schemas/tesla_envelope.fbs`          | reconstructed from same commit (`messages/tesla/Flatbuffers*.go`) |

## Generated files (do not edit)

- `tesla_envelope_generated.py` — FlatBuffers Python bindings (`flatc --gen-onefile --python`)
- `vehicle_alert_pb2.py`, `vehicle_connectivity_pb2.py`, `vehicle_data_pb2.py`, `vehicle_error_pb2.py`, `vehicle_metric_pb2.py` — Protobuf Python bindings

## Regeneration

Pre-reqs: `flatc` ≥ 24, and a portable protoc that targets the protobuf runtime range we declare in `manifest.json` (currently `protobuf>=4.25,<8`). protoc 25.3 generates code compatible with that whole range — newer protoc versions emit a hard runtime check that pins to runtime ≥ protoc-major.

```bash
cd custom_components/tesla_telemetry/proto

# 1. Refresh source schemas (only when bumping the pinned commit)
SHA=20df968d7a09e8ec4e8b0175e0cc1a6153c34b2e
for f in vehicle_alert vehicle_connectivity vehicle_data vehicle_error vehicle_metric; do
  curl -sSfL -o "schemas/${f}.proto" \
    "https://raw.githubusercontent.com/teslamotors/fleet-telemetry/${SHA}/protos/${f}.proto"
done
# tesla_envelope.fbs is hand-maintained: re-verify against
# messages/tesla/Flatbuffers{Envelope,Stream,StreamAck,Message}.go at the new SHA.

# 2. Regenerate
rm -f tesla_envelope_generated.py vehicle_*_pb2.py
flatc --python --gen-onefile -o . schemas/tesla_envelope.fbs
protoc --python_out=. -Ischemas schemas/vehicle_*.proto   # use protoc 25.3 binary

# 3. Smoke-test: in a venv with `flatbuffers` and `protobuf>=4.25` installed,
# build an envelope with a known Payload, parse it back, decode the inner
# protobuf, and confirm fields round-trip. The integration's own receiver
# tests cover this once they exist.
```

## Why a reconstructed `.fbs`?

The Go bindings (`messages/tesla/Flatbuffers*.go`) are public and stable, but the FlatBuffers source schema is not. `tesla_envelope.fbs` is line-for-line consistent with those bindings: vtable offsets, field types, and the `Message` union enum IDs (`FlatbuffersStream=4`, `FlatbuffersStreamAck=5`, with three placeholder reserved members at 1–3). Any divergence from the upstream Go file breaks wire compatibility — re-verify if upstream changes the bindings.

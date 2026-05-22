# Tesla Fleet Telemetry for Home Assistant

A custom Home Assistant integration that consumes Tesla's
[Fleet Telemetry](https://developer.tesla.com/docs/fleet-api/fleet-telemetry)
push stream — an mTLS WebSocket carrying Protobuf-in-FlatBuffers frames — and
surfaces the vehicle's live state as Home Assistant entities.

Unlike a polled integration, the vehicle *pushes* updates: location and speed
arrive every few seconds while the car is awake, and discrete events (doors,
locks, charging state) arrive the moment they change.

> **This is an advanced integration.** Tesla delivers Fleet Telemetry only to
> a publicly reachable endpoint that terminates mutual TLS. You must run a
> reverse proxy (nginx is assumed throughout this document) and hold a Tesla
> developer *partner* account. If you only want polled climate/charging/door
> data with no infrastructure to manage, use the official
> [`tesla_fleet`](https://www.home-assistant.io/integrations/tesla_fleet/)
> integration instead.

This integration **complements** `tesla_fleet`: run both side by side and the
telemetry entities give you a real-time mirror of the polled ones. It does not
send commands to the vehicle — it is read-only.

## Entities

One Home Assistant **device per vehicle**, carrying ~52 entities. Add the
integration once per VIN to track multiple cars.

| Platform | Count | Examples |
| --- | --- | --- |
| `device_tracker` | 2 | Location, Route (active nav destination) |
| `sensor` | 33 | Speed, State of charge, Charging state, Inside temperature, Odometer, Tire pressure ×4 |
| `binary_sensor` | 17 | Doors ×6, Windows ×4, Lock, Charging, Climate, Sentry armed, User present |

<details>
<summary>Full entity list</summary>

**Device trackers** — Location, Route

**Sensors** — Speed, Distance to arrival, Time to arrival, Traffic delay,
Odometer, Gear, Battery level, State of charge, Battery range, Rated range,
Charging state, Charge rate, AC charging power, DC charging power,
AC charge energy added, DC charge energy added, Charger current,
Charger voltage, Fast charger type, Charging cable, Charge limit,
Time to full charge, Inside temperature, Outside temperature,
Climate left setpoint, Climate right setpoint, Tire pressure (front left,
front right, rear left, rear right), Software version,
Software update download, Software update install

**Binary sensors** — Front/rear driver/passenger doors, Frunk, Trunk,
Front/rear driver/passenger windows, Lock, Charge port door, Charge cable,
Charging, Climate, Sentry armed, User present

</details>

Entity IDs follow the vehicle's name, e.g. `sensor.<vehicle>_speed`,
`device_tracker.<vehicle>_location`.

## How it works

```
┌──────────┐  mTLS WSS   ┌────────┐  HTTP (X-Tesla-* headers)  ┌────────────────┐
│  Tesla   │ ──────────► │ nginx  │ ─────────────────────────► │ Home Assistant │
│ Vehicle  │             │ (mTLS  │                            │ /api/tesla_    │
│ (Hermes) │             │  term) │                            │ telemetry/ws   │
└──────────┘             └────────┘                            └────────────────┘
```

* **No sidecar service.** nginx terminates mTLS, then forwards the WebSocket
  upgrade to Home Assistant's own HTTP server. The integration registers a
  view at `/api/tesla_telemetry/ws`.
* **OAuth via `application_credentials`.** Home Assistant Core owns the Tesla
  OAuth token lifecycle. Each integration instance gets its own grant — no
  refresh-token contention with `tesla_fleet`.
* **`Tesla.SS256` JWT signing.** Tesla's `fleet_telemetry_config_jws` endpoint
  requires a JWT signed with RFC 8235 Schnorr over P-256. The signer is
  implemented in `crypto.py`.
* **FlatBuffers + Protobuf decoding.** Tesla wraps Protobuf payloads in a
  FlatBuffers envelope; the generated bindings are vendored under `proto/`.

## Requirements

* Home Assistant **2024.12** or later.
* A **Tesla developer account** with a registered *partner application*
  ([developer.tesla.com](https://developer.tesla.com/)). North America
  accounts are supported today; EU/CN are not yet wired into the config flow.
* An **EC P-256 (secp256r1) partner key pair**, with the public key hosted at
  `https://<partner-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`.
* A **publicly reachable reverse proxy** (nginx assumed) that:
  * presents a valid TLS server certificate,
  * terminates mutual TLS, verifying the vehicle's client certificate against
    Tesla's fleet CA,
  * proxies the WebSocket upgrade to Home Assistant,
  * injects the `X-Tesla-Proxy-Secret` and `X-Tesla-Verified-Vin` headers.
* Python packages `cryptography`, `protobuf`, and `flatbuffers` — declared in
  `manifest.json` and installed automatically by Home Assistant.

## Setup

### 1. Tesla developer / partner application

1. Create an application at [developer.tesla.com](https://developer.tesla.com/).
   Note the **client ID** and **client secret**.
2. Add `https://my.home-assistant.io/redirect/oauth` to the application's
   **Allowed Redirect URIs** (Home Assistant's OAuth flow uses it).
3. Request the scopes `openid`, `offline_access`, and `vehicle_device_data`.
4. Generate the partner key pair and host its public half:
   ```sh
   openssl ecparam -genkey -name prime256v1 -noout -out partner.key
   openssl ec -in partner.key -pubout -out public-key.pem
   ```
   Publish `public-key.pem` at
   `https://<partner-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`.
   Keep `partner.key` — you paste it into Home Assistant later.

### 2. nginx reverse proxy

nginx terminates mutual TLS and forwards the WebSocket to Home Assistant.
Download Tesla's fleet CA (`config/files/prod_ca.crt` from
[`teslamotors/fleet-telemetry`](https://github.com/teslamotors/fleet-telemetry))
and use it as the client-certificate trust anchor.

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name tesla-telemetry.example.com;

    # Server certificate presented to the vehicle. Its chain must be
    # anchored in the CA bundle pushed as fleet_telemetry_config.ca — the
    # integration's default bundle covers Let's Encrypt and Sectigo/ZeroSSL.
    ssl_certificate     /etc/letsencrypt/live/tesla-telemetry.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tesla-telemetry.example.com/privkey.pem;

    # Mutual TLS: require and verify the vehicle's client certificate
    # against Tesla's fleet CA.
    ssl_client_certificate /etc/nginx/tesla/prod_ca.crt;
    ssl_verify_client on;
    ssl_verify_depth 2;

    location /api/tesla_telemetry/ws {
        proxy_pass http://127.0.0.1:8123/api/tesla_telemetry/ws;
        proxy_http_version 1.1;

        # WebSocket upgrade.
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;

        # Trusted only because ssl_verify_client succeeded above.
        proxy_set_header X-Tesla-Proxy-Secret  "REPLACE-WITH-YOUR-PROXY-SECRET";
        proxy_set_header X-Tesla-Verified-Vin  $ssl_client_s_dn;

        # Telemetry streams are long-lived.
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}
```

The proxy secret is any shared string; the integration generates one for you
in the config flow, and you paste that same value into the `proxy_set_header`
line above.

### 3. Install the integration

Until this repository is published in the default HACS store, add it as a
custom repository:

1. HACS → ⋯ → **Custom repositories** → add
   `https://github.com/johnbr/ha-tesla-fleet-telemetry` as type *Integration*.
2. Install **Tesla Fleet Telemetry**.
3. Restart Home Assistant.

(Manual install: copy `custom_components/tesla_telemetry` into your Home
Assistant `config/custom_components/` directory and restart.)

### 4. Configure in Home Assistant

1. **Settings → Devices & Services → Application Credentials** — add the Tesla
   **client ID** and **client secret** from step 1.
2. **Settings → Devices & Services → Add Integration → Tesla Fleet Telemetry.**
3. Complete the Tesla OAuth login, then pick the **VIN** to track.
4. On the **endpoint** step, enter:
   * the public **hostname** and **port** nginx listens on,
   * the **partner domain** hosting your `.well-known` public key,
   * the **proxy secret** (used in the nginx config above),
   * the **partner private key** (`partner.key` contents from step 1).

### 5. Bootstrap and verify

1. Run the **`tesla_telemetry.bootstrap`** service once. It registers your
   partner domain with Tesla and pushes the initial telemetry configuration.
2. Run **`tesla_telemetry.get_telemetry_config`** and confirm `synced: true`
   in the response. Tesla's `synced` flag can lag several minutes behind a
   successful push.
3. Wake the vehicle (open the Tesla app) and watch the nginx access log for
   the telemetry vhost — a `Hermes/...` user agent connecting is the sign the
   car has picked up the configuration. Entities begin updating shortly after.

## Multiple vehicles

Add the integration again for each additional VIN. The endpoint settings
(hostname, proxy secret, partner key) are deployment-wide, so the endpoint
step is pre-filled from your first vehicle — just confirm it. Each VIN becomes
its own Home Assistant device with its own entities. Service calls take an
`entry_id` to select the vehicle when more than one is configured.

## Services

| Service | Purpose |
| --- | --- |
| `tesla_telemetry.bootstrap` | One-time onboarding: register the partner domain and push the initial config. |
| `tesla_telemetry.resync_telemetry_config` | Re-push the config. Runs automatically (the config's `exp` is ~30 days); rarely needed by hand. |
| `tesla_telemetry.get_telemetry_config` | Fetch Tesla's current config for the VIN — check `synced`. |
| `tesla_telemetry.dump_public_key` | Emit the partner public key PEM, ready to host at `.well-known`. |
| `tesla_telemetry.set_interval_preset` | Switch streaming intervals between `default` and `high_rate` (~1 s location/speed). |

The telemetry configuration expires on Tesla's side after roughly 30 days. The
integration checks daily and re-pushes automatically when the last sync is
more than 7 days old, so no manual upkeep is required.

## Troubleshooting

* **`synced: false` for a long time.** Tesla's flag lags reality. If the nginx
  access log shows a `Hermes/...` connection, the car has the config.
* **TLS handshake failures in the nginx error log.** The vehicle is rejecting
  nginx's server certificate. Make sure the certificate chain is anchored in
  the CA bundle pushed as `fleet_telemetry_config.ca` — pass a `ca_pem`
  override to `bootstrap`/`resync_telemetry_config` if you use a CA outside
  the default bundle.
* **nginx connects but no entities update.** Check the `X-Tesla-Proxy-Secret`
  header in the nginx config matches the proxy secret stored in the config
  entry, and that `X-Tesla-Verified-Vin` carries the certificate subject.
* Enable debug logging with:
  ```yaml
  logger:
    logs:
      custom_components.tesla_telemetry: debug
  ```

## License

[MIT](LICENSE).

## Disclaimer

This integration is not affiliated with, endorsed by, or sponsored by Tesla,
Inc. "Tesla" is a trademark of Tesla, Inc.

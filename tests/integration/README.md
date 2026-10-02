# Integration tests (live Asterisk)

`tests/integration/test_asterisk_live.py` talks to a **running** DIAL Asterisk container
(`deploy/asterisk`) over ARI. The whole module is skipped unless `DIAL_INTEGRATION=1`, so the normal
unit-test run (`pytest apps`) is unaffected and needs no network.

## Run

```sh
# 1) bring up db + dial + asterisk (see deploy/asterisk/docker-compose.example.yml), migrate, and
#    sync at least one SIP device from DIAL (approve an extension with a device, or
#    POST /api/v1/pbx/resync/?event=demo as orga)

# 2) point the tests at ARI (host port 8088 published by the container)
DIAL_INTEGRATION=1 \
ASTERISK_ARI_URL=http://127.0.0.1:8088/ari \
ASTERISK_ARI_USER=dial ASTERISK_ARI_PASSWORD=dial \
DIAL_TEST_ENDPOINT=demo-aaaa \
.venv/bin/python -m pytest tests/integration -p no:cacheprovider -v
```

| Variable | Default | Meaning |
|---|---|---|
| `DIAL_INTEGRATION` | – | must be `1`, otherwise everything is skipped |
| `ASTERISK_ARI_URL` | `http://127.0.0.1:8088/ari` | ARI base URL (same variable DIAL itself uses) |
| `ASTERISK_ARI_USER` / `ASTERISK_ARI_PASSWORD` | `dial` / `dial` | ARI credentials (= container `ARI_USER` / `ARI_PASSWORD`) |
| `DIAL_TEST_ENDPOINT` | – | `sip_username` of a device DIAL has synced; without it the test only requires *some* PJSIP endpoint to be visible |
| `DIAL_TEST_CONTEXT` + `DIAL_AMI_HOST` (`DIAL_AMI_PORT`, `DIAL_AMI_USER`, `DIAL_AMI_PASSWORD`) | – | optional: verify over AMI that `[dial-<slug>]` is loaded with `switch => Realtime/@` |
| `DIAL_INTEGRATION_TIMEOUT` | `5` | HTTP timeout in seconds |

## What is checked

1. `GET /ari/asterisk/info` answers with a version >= 18 and a startup time (container healthy).
2. Wrong ARI credentials are rejected (401).
3. `GET /ari/endpoints/PJSIP` lists the realtime endpoints - proves the ODBC realtime chain
   (`res_odbc` → `res_config_odbc` → sorcery `ps_endpoints`) works and DIAL's `sync_device` rows are read.
4. If Django is configured with `DIAL_PBX_BACKEND=apps.pbx.backends.asterisk.AsteriskPBX`,
   `get_pbx().health()["ok"]` is `True` (exercises `apps.pbx.ari.ARIClient` for real).
5. Optionally the event shell context over AMI.

Placing a real SIP call is intentionally out of scope here - use a softphone as described in
`deploy/asterisk/README.md` ("Testing with a softphone").

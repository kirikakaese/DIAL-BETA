# Integration tests (live Asterisk)

`tests/integration/test_asterisk_live.py` talks to a **running** PET Asterisk container
(`deploy/asterisk`) over ARI. The whole module is skipped unless `PET_INTEGRATION=1`, so the normal
unit-test run (`pytest apps`) is unaffected and needs no network.

## Run

```sh
# 1) bring up db + pet + asterisk (see deploy/asterisk/docker-compose.example.yml), migrate, and
#    sync at least one SIP device from PET (approve an extension with a device, or
#    POST /api/v1/pbx/resync/?event=demo as orga)

# 2) point the tests at ARI (host port 8088 published by the container)
PET_INTEGRATION=1 \
ASTERISK_ARI_URL=http://127.0.0.1:8088/ari \
ASTERISK_ARI_USER=pet ASTERISK_ARI_PASSWORD=pet \
PET_TEST_ENDPOINT=demo-aaaa \
.venv/bin/python -m pytest tests/integration -p no:cacheprovider -v
```

| Variable | Default | Meaning |
|---|---|---|
| `PET_INTEGRATION` | – | must be `1`, otherwise everything is skipped |
| `ASTERISK_ARI_URL` | `http://127.0.0.1:8088/ari` | ARI base URL (same variable PET itself uses) |
| `ASTERISK_ARI_USER` / `ASTERISK_ARI_PASSWORD` | `pet` / `pet` | ARI credentials (= container `ARI_USER` / `ARI_PASSWORD`) |
| `PET_TEST_ENDPOINT` | – | `sip_username` of a device PET has synced; without it the test only requires *some* PJSIP endpoint to be visible |
| `PET_TEST_CONTEXT` + `PET_AMI_HOST` (`PET_AMI_PORT`, `PET_AMI_USER`, `PET_AMI_PASSWORD`) | – | optional: verify over AMI that `[pet-<slug>]` is loaded with `switch => Realtime/@` |
| `PET_INTEGRATION_TIMEOUT` | `5` | HTTP timeout in seconds |

## What is checked

1. `GET /ari/asterisk/info` answers with a version >= 18 and a startup time (container healthy).
2. Wrong ARI credentials are rejected (401).
3. `GET /ari/endpoints/PJSIP` lists the realtime endpoints - proves the ODBC realtime chain
   (`res_odbc` → `res_config_odbc` → sorcery `ps_endpoints`) works and PET's `sync_device` rows are read.
4. If Django is configured with `PET_PBX_BACKEND=apps.pbx.backends.asterisk.AsteriskPBX`,
   `get_pbx().health()["ok"]` is `True` (exercises `apps.pbx.ari.ARIClient` for real).
5. Optionally the event shell context over AMI.

Placing a real SIP call is intentionally out of scope here - use a softphone as described in
`deploy/asterisk/README.md` ("Testing with a softphone").

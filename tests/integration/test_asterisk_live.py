"""Live checks against the reference Asterisk container (``deploy/asterisk``).

Skipped unless ``PET_INTEGRATION=1``. Talks to ARI only (no SIP), using ``requests`` directly so
these tests do not depend on Django settings or a database.

Environment:

- ``ASTERISK_ARI_URL``       base URL incl. ``/ari`` (default ``http://127.0.0.1:8088/ari``)
- ``ASTERISK_ARI_USER`` / ``ASTERISK_ARI_PASSWORD``   (default ``pet`` / ``pet``)
- ``PET_TEST_ENDPOINT``      optional ``sip_username`` of a device PET has synced; when unset the
  test only requires that *some* realtime endpoint is visible to PJSIP
- ``PET_TEST_CONTEXT``       optional event context (``pet-<slug>``) expected to exist in the dialplan
- ``PET_AMI_HOST`` / ``PET_AMI_PORT`` / ``PET_AMI_USER`` / ``PET_AMI_PASSWORD``  optional; when
  ``PET_AMI_HOST`` is set the dialplan context check runs over AMI
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("PET_INTEGRATION") != "1",
                                reason="set PET_INTEGRATION=1 to run live Asterisk tests")

requests = pytest.importorskip("requests")

ARI_URL = os.environ.get("ASTERISK_ARI_URL", "http://127.0.0.1:8088/ari").rstrip("/")
ARI_AUTH = (os.environ.get("ASTERISK_ARI_USER", "pet"), os.environ.get("ASTERISK_ARI_PASSWORD", "pet"))
TIMEOUT = float(os.environ.get("PET_INTEGRATION_TIMEOUT", "5"))


def ari_get(path: str):
    resp = requests.get(f"{ARI_URL}/{path.lstrip('/')}", auth=ARI_AUTH, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="module")
def info():
    try:
        return ari_get("/asterisk/info")
    except requests.RequestException as exc:  # pragma: no cover - live only
        pytest.fail(f"ARI at {ARI_URL} not reachable: {exc}")


def test_ari_health(info):
    assert info["system"]["version"], "no Asterisk version in /asterisk/info"
    assert info["status"]["startup_time"]
    major = int(str(info["system"]["version"]).split(".")[0])
    assert major >= 18, f"Asterisk {info['system']['version']} too old for func_json/pbx_realtime features"


def test_ari_rejects_bad_credentials():
    resp = requests.get(f"{ARI_URL}/asterisk/info", auth=("nobody", "wrong"), timeout=TIMEOUT)
    assert resp.status_code == 401


def test_pjsip_module_and_realtime_endpoints_visible():
    endpoints = ari_get("/endpoints/PJSIP")
    assert isinstance(endpoints, list)
    wanted = os.environ.get("PET_TEST_ENDPOINT")
    resources = {e["resource"] for e in endpoints}
    if wanted:
        assert wanted in resources, f"synced endpoint {wanted!r} not visible to PJSIP (got {sorted(resources)})"
        detail = ari_get(f"/endpoints/PJSIP/{wanted}")
        assert detail["technology"] == "PJSIP" and detail["state"] in ("online", "offline", "unknown")
    else:
        assert resources, ("no PJSIP endpoints visible - sync a device from PET (or set PET_TEST_ENDPOINT) "
                           "and check `odbc show` / sorcery.conf in the container")


def test_pet_backend_health_if_configured():
    """When Django is configured for the Asterisk backend, its health() must agree with ARI."""
    if os.environ.get("DJANGO_SETTINGS_MODULE") is None:
        pytest.skip("DJANGO_SETTINGS_MODULE not set")
    django = pytest.importorskip("django")
    django.setup()
    from django.conf import settings

    if not settings.PET_PBX_BACKEND.endswith("AsteriskPBX"):
        pytest.skip("PET_PBX_BACKEND is not the Asterisk backend")
    from apps.pbx import get_pbx, reset_pbx_cache

    reset_pbx_cache()
    health = get_pbx().health()
    assert health["ok"] is True, health
    assert health["backend"] == "asterisk"


def test_event_context_loaded_via_ami():
    host = os.environ.get("PET_AMI_HOST")
    context = os.environ.get("PET_TEST_CONTEXT")
    if not host or not context:
        pytest.skip("set PET_AMI_HOST and PET_TEST_CONTEXT to verify the shell context over AMI")
    from apps.pbx.ami import AMIClient

    with AMIClient(host, int(os.environ.get("PET_AMI_PORT", "5038")),
                   os.environ.get("PET_AMI_USER", "pet"), os.environ.get("PET_AMI_PASSWORD", "pet")) as ami:
        resp = ami.action("Command", Command=f"dialplan show {context}")
    assert resp.get("Response") == "Success", resp
    out = resp.get("Output", "") + resp.get("_raw", "")
    assert "Realtime/@" in out, f"context {context} has no realtime switch:\n{out}"

"""Unauthenticated-by-design endpoints under ``/prov/``: phone autoprovisioning and the GSM core hook.

* ``GET /prov/<token>/linphone.xml``     - Linphone remote-provisioning XML (QR: ``linphone-config:<url>``).
* ``GET /prov/<token>/acrobits.xml``     - Acrobits/Groundwire ``<account>`` XML (QR: the URL itself).
* ``GET /prov/<token>/phonebook.xml``    - remote phonebook (vendor XML picked from the device's profile,
  ``?vendor=`` overrides, ``?q=`` filters); the event-wide directory token never appears in a config file.
* ``GET /prov/<token>/<filename>``       - the provisioning token is the secret (no login; phones can't).
* ``GET /prov/<vendor>/<mac>.cfg|.xml``  - MAC-based lookup for phones that only know their MAC; the
  request must still carry ``?token=`` or HTTP Basic ``sip_username:sip_password``. Credentials are
  never served on the MAC alone.
* ``POST /prov/gsm/register/``           - ``X-DIAL-PBX-Secret`` protected hook for OsmoHLR & co.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json

from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.events.models import Event

from . import services, softphone
from .services import GSMRegisterError

MAC_AUTH_HELP = (
    "DIAL provisioning: fetching a config by MAC address requires either ?token=<provisioning token> "
    "or HTTP Basic auth with the device's SIP username and password. The per-device URL shown on the "
    "device page (/prov/<token>/<filename>) needs no extra authentication.\n"
)


def _hook_authorized(request, event=None) -> bool:
    from apps.pbx.api import hook_secret

    secret = hook_secret(event)
    given = request.headers.get("X-DIAL-PBX-Secret", "")
    return bool(secret) and hmac.compare_digest(str(given), str(secret))


def _config_response(device) -> HttpResponse:
    body, content_type = services.render_provisioning(device)
    resp = HttpResponse(body, content_type=content_type)
    resp["Cache-Control"] = "no-store"
    resp["X-Robots-Tag"] = "noindex"
    return resp


@never_cache
@require_GET
def by_token(request, token, filename):
    device = services.device_for_provisioning_token(token)
    if device is None or not services.filename_matches(device, filename):
        raise Http404
    return _config_response(device)


@never_cache
@require_GET
def softphone_xml(request, token, client):
    """Softphone provisioning document; ``client`` is ``linphone`` or ``acrobits`` (fixed by the URL route)."""
    device = services.device_for_softphone_token(token)
    if device is None or client not in softphone.RENDERERS:
        raise Http404
    body, content_type = softphone.render_softphone(device, client)
    resp = HttpResponse(body, content_type=content_type)
    resp["Cache-Control"] = "no-store"
    resp["X-Robots-Tag"] = "noindex"
    return resp


@never_cache
@require_GET
def phonebook_xml(request, token):
    """Remote phonebook for a provisioned phone; the device's provisioning token is the credential."""
    from apps.phonebook import remote

    device = services.device_for_directory_token(token)
    if device is None or not remote.servable(device.event):
        raise Http404
    vendor = remote.vendor_for_device(device, request.GET.get("vendor"))
    body = remote.render_directory(device.event, vendor, q=remote.search_term(request.GET))
    resp = HttpResponse(body, content_type=remote.CONTENT_TYPE)
    resp["Cache-Control"] = "no-store"
    resp["X-Robots-Tag"] = "noindex"
    return resp


def _basic_auth(request) -> tuple[str, str]:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return "", ""
    user, _, pw = decoded.partition(":")
    return user, pw


def _unauthorized() -> HttpResponse:
    resp = HttpResponse(MAC_AUTH_HELP, content_type="text/plain", status=401)
    resp["WWW-Authenticate"] = 'Basic realm="DIAL provisioning"'
    return resp


@never_cache
@require_GET
def by_mac(request, vendor, mac, ext):
    token = request.GET.get("token", "")
    username, password = _basic_auth(request)
    if not token and not (username and password):
        return _unauthorized()
    device = services.device_for_mac(vendor, mac)
    if device is None:
        raise Http404
    if not services.check_provisioning_auth(device, token=token, username=username, password=password):
        return _unauthorized()
    return _config_response(device)


def _payload(request) -> dict:
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {k: request.POST.get(k, "") for k in ("event", "token", "imsi", "msisdn")}


@csrf_exempt
@require_POST
def gsm_register(request):
    """``{event, token, imsi, msisdn?}`` -> bind the SIM to the device holding ``token``."""
    data = _payload(request)
    event = Event.objects.filter(slug=str(data.get("event") or "")).first()
    if not _hook_authorized(request, event):
        return JsonResponse({"registered": False, "detail": "invalid PBX secret"}, status=401)
    if event is None:
        return JsonResponse({"registered": False, "detail": "unknown event"}, status=400)
    if not event.has_gsm:
        return JsonResponse({"registered": False, "detail": "GSM is not enabled for this event"}, status=400)
    try:
        device = services.gsm_register(event, str(data.get("token") or ""), str(data.get("imsi") or ""),
                                       str(data.get("msisdn") or ""))
    except GSMRegisterError as exc:
        return JsonResponse({"registered": False, "detail": str(exc)}, status=404)
    ext = device.primary_extension
    return JsonResponse({
        "registered": True, "device": str(device.pk), "imsi": device.imsi, "msisdn": device.msisdn,
        "extension": ext.number if ext else None, "generations": device.gsm_generations,
    })

"""Early-access gate (DIAL_EARLY_ACCESS_PASSWORD): browsers need the shared password, machines keep working."""
import pytest
from django.test import override_settings

from apps.accounts.models import ServiceAccount
from apps.core import early_access
from apps.core.a11y import check_html

GATE = override_settings(DIAL_EARLY_ACCESS_PASSWORD="open-sesame")


def unlock(client, pw="open-sesame", next_url="/"):
    return client.post("/early-access/", {"password": pw, "next": next_url})


@pytest.mark.django_db
def test_off_by_default(client):
    assert not early_access.enabled()
    assert client.get("/early-access/").status_code == 302
    assert client.get("/accounts/login/").status_code == 200


@pytest.mark.django_db
@GATE
def test_pages_need_the_password(client):
    r = client.get("/accounts/register/")
    assert r.status_code == 302 and r["Location"].startswith("/early-access/?next=/accounts/register/")
    page = client.get("/early-access/?next=/accounts/register/")
    assert page.status_code == 200 and check_html(page.content.decode()) == []
    r = unlock(client, "wrong")
    assert r.status_code == 401 and "not correct" in r.content.decode()
    r = unlock(client, next_url="/accounts/register/")
    assert r["Location"] == "/accounts/register/" and r.cookies[early_access.COOKIE]["httponly"]
    assert client.get("/accounts/register/").status_code == 200


@pytest.mark.django_db
@GATE
def test_api_is_gated_but_tokens_work(client, user):
    assert client.get("/api/v1/health/").status_code == 401
    _acct, raw = ServiceAccount.issue(name="bot", owner=user)
    assert client.get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 200
    # a made-up token passes the gate but is rejected by the API itself
    assert client.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer dial_fake").status_code in (401, 403)


@pytest.mark.django_db
@GATE
def test_machines_are_not_gated(client):
    # phones, Asterisk hooks and the remote phonebook reach their views (which check their own secrets)
    assert client.get("/prov/unknown-token/phone.cfg").status_code != 302
    assert client.get("/e/demo/phonebook/remote/bad-token/snom.xml").status_code != 302
    r = client.post("/api/v1/pbx/hooks/register/", {}, HTTP_X_DIAL_PBX_SECRET="wrong")
    assert r.status_code != 302 and b"early access" not in r.content
    assert client.get("/manifest.webmanifest").status_code != 302



@pytest.mark.django_db
@GATE
def test_federation_directory_is_gated(client):
    # it lists event names, so it stays private until launch (peers cannot fetch it meanwhile)
    assert client.get("/api/v1/federation/directory/").status_code == 401
    unlock(client)
    assert client.get("/api/v1/federation/directory/").status_code == 200


@pytest.mark.django_db
@GATE
def test_password_change_and_next_safety(client):
    unlock(client)
    assert client.get("/accounts/login/").status_code == 200
    with override_settings(DIAL_EARLY_ACCESS_PASSWORD="new-password"):
        assert client.get("/accounts/login/").status_code == 302
        r = unlock(client, "new-password", next_url="https://evil.example.org/")
        assert r["Location"] == "/"
    assert not early_access.cookie_valid("garbage")
    assert not early_access.cookie_valid(None)

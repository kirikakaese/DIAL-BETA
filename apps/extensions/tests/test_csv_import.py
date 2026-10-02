"""CSV import of extensions/users: parsing, preview, apply, API, portal flow."""
import base64
import json

import pytest
from django.core import mail
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.events.models import EventMembership
from apps.extensions import csv_import, services
from apps.extensions.models import Extension

pytestmark = pytest.mark.django_db

HEADER = "number,type,display_name,email,username,description,location,in_phonebook,group,role"


def _csv(*rows, header=HEADER, delim=","):
    body = "\n".join([header] + list(rows))
    return body.replace(",", delim) if delim != "," else body


# --------------------------------------------------------------------------- parsing

def test_parse_comma_and_semicolon_and_bom():
    comma = csv_import.parse_csv(_csv("4242,dect,Alice,alice@example.org,,,,yes,,"))
    semi = csv_import.parse_csv("\ufeff" + _csv("4242;dect;Alice;alice@example.org;;;;yes;;", delim=";"))
    for rows in (comma, semi):
        assert len(rows) == 1
        r = rows[0]
        assert (r.number, r.type, r.display_name, r.email, r.in_phonebook) == ("4242", "dect", "Alice",
                                                                               "alice@example.org", True)
        assert r.errors == []


def test_parse_bytes_with_bom_and_case_insensitive_headers():
    data = "\ufeffNumber;Type;E-Mail;Location Hint;Public;Foo\n4711;SIP;Bob@Example.org;Hall 1;0;bar\n".encode()
    rows = csv_import.parse_csv(data)
    assert rows.unknown_columns == ["foo"]
    r = rows[0]
    assert (r.number, r.type, r.email, r.location, r.in_phonebook) == ("4711", "sip", "bob@example.org", "Hall 1",
                                                                       False)


@pytest.mark.parametrize("raw,expected", [("yes", True), ("1", True), ("true", True), ("TRUE", True),
                                          ("no", False), ("0", False), ("false", False), ("", None)])
def test_parse_booleans(raw, expected):
    rows = csv_import.parse_csv(f"number,in_phonebook\n4242,{raw}\n")
    assert rows[0].in_phonebook is expected


def test_parse_row_level_errors_and_defaults():
    rows = csv_import.parse_csv("number,type,in_phonebook,role,email\n4242,,,,\n4243,hoverboard,maybe,king,nope\n")
    assert rows[0].type == "dect" and rows[0].errors == []
    errs = " ".join(rows[1].errors)
    assert "hoverboard" in errs and "in_phonebook" in errs and "king" in errs and "nope" in errs


def test_parse_requires_number_column_and_content():
    with pytest.raises(csv_import.CSVImportError):
        csv_import.parse_csv("type,email\ndect,a@b.c\n")
    with pytest.raises(csv_import.CSVImportError):
        csv_import.parse_csv("\n\n")


def test_sample_csv_round_trips():
    rows = csv_import.parse_csv(csv_import.sample_csv())
    assert [r.number for r in rows] == ["4242", "4300"]
    assert rows.unknown_columns == []


# --------------------------------------------------------------------------- preview

def test_preview_actions(event, user, orga, angels):
    services.register(event, user, "4242", "dect")
    rows = csv_import.parse_csv(_csv(
        "4242,dect,,alice@example.org,,,,,,",  # taken
        "4243,dect,,alice@example.org,,,,,,",  # ok, existing user
        "4244,dect,,new@example.org,newbie,,,,angels,",  # needs create_users
        "9100,dect,,alice@example.org,,,,,,",  # blocked range
        "112,dect,,alice@example.org,,,,,,",  # emergency
        "4245,dect,,,,,,,,",  # ownerless
        "4243,dect,,alice@example.org,,,,,,",  # duplicate in file
        "4246,dect,,alice@example.org,,,,,nope,",  # unknown group
    ))
    plan = csv_import.preview(event, rows)
    actions = [r.action for r in plan.rows]
    assert actions == ["skip-taken", "create", "error", "error", "error", "create", "error", "error"]
    assert plan.rows[2].create_user is False and "create missing users" in plan.rows[2].messages[0]
    assert plan.rows[5].ownerless is True
    assert "Duplicate" in plan.rows[6].messages[0]
    assert "nope" in plan.rows[7].messages[0]

    plan = csv_import.preview(event, rows, create_users=True, allow_ownerless=False)
    assert plan.rows[2].action == "create" and plan.rows[2].create_user and plan.rows[2].new_username == "newbie"
    assert plan.rows[5].action == "error"
    assert plan.new_users == 1 and plan.creatable == 2 and plan.skipped == 1


def test_preview_restricted_range_soft_for_new_members(event, user):
    rows = csv_import.parse_csv("number,email,role\n1234,alice@example.org,\n1235,alice@example.org,orga\n")
    plan = csv_import.preview(event, rows)
    assert plan.rows[0].action == "error"  # plain user, no membership change
    assert plan.rows[1].action == "create" and "Restricted" in plan.rows[1].messages[0]


# --------------------------------------------------------------------------- apply

def test_apply_creates_users_memberships_groups_and_extensions(event, orga, angels, user):
    rows = csv_import.parse_csv(_csv(
        "4242,dect,Alice,alice@example.org,,Infodesk,Hall 2,yes,angels,helpdesk",
        "4243,sip,,carol@example.org,carol,,,no,angels,",
        "4244,announcement,Welcome,,,,,,,",
    ))
    result = csv_import.apply(event, rows, orga, create_users=True)
    assert result.applied == 3 and result.errors == []
    assert [str(u.email) for u in result.users_created] == ["carol@example.org"]

    carol = User.objects.get(email="carol@example.org")
    assert carol.username == "carol" and not carol.has_usable_password() and not carol.email_verified
    m = EventMembership.objects.get(event=event, user=carol)
    assert m.role == "user" and list(m.groups.values_list("slug", flat=True)) == ["angels"]
    alice_m = EventMembership.objects.get(event=event, user=user)
    assert alice_m.role == "helpdesk" and alice_m.groups.filter(slug="angels").exists()

    e1 = Extension.objects.get(event=event, number="4242")
    assert e1.owner == user and e1.state == "active" and e1.location_hint == "Hall 2" and e1.in_phonebook
    e2 = Extension.objects.get(event=event, number="4243")
    assert e2.owner == carol and e2.type == "sip" and e2.in_phonebook is False and e2.display_name == "carol"
    e3 = Extension.objects.get(event=event, number="4244")
    assert e3.owner is None and e3.type == "announcement" and e3.display_name == "Welcome"

    # invitation mail for the new account
    assert result.mails_sent == 1 and len(mail.outbox) == 1
    assert mail.outbox[0].to == ["carol@example.org"] and "/accounts/password/reset/" in mail.outbox[0].body

    summary = AuditLog.objects.filter(event=event, target_id=str(event.pk), message__startswith="CSV import").get()
    assert summary.message == "CSV import: 3 extensions, 1 users" and summary.actor == orga


def test_apply_isolates_failing_rows(event, orga, user, monkeypatch):
    rows = csv_import.parse_csv(_csv(
        "4242,dect,,alice@example.org,,,,,,",
        "4243,dect,,alice@example.org,,,,,,",
        "4244,dect,,alice@example.org,,,,,,",
    ))
    real_register = services.register

    def flaky(event, owner, number, *a, **kw):
        if number == "4243":
            raise services.ExtensionError("boom")
        return real_register(event, owner, number, *a, **kw)

    monkeypatch.setattr(csv_import.services, "register", flaky)
    result = csv_import.apply(event, rows, orga)
    assert result.applied == 2
    assert [e["number"] for e in result.errors] == ["4243"] and result.errors[0]["message"] == "boom"
    assert set(Extension.objects.filter(event=event).values_list("number", flat=True)) == {"4242", "4244"}
    assert [r.action for r in result.plan.rows] == ["created", "error", "created"]


def test_apply_rolls_back_user_when_row_fails(event, orga):
    services.register(event, orga, "4242", "dect", force_active=True)
    rows = csv_import.parse_csv("number,email\n4242,dave@example.org\n")
    result = csv_import.apply(event, rows, orga, create_users=True)
    assert result.applied == 0 and result.plan.rows[0].action == "skip-taken"
    assert not User.objects.filter(email="dave@example.org").exists()
    assert mail.outbox == []


def test_apply_never_downgrades_roles(event, orga):
    rows = csv_import.parse_csv("number,email,role\n4242,orga@example.org,user\n")
    csv_import.apply(event, rows, orga)
    assert EventMembership.objects.get(event=event, user=orga).role == "orga"


# --------------------------------------------------------------------------- API

def test_api_import_dry_run_and_apply(event, orga, user):
    c = APIClient()
    c.force_authenticate(orga)
    body = {"csv": _csv("4242,dect,,alice@example.org,,,,,,", "9100,dect,,alice@example.org,,,,,,"),
            "dry_run": True, "create_users": False}
    r = c.post("/api/v1/extensions/import/?event=demo", body, format="json")
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["applied"] == 0 and data["dry_run"] is True
    assert [p["action"] for p in data["plan"]] == ["create", "error"]
    assert data["errors"][0]["number"] == "9100"
    assert not Extension.objects.filter(event=event).exists()

    body["dry_run"] = False
    r = c.post("/api/v1/extensions/import/?event=demo", body, format="json")
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["applied"] == 1 and data["plan"][0]["action"] == "created"
    assert Extension.objects.get(event=event, number="4242").owner == user


def test_api_import_permissions_and_validation(event, user, orga):
    c = APIClient()
    c.force_authenticate(user)
    assert c.post("/api/v1/extensions/import/?event=demo", {"csv": "number\n4242\n"}, format="json").status_code == 403
    c.force_authenticate(orga)
    assert c.post("/api/v1/extensions/import/?event=nope", {"csv": "number\n4242\n"}, format="json").status_code == 404
    assert c.post("/api/v1/extensions/import/?event=demo", {}, format="json").status_code == 400
    r = c.post("/api/v1/extensions/import/?event=demo", {"csv": "type\ndect\n"}, format="json")
    assert r.status_code == 400 and "number" in r.json()["detail"]


# --------------------------------------------------------------------------- portal

def test_portal_three_step_flow(client, event, orga, user, angels):
    client.force_login(orga)
    url = f"/e/{event.slug}/orga/import-csv/"
    r = client.get(url)
    assert r.status_code == 200 and b"Preview import" in r.content

    text = _csv("4242,dect,,alice@example.org,,,,,angels,", "4243,dect,,erin@example.org,erin,,,,,")
    r = client.post(url, {"step": "upload", "text": text, "create_users": "on"})
    assert r.status_code == 200
    html = r.content.decode()
    assert "Creates user erin" in html and 'name="step" value="apply"' in html
    b64 = base64.b64encode(text.encode()).decode()
    assert b64 in html and 'name="create_users" value="1"' in html
    assert not Extension.objects.filter(event=event).exists()

    r = client.post(url, {"step": "apply", "csv_b64": b64, "create_users": "1"})
    assert r.status_code == 200
    html = r.content.decode()
    assert "2 created" in html and "1 new users" in html
    assert Extension.objects.filter(event=event, state="active").count() == 2
    assert User.objects.filter(email="erin@example.org", username="erin").exists()
    assert EventMembership.objects.get(event=event, user=user).groups.filter(slug="angels").exists()


def test_portal_upload_file_and_errors(client, event, orga):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(orga)
    url = f"/e/{event.slug}/orga/import-csv/"
    r = client.post(url, {"step": "upload"})
    assert r.status_code == 200 and b"Upload a CSV file or paste CSV text." in r.content
    f = SimpleUploadedFile("n.csv", "\ufeffnumber;type\n4242;dect\n".encode(), content_type="text/csv")
    r = client.post(url, {"step": "upload", "file": f})
    assert r.status_code == 200 and b"4242" in r.content and b'value="apply"' in r.content
    r = client.post(url, {"step": "upload", "text": "type\ndect\n"})
    assert r.status_code == 200 and b"must contain a" in r.content


def test_portal_forbidden_for_plain_user_and_helpdesk(client, event, user, member):
    client.force_login(user)
    assert client.get(f"/e/{event.slug}/orga/import-csv/").status_code == 403
    assert client.get(f"/e/{event.slug}/orga/import-csv/sample.csv").status_code == 403
    member.role = "helpdesk"
    member.save()
    assert client.get(f"/e/{event.slug}/orga/import-csv/").status_code == 403


def test_portal_sample_download_and_links(client, event, orga):
    client.force_login(orga)
    r = client.get(f"/e/{event.slug}/orga/import-csv/sample.csv")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert "attachment" in r["Content-Disposition"]
    assert r.content.decode().splitlines()[0] == ",".join(csv_import.COLUMNS)
    r = client.get(f"/e/{event.slug}/orga/extensions/")
    assert f"/e/{event.slug}/orga/import-csv/".encode() in r.content


def test_json_serialisable_plan(event, user):
    rows = csv_import.parse_csv("number,email\n4242,alice@example.org\n")
    plan = csv_import.preview(event, rows)
    json.dumps(plan.as_list())

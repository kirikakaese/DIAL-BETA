"""Tests for the ``pet`` CLI: argument parsing, table rendering and one HTTP round-trip with
``requests`` mocked."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from apps.api import cli


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- parsing


def test_parser_extensions_list():
    args = cli.build_parser().parse_args(
        ["extensions", "list", "--event", "demo", "--state", "requested"]
    )
    assert args.command == "extensions"
    assert args.action == "list"
    assert args.event == "demo"
    assert args.state == "requested"
    assert args.func is cli.cmd_extensions


def test_parser_events_transition_validates_state():
    p = cli.build_parser()
    args = p.parse_args(["events", "transition", "demo", "live"])
    assert (args.slug, args.state) == ("demo", "live")
    with pytest.raises(SystemExit):
        p.parse_args(["events", "transition", "demo", "bogus"])


def test_parser_requires_event_for_queue():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["queue"])


def test_parser_global_options_and_format():
    args = cli.build_parser().parse_args(
        [
            "--url",
            "http://pet:8000",
            "--token",
            "pet_x",
            "--format",
            "csv",
            "phonebook",
            "--event",
            "demo",
        ]
    )
    assert args.url == "http://pet:8000"
    assert args.token == "pet_x"
    assert args.format == "csv"
    assert args.func is cli.cmd_phonebook


def test_make_client_uses_environment(monkeypatch):
    monkeypatch.setenv("PET_URL", "http://env.example/")
    monkeypatch.setenv("PET_TOKEN", "pet_env")
    args = cli.build_parser().parse_args(["health"])
    client = cli.make_client(args)
    assert client.base == "http://env.example"
    assert client.session.headers["Authorization"] == "Bearer pet_env"


# --------------------------------------------------------------------------- rendering


def test_render_table_and_csv():
    rows = [
        {"number": "4242", "name": "Alice", "ok": True},
        {"number": "1", "name": None, "ok": False},
    ]
    table = cli.render_table(rows, ["number", "name", "ok"])
    lines = table.splitlines()
    assert lines[0].startswith("number  name")
    assert "4242    Alice  yes" in table
    assert lines[3].startswith("1       -")  # None rendered as dash
    csv_out = cli.render_csv(rows, ["number", "name"])
    assert csv_out.splitlines() == ["number,name", "4242,Alice", "1,-"]


def test_render_table_empty():
    assert "(no results)" in cli.render_table([], ["a"])


# ------------------------------------------------------------ HTTP round trip (mocked)


def test_extensions_list_requests_and_paginates(capsys):
    pages = [
        FakeResponse(
            {
                "count": 3,
                "next": "http://x/?offset=2",
                "results": [
                    {
                        "id": "a",
                        "number": "4242",
                        "type": "sip",
                        "state": "active",
                        "owner": "alice",
                    },
                    {
                        "id": "b",
                        "number": "4711",
                        "type": "dect",
                        "state": "active",
                        "owner": "bob",
                    },
                ],
            }
        ),
        FakeResponse(
            {
                "count": 3,
                "next": None,
                "results": [
                    {
                        "id": "c",
                        "number": "1000",
                        "type": "dect",
                        "state": "active",
                        "owner": "orga",
                    },
                ],
            }
        ),
    ]
    with mock.patch("requests.Session.request", side_effect=pages) as req:
        rc = cli.main(
            [
                "--url",
                "http://pet.test",
                "--token",
                "pet_abc",
                "extensions",
                "list",
                "--event",
                "demo",
                "--state",
                "active",
            ]
        )
    assert rc == 0
    assert req.call_count == 2
    method, url = req.call_args_list[0].args
    assert method == "GET"
    assert url == "http://pet.test/api/v1/extensions/"
    params = req.call_args_list[0].kwargs["params"]
    assert params["event__slug"] == "demo"
    assert params["state"] == "active"
    assert params["offset"] == 0
    assert req.call_args_list[1].kwargs["params"]["offset"] == 2
    out = capsys.readouterr().out
    assert "4242" in out and "4711" in out and "1000" in out


def test_extensions_approve_posts_note():
    resp = FakeResponse({"id": "a", "number": "8001", "state": "active"})
    with mock.patch("requests.Session.request", return_value=resp) as req:
        rc = cli.main(["--url", "http://pet.test", "extensions", "approve", "a", "--note", "ok"])
    assert rc == 0
    method, url = req.call_args.args
    assert (method, url) == ("POST", "http://pet.test/api/v1/extensions/a/approve/")
    assert req.call_args.kwargs["json"] == {"note": "ok"}


def test_http_error_is_reported(capsys):
    resp = FakeResponse({"detail": "Only requested extensions can be approved."}, status=400)
    with mock.patch("requests.Session.request", return_value=resp):
        rc = cli.main(["--url", "http://pet.test", "extensions", "approve", "a"])
    assert rc == 2
    assert "HTTP 400" in capsys.readouterr().err


def test_resync_posts_event_query(capsys):
    resp = FakeResponse({"event": "demo", "synced": 12, "backend": "asterisk"})
    with mock.patch("requests.Session.request", return_value=resp) as req:
        rc = cli.main(["--url", "http://pet.test", "resync", "--event", "demo"])
    assert rc == 0
    assert req.call_args.args == ("POST", "http://pet.test/api/v1/pbx/resync/")
    assert req.call_args.kwargs["params"] == {"event": "demo"}
    assert "12 extensions synced to asterisk" in capsys.readouterr().out


def test_health_passes_event(capsys):
    resp = FakeResponse({"ok": True, "event": "demo", "pbx": {"ok": True}, "dect": {"ok": True}})
    with mock.patch("requests.Session.request", return_value=resp) as req:
        rc = cli.main(["--url", "http://pet.test", "health", "--event", "demo"])
    assert rc == 0
    assert req.call_args.args == ("GET", "http://pet.test/api/v1/health/")
    assert req.call_args.kwargs["params"] == {"event": "demo"}
    with mock.patch("requests.Session.request", return_value=resp) as req:
        cli.main(["--url", "http://pet.test", "health"])
    assert req.call_args.kwargs["params"] == {}


def test_pbx_connection_show_set_reset(capsys):
    state = {"event": "demo", "pbx": {"backend": "asterisk", "backend_label": "Asterisk",
                                       "ari_url": "http://10.1.1.5:8088/ari"}, "dect": None,
             "effective": {"pbx": "asterisk", "dect": "dummy"}}
    with mock.patch("requests.Session.request", return_value=FakeResponse(state)) as req:
        assert cli.main(["--url", "http://pet.test", "pbx", "connection", "show", "--event", "demo"]) == 0
    assert req.call_args.args == ("GET", "http://pet.test/api/v1/pbx/connection/")
    out = capsys.readouterr().out
    assert "pbx: Asterisk @ http://10.1.1.5:8088/ari" in out and "dect: server default (dummy)" in out

    with mock.patch("requests.Session.request", return_value=FakeResponse(state)) as req:
        rc = cli.main(["--url", "http://pet.test", "pbx", "connection", "set", "--event", "demo",
                       "--pbx", '{"backend": "asterisk", "ari_url": "http://10.1.1.5:8088/ari"}'])
    assert rc == 0
    assert req.call_args.args == ("PATCH", "http://pet.test/api/v1/pbx/connection/")
    assert req.call_args.kwargs["json"] == {"pbx": {"backend": "asterisk", "ari_url": "http://10.1.1.5:8088/ari"}}

    assert cli.main(["--url", "http://pet.test", "pbx", "connection", "set", "--event", "demo",
                     "--pbx", "not json"]) != 0

    with mock.patch("requests.Session.request", return_value=FakeResponse(state)) as req:
        cli.main(["--url", "http://pet.test", "pbx", "connection", "reset", "--event", "demo", "--part", "dect"])
    assert req.call_args.args == ("DELETE", "http://pet.test/api/v1/pbx/connection/")
    assert req.call_args.kwargs["params"] == {"event": "demo", "part": "dect"}


def test_parser_pages_list_and_show(capsys):
    p = cli.build_parser()
    args = p.parse_args(["pages", "list", "--event", "demo"])
    assert (args.command, args.action, args.event) == ("pages", "list", "demo")
    assert args.func is cli.cmd_pages
    args = p.parse_args(["pages", "show", "--event", "demo", "how-to-dect"])
    assert (args.action, args.slug) == ("show", "how-to-dect")
    with pytest.raises(SystemExit):
        p.parse_args(["pages", "list"])

    page = {"id": 1, "slug": "how-to-dect", "title": "DECT how-to", "order": 0, "published": True,
            "show_on_dashboard": True, "updated_at": "2026-01-01T10:00:00Z", "body": "# Hi\n\nDial *1*."}
    listing = FakeResponse({"count": 1, "next": None, "results": [page]})
    with mock.patch("requests.Session.request", return_value=listing) as req:
        assert cli.main(["--url", "http://pet.test", "pages", "show", "--event", "demo", "how-to-dect"]) == 0
    assert req.call_args.args == ("GET", "http://pet.test/api/v1/pages/")
    assert req.call_args.kwargs["params"]["event"] == "demo"
    out = capsys.readouterr().out
    assert "# DECT how-to" in out and "Dial *1*." in out
    with mock.patch("requests.Session.request", return_value=listing):
        assert cli.main(["--url", "http://pet.test", "pages", "show", "--event", "demo", "nope"]) == 2


def test_parser_events_schedule_and_patch(capsys):
    p = cli.build_parser()
    args = p.parse_args(["events", "schedule", "demo", "--live", "2030-08-12T10:00:00+02:00", "--clear"])
    assert (args.command, args.action, args.slug) == ("events", "schedule", "demo")
    assert args.live == "2030-08-12T10:00:00+02:00" and args.registration is None and args.clear is True
    assert args.func is cli.cmd_events
    with pytest.raises(SystemExit):
        p.parse_args(["events", "schedule"])

    ev = {"slug": "demo", "state": "registration", "registration_opens_at": None,
          "goes_live_at": "2030-08-12T08:00:00Z", "archives_at": None,
          "next_scheduled_transition": {"state": "live", "at": "2030-08-12T08:00:00+00:00"}}
    with mock.patch("requests.Session.request", return_value=FakeResponse(ev)) as req:
        rc = cli.main(["--url", "http://pet.test", "events", "schedule", "demo", "--clear",
                       "--live", "2030-08-12T10:00:00+02:00"])
    assert rc == 0
    assert req.call_args.args == ("PATCH", "http://pet.test/api/v1/events/demo/")
    assert req.call_args.kwargs["json"] == {"registration_opens_at": None, "archives_at": None,
                                            "goes_live_at": "2030-08-12T10:00:00+02:00"}
    out = capsys.readouterr().out
    assert "goes_live_at: 2030-08-12T08:00:00Z" in out and "next: live at" in out
    # neither a timestamp nor --clear -> usage error, no request
    with mock.patch("requests.Session.request") as req:
        assert cli.main(["--url", "http://pet.test", "events", "schedule", "demo"]) == 2
    assert req.call_count == 0


def test_parser_extensions_import_and_round_trip(tmp_path, capsys):
    p = cli.build_parser()
    args = p.parse_args(["extensions", "import", "--event", "demo", "--file", "n.csv", "--dry-run",
                         "--create-users"])
    assert (args.command, args.action, args.event, args.file) == ("extensions", "import", "demo", "n.csv")
    assert args.dry_run is True and args.create_users is True and args.func is cli.cmd_extensions
    args = p.parse_args(["extensions", "import", "--event", "demo", "--file", "n.csv"])
    assert args.dry_run is False and args.create_users is False
    with pytest.raises(SystemExit):
        p.parse_args(["extensions", "import", "--event", "demo"])

    csv_file = tmp_path / "numbers.csv"
    csv_file.write_text("\ufeffnumber;email\n4242;alice@example.org\n9100;bob@example.org\n", encoding="utf-8")
    plan = [{"line": 2, "number": "4242", "type": "dect", "owner": "alice@example.org", "ownerless": False,
             "action": "create", "messages": []},
            {"line": 3, "number": "9100", "type": "dect", "owner": None, "ownerless": False, "action": "error",
             "messages": ["This range is blocked: Services."]}]
    dry = FakeResponse({"plan": plan, "applied": 0, "dry_run": True, "unknown_columns": [],
                        "errors": [{"line": 3, "number": "9100", "action": "error", "message": "blocked"}]})
    with mock.patch("requests.Session.request", return_value=dry) as req:
        rc = cli.main(["--url", "http://pet.test", "extensions", "import", "--event", "demo",
                       "--file", str(csv_file), "--dry-run", "--create-users"])
    assert rc == 1  # one row failed
    assert req.call_args.args == ("POST", "http://pet.test/api/v1/extensions/import/")
    assert req.call_args.kwargs["params"] == {"event": "demo"}
    body = req.call_args.kwargs["json"]
    assert body["dry_run"] is True and body["create_users"] is True
    assert body["csv"].startswith("number;email")  # BOM stripped
    out = capsys.readouterr().out
    assert "4242" in out and "skip" not in out.split("\n")[0]
    assert "This range is blocked" in out and "dry run: 1 of 2 row(s) would be imported" in out

    applied = FakeResponse({"plan": [dict(plan[0], action="created")], "applied": 1, "users_created": 0,
                            "skipped": 0, "dry_run": False, "unknown_columns": ["foo"], "errors": []})
    with mock.patch("requests.Session.request", return_value=applied) as req:
        rc = cli.main(["--url", "http://pet.test", "extensions", "import", "--event", "demo",
                       "--file", str(csv_file)])
    assert rc == 0 and req.call_args.kwargs["json"]["dry_run"] is False
    out = capsys.readouterr().out
    assert "imported 1 extension(s), created 0 user(s), 0 row(s) skipped/failed" in out
    assert "ignored columns: foo" in out

    with mock.patch("requests.Session.request") as req:
        assert cli.main(["--url", "http://pet.test", "extensions", "import", "--event", "demo",
                         "--file", str(tmp_path / "missing.csv")]) == 2
    assert req.call_count == 0

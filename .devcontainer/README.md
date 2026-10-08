# DIAL in GitHub Codespaces

Try DIAL in the browser without a server: on the repository page click **Code → Codespaces → Create
codespace on main** (or on any branch).

The first start takes a few minutes: it installs the dependencies, creates a SQLite database and fills it
with the *Demo Camp* event (`.devcontainer/setup.sh`). Afterwards DIAL starts on port 8000 on every start
of the codespace (`.devcontainer/start.sh`) and the browser tab opens by itself. If it doesn't: **Ports**
tab → port 8000 → globe icon.

| Account | Password |
|---|---|
| `admin@dial.local` (superuser) | `admin` |
| the other demo users | `demo1234!` |

- The port is **private**: only you, logged in to GitHub, can open the link. Don't switch it to public.
- Phones, Asterisk and DECT are simulated (dummy backends); the LDAP phonebook does not run.
- Server log: `tail -f /tmp/dial.log`. Restart: `pkill -f "manage.py runserver"; bash .devcontainer/start.sh`.
- Start over with fresh demo data: `.venv/bin/python manage.py seed_demo --reset`.
- No Redis, no Celery worker: background jobs run immediately; e-mails appear in the server log.
- A stopped codespace keeps its data; GitHub stops it after 30 minutes without activity. Delete it under
  github.com/codespaces when you no longer need it.

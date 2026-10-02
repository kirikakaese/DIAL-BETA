# DIAL Ansible deployment

Installs Docker (official apt repo, Debian/Ubuntu), checks out DIAL into `/opt/dial/src`, renders
`.env` from inventory variables and runs the stack from `docker-compose.yml` with
`docker compose up -d --build`.

```sh
cp inventory.example.ini inventory.ini      # edit host, SECRET_KEY, passwords
ansible-playbook -i inventory.ini site.yml
```

Variables (see `roles/dial/defaults/main.yml`): `dial_repo_url`, `dial_repo_version`, `dial_dir`,
`dial_env` (dict rendered 1:1 into `.env`; keys/defaults mirror `.env.example`). Override any key
with `dial_env_extra`, e.g. `-e '{"dial_env_extra": {"OMM_HOST": "10.0.0.5"}}'`.

Afterwards: `http://<host>:8000` (`admin@dial.local` / `admin` while `DIAL_SEED_DEMO=1`), SIP on
5060/5061, RTP 10000-10200/udp. For a real event LAN switch the `asterisk` service to
`network_mode: host` in `docker-compose.yml` and put a TLS reverse proxy in front of `web`.

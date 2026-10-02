# PET Ansible deployment

Installs Docker (official apt repo, Debian/Ubuntu), checks out PET into `/opt/pet/src`, renders
`.env` from inventory variables and runs the stack from `docker-compose.yml` with
`docker compose up -d --build`.

```sh
cp inventory.example.ini inventory.ini      # edit host, SECRET_KEY, passwords
ansible-playbook -i inventory.ini site.yml
```

Variables (see `roles/pet/defaults/main.yml`): `pet_repo_url`, `pet_repo_version`, `pet_dir`,
`pet_env` (dict rendered 1:1 into `.env`; keys/defaults mirror `.env.example`). Override any key
with `pet_env_extra`, e.g. `-e '{"pet_env_extra": {"OMM_HOST": "10.0.0.5"}}'`.

Afterwards: `http://<host>:8000` (`admin@pet.local` / `admin` while `PET_SEED_DEMO=1`), SIP on
5060/5061, RTP 10000-10200/udp. For a real event LAN switch the `asterisk` service to
`network_mode: host` in `docker-compose.yml` and put a TLS reverse proxy in front of `web`.

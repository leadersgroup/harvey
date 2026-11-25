# Harvey on Railway

One container runs the dashboard, the heartbeat loop and a small Caddy proxy.
All state (config, `.env`, prompts, skills, `data/`) lives on one volume
mounted at `/data`.

## Service settings

| Setting | Value |
|---|---|
| Source | this repository, branch `railway` (uses `railway.json`) |
| Volume | mount path `/data` |
| Public domain | generate one; the dashboard is served there |

## Variables

| Variable | Purpose |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Pro/Max sign-in. Create it on your own computer with `claude setup-token`. No API key is used; `ANTHROPIC_API_KEY` is ignored on purpose. |
| `HEARTBEAT_MINUTES` | Minutes between cycles, applied to the config on every boot (30). |
| `HARVEY_AUTORUN` | `1` starts the heartbeat loop at boot, so it resumes after a redeploy. Leave `0` until the old install is stopped. |
| `BACKUP_GITHUB_REPO` | Private repository (`owner/name`) that receives a data snapshot two minutes after each start and every 24 hours (`BACKUP_HOURS`). |
| `BACKUP_GITHUB_TOKEN` | Fine-grained GitHub token with Contents read/write on that repository only. |
| `PUBLIC_ORIGIN` | Only for a custom domain, e.g. `https://harvey.example.com`. Defaults to the Railway domain. |

## Opening the dashboard

Harvey still listens on 127.0.0.1 only and still requires its token. The
deploy log prints the unlock link with the public domain already in it:

    Harvey Dashboard running. Open this link to unlock it:
        https://<domain>/#token=...

Anyone with that link can use the dashboard, so treat it like a password. To
replace the token, delete `data/dashboard-token` on the volume and redeploy.

## Moving an existing install here

Stop Harvey on the old machine first, so nothing is sent twice and the
database is complete. Then, from inside the old Harvey folder:

    COPYFILE_DISABLE=1 tar czf - .env harvey.yaml harvey.local.yaml data prompts skills \
      | curl -sS -X POST --data-binary @- \
          -H "Authorization: Bearer <dashboard token>" \
          -H "Content-Type: application/gzip" \
          https://<domain>/_import

Leave out any file that does not exist. The container restarts on the
imported data. To replace an existing setup add `-H "X-Overwrite: yes"`.

## Upgrading Harvey's code

The `railway` branch is `main` plus the `railway/` folder, `railway.json` and
nothing else, so it never conflicts with Harvey's own files. Railway deploys
every push to `railway`.

1. Bring new code into `main` (your own commits, or a merge from upstream
   `ethanplusai/harvey`), resolve any conflicts there and run the tests.
2. Merge `main` into `railway` and push. Railway builds and deploys it.
3. Watch the deploy log for `package:` and `commit:` and for the dashboard
   link. If the build fails, compare `railway/Dockerfile` with the root
   `Dockerfile`, which upstream updates when system packages change.

What protects the data during an upgrade:

- Config, `.env`, prompts, skills and `data/` live on the volume and are never
  overwritten by a deploy; only the package code is replaced.
- Whenever the deployed commit changes, the database is copied to
  `data/backups/` before the new code starts. The newest three are kept and
  the log names each one.
- To go back: roll the deployment back in Railway, set `RESTORE_BACKUP` to the
  backup's file name from the log, redeploy, then remove the variable.

Upstream renamed the package from `harvey` to `sellbird`. The scripts here
detect which one is present and handle both sets of file names; Sellbird
itself renames the old config and database on first start.

## Off-site backup

`railway/backup.py` pushes the database, config, prompts and skills to the
private repository above. `.env` is never included, only the names of its
variables. Each snapshot replaces the last one. The repository's own README
explains how to restore. The log line to look for is
`[railway] backup: snapshot pushed to ...`.

## Limits

- The Claude token from `setup-token` can only run model requests, so the
  dashboard's Claude quota gauge may stay empty and the daily-percent limit
  may not apply. Harvey keeps working either way.
- Mailboxes that sign in through a desktop keychain or a browser (Gmail or
  Microsoft OAuth, LinkedIn) do not carry over; Instantly and SMTP do.

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

## Limits

- The Claude token from `setup-token` can only run model requests, so the
  dashboard's Claude quota gauge may stay empty and the daily-percent limit
  may not apply. Harvey keeps working either way.
- Mailboxes that sign in through a desktop keychain or a browser (Gmail or
  Microsoft OAuth, LinkedIn) do not carry over; Instantly and SMTP do.

# `deploy/`

Everything needed to run this app on a public host, and nothing that runs by
itself. **[`RUNBOOK.md`](RUNBOOK.md) is the sequence** — read that to deploy.
This file is the map.

| File | What it is |
|---|---|
| [`RUNBOOK.md`](RUNBOOK.md) | The commands, in order: key pair → security group → launch → Elastic IP → tunnel → verify. Includes teardown and troubleshooting. |
| [`cloud-init.yaml`](cloud-init.yaml) | EC2 user-data. Installs Docker and `cloudflared`, clones the repo, generates the database password, starts the stack, seeds it. |
| [`docker-compose.prod.yml`](docker-compose.prod.yml) | Compose **override** for the deployment. Used with the root file, never instead of it. |
| [`cloudflared.yml`](cloudflared.yml) | Tunnel ingress. Copied to `/etc/cloudflared/config.yml` on the instance. |

## The three decisions in here

**An override, not a fork.** The deployment runs
`docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml`.
Everything the override does not mention — the healthcheck, the volume, the
build context, `depends_on: service_healthy` — is byte-for-byte what runs on a
laptop. A second standalone compose file would have been easier to read and
would have drifted within a week.

**No inbound application port.** The app binds `127.0.0.1:8000` and Postgres
binds nothing at all; `cloudflared` makes an outbound connection to
Cloudflare's edge, so requests arrive down a connection the instance opened.
The security group therefore allows SSH and nothing else. The thing to
remember is that this is *load-bearing*: opening 8000 in the security group
does not add a convenience, it removes the only reason the database is not on
the public internet.

**No API key on the box.** `ANTHROPIC_API_KEY` is set to the empty string and
the fallback is disabled. Without a key, the LLM fallback returns `None` and
search degrades to the rule parser, which alone answers every example in the
brief. On an endpoint with no authentication, a key would be an open and
billable relay for whoever found the URL first.

## What has and has not been run

**Two real boots have been run, and both found defects.** That is the most
useful sentence here, so it goes first. Four defects, all now fixed:

1. **`set -euo pipefail` aborted everything** (boot 1). `runcmd` entries run
   under `/bin/sh` — dash on Ubuntu — and dash's `set` has no `pipefail`. It
   does not ignore the unknown option, it exits; and because cloud-init
   concatenates every entry into one script, the first boot installed
   *nothing*. The blocks use `set -eu` now.
2. **The seed block's `exit 0` skipped the `usermod` beneath it** (boot 2).
   Same concatenation: `exit 0` ends the file, not the block. `ubuntu` never
   joined the `docker` group, so every `docker compose` command over SSH would
   have failed with permission denied — on a box that otherwise looked
   deployed. The `usermod` now runs right after Docker installs, and the seed
   ends with a flag and a `break` instead of an exit.
3. **Nothing handed the root-owned clone to `ubuntu`** (boot 2). `runcmd` runs
   as root, so `.env` stayed mode 600 and unreadable — `docker compose` fails
   on it — and git refused the repository as "dubious ownership", which breaks
   the update procedure's `git pull`. The tree is `chown`ed to `ubuntu` now.
4. **The compose file told you to seed without `--yes`**, which the guard makes
   exit 2. A reviewer following it would hit a refusal they were not warned
   about.

Defects 1–3 pass `sh -n`, which is exactly why an earlier version of this
section was wrong: `sh -n` parses, it does not run. The blocks are now checked
by executing them under `dash` with the mutating commands stubbed, which is a
test that can fail. Defect 3 in particular was invisible until a real container
ran and a real `ubuntu` user tried to read a real file.

Still unexecuted: nothing in the boot path — the image build, the migrations,
the seed, and the app serving all ran on the instance. What remains untested is
the tunnel, which needs a Cloudflare account.

### What the second boot actually produced

```
cloud-init status: done
Seeded on attempt 2.
{"status":"ok"}                       # GET /health
app Up 35 seconds
db  Up 38 seconds (healthy)
```

The seed's first attempt failed with `relation "stage_transitions" does not
exist` — migrations had not finished — and the retry loop absorbed it. That is
the loop doing exactly what it exists for.

All eight of the brief's example searches were run against the deployed app and
behave as the README documents, including `sharam` → 1 result and the
explanatory 422 for a query the parser cannot read.

## Teardown

See [RUNBOOK.md](RUNBOOK.md#teardown). Short version: terminate the instance,
release the Elastic IP, delete the security group and key pair — and delete
the tunnel and its DNS record on Cloudflare if you want the hostname gone too.
Terminating the instance destroys the database with it, which is fine because
the data is sample data.

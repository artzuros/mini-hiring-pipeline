# Runbook — deploying to AWS EC2 behind a Cloudflare Tunnel

The shape, in one sentence: **one small EC2 instance** runs the same compose
file that runs locally, Postgres included, and is reached through a
**Cloudflare Tunnel**, so the instance has **no inbound application port open
at all**.

Why this shape and not a PaaS: it is the one the plan already described, it
keeps Postgres and the app in one place (no RDS to pay for or configure), and
the tunnel is what makes "no open ports" true rather than aspirational. The
cost is that you own the operating system, which for a demo means patching it
or deleting it.

Everything below is written to be run in order. Nothing here has been
executed — see the honesty note at the end.

---

## 0. Prerequisites

```bash
brew install awscli           # AWS CLI v2
aws configure                 # paste an access key for a user who can make EC2 resources
```

Region: **`ap-south-1`** (Mumbai) is assumed throughout, since the machine is
on IST — lower latency from here than `us-east-1`, and the same price. Set it
in `aws configure` or pass `--region` everywhere.

You also need a domain on Cloudflare. The tunnel can only route a hostname
whose DNS Cloudflare manages.

---

## 1. A key pair

```bash
aws ec2 create-key-pair \
  --key-name mini-hiring \
  --query 'KeyMaterial' --output text > ~/.ssh/mini-hiring.pem
chmod 400 ~/.ssh/mini-hiring.pem
```

## 2. A security group that opens nothing but SSH

```bash
MY_IP="$(curl -s https://checkip.amazonaws.com)"
SG="$(aws ec2 create-security-group \
  --group-name mini-hiring \
  --description "SSH from one address; no application port" \
  --query GroupId --output text)"

aws ec2 authorize-security-group-ingress \
  --group-id "$SG" --protocol tcp --port 22 --cidr "${MY_IP}/32"
```

**There is no rule for port 8000, and that is the point.** The tunnel is an
outbound connection; nothing needs to reach the app from outside. If you find
yourself adding an inbound rule for 8000, something has gone wrong with the
tunnel — fix that instead.

## 3. Launch the instance

`t3.small` (2 GB) is the recommendation. Building the image *and* running
Postgres in 1 GB is the most likely reason a first attempt fails; if you must
use `t3.micro`, add a swap file before running compose (see Troubleshooting).

```bash
INSTANCE="$(aws ec2 run-instances \
  --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --instance-type t3.small \
  --key-name mini-hiring \
  --security-group-ids "$SG" \
  --user-data file://deploy/cloud-init.yaml \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=mini-hiring}]' \
  --query 'Instances[0].InstanceId' --output text)"

aws ec2 wait instance-running --instance-ids "$INSTANCE"
```

## 4. An Elastic IP

Without one, a stop/start changes the public address and the SSH command in
your notes stops working.

```bash
ALLOC="$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)"
aws ec2 associate-address --instance-id "$INSTANCE" --allocation-id "$ALLOC"

aws ec2 describe-instances --instance-ids "$INSTANCE" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
```

## 5. Watch the first boot

`cloud-init` installs Docker, installs `cloudflared`, clones the repository,
generates the database password, starts the stack, and seeds it. It takes
several minutes, most of it building the image on the instance.

```bash
ssh -i ~/.ssh/mini-hiring.pem ubuntu@<public-ip> \
  'cloud-init status --wait; tail -40 /var/log/cloud-init-output.log'
```

Then confirm the app is actually serving, from the instance:

```bash
ssh -i ~/.ssh/mini-hiring.pem ubuntu@<public-ip> \
  'curl -fsS http://127.0.0.1:8000/health'
# {"status":"ok"}
```

A `503` here means the app is up but Postgres is not — check
`docker compose logs db`.

## 6. The tunnel

Run these **on the instance** (they need the browser login, which prints a URL
you open on your own machine).

```bash
ssh -i ~/.ssh/mini-hiring.pem ubuntu@<public-ip>

cloudflared tunnel login                       # open the printed URL, pick the domain
cloudflared tunnel create mini-hiring          # prints the tunnel UUID
cloudflared tunnel route dns mini-hiring hiring.example.com

UUID="$(ls ~/.cloudflared/*.json | head -1 | xargs basename | sed 's/\.json//')"
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/"$UUID".json /etc/cloudflared/

cd /opt/mini-hiring
sed "s/__TUNNEL_ID__/$UUID/; s/__HOSTNAME__/hiring.example.com/" \
  deploy/cloudflared.yml > /tmp/config.yml
cloudflared tunnel --config /tmp/config.yml ingress validate
sudo mv /tmp/config.yml /etc/cloudflared/config.yml

sudo cloudflared service install               # reads /etc/cloudflared/config.yml
```

Finally, the one thing the security group cannot do for you: **the tunnel
hostname must not also be a public A record.** Cloudflare creates a CNAME to
the tunnel; if an old A record for the same name exists, requests bypass the
tunnel and hit whatever it points at. Delete it.

## 7. Verify

From your own machine:

```bash
curl -fsS https://hiring.example.com/health          # {"status":"ok"}

# The brief's example searches, which should answer as the README documents.
curl -fsS 'https://hiring.example.com/search?q=Find+Priya+Sharma'
curl -fsS 'https://hiring.example.com/search?q=stuck+in+Screening+for+more+than+a+week'
curl -fsS 'https://hiring.example.com/search?q=asdkfjasldkfj'   # the explanatory 422
```

And the two properties that matter most:

```bash
# No API key anywhere on the box. (Checking for the variable being *absent*
# would be the wrong test: the prod file sets it, deliberately, to the empty
# string, so that adding a key later is a visible edit rather than an
# accident. The environment *file* is the thing that must not carry one.)
ssh -i ~/.ssh/mini-hiring.pem ubuntu@<public-ip> \
  'grep -c ANTHROPIC /opt/mini-hiring/.env'          # 0

# Nothing but SSH is reachable from outside.
aws ec2 describe-security-groups --group-ids "$SG" \
  --query 'SecurityGroups[0].IpPermissions[].FromPort'
# [22]
```

---

## Updating the deployment

```bash
ssh -i ~/.ssh/mini-hiring.pem ubuntu@<public-ip>
cd /opt/mini-hiring
git pull
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

Migrations run on container start (`scripts/entrypoint.sh`), so a schema
change needs no separate step. The seed is **not** re-run — it truncates.

## Teardown

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE"
aws ec2 release-address --allocation-id "$ALLOC"
aws ec2 delete-security-group --group-id "$SG"
aws ec2 delete-key-pair --key-name mini-hiring

# On Cloudflare, if you want the DNS record gone too:
cloudflared tunnel delete mini-hiring
```

Terminating the instance destroys the EBS volume and with it the database.
There is no backup, deliberately: the data is sample data.

---

## Troubleshooting

**`docker compose` rejects `!override` / `!reset`.** The Compose plugin is
older than 2.24 — Ubuntu's repository ships one that is. `cloud-init`
installs from Docker's repository for this reason; if you installed Docker by
hand from `apt`, replace it:

```bash
sudo apt-get remove -y docker-compose-v2
# then re-run step 1 of the cloud-init's first block
```

**The build is killed on a 1 GB instance.** Add swap, then rebuild:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**`/health` returns 503.** Postgres is not reachable.
`docker compose logs db` will say why; the usual cause is the first boot
still initialising the cluster.

**Requests reach the origin but return 502.** `cloudflared` is running but
nothing is listening on `127.0.0.1:8000`. Confirm on the instance with
`curl http://127.0.0.1:8000/health`, then check the container is up
(`docker compose ps`).

**You forgot `-f deploy/docker-compose.prod.yml`.** The stack still starts,
which is what makes this worth naming: the base file publishes 8000 on every
interface and reads `POSTGRES_PASSWORD` from nothing, so the app would come up
on a guessable password with the port open to the security group. The symptom
is not an error — it is `docker compose ps` showing the app, and
`aws ec2 describe-security-groups` becoming the only thing standing between
you and an exposed database. Tear down with `down -v` and re-run with both
`-f` flags.

---

## What this deployment is not

- **No authentication.** Anyone with the URL can add candidates, move them,
  reject them, and write notes. That is a deliberate choice for a demo, taken
  with the exposure understood — see the README's Deployment section. It is
  not a choice to carry into anything real.
- **No TLS termination to manage, and no certificate** — Cloudflare provides
  both, which is part of why the tunnel is worth it.
- **No backups, no monitoring, no log aggregation.** `/health` answers a
  probe; nothing is watching it.
- **The base image has known CVEs.** A scan of `python:3.12-slim` reports a
  small number of high-severity findings in OS packages. Rebuilding the image
  picks up Debian's fixes; there is no automated rebuild here.

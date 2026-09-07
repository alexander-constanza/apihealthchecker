# Running the checker inside a tailnet

## What this changes

Before: the checker is on the public internet, so it can only check things
that are also on the public internet. Every private endpoint has to be exposed
to be monitored, which means the act of monitoring it is what makes it
reachable.

After: the checker is a node on a private network with the things it checks.
The API's public ingress rule is deleted, the checks keep passing, and a new
check reports whether the traffic between the two is taking a direct path or
being relayed through a Tailscale DERP server on the way.

Three moving parts:

| Node | Tag | Role |
| --- | --- | --- |
| The laptop | none (user device) | Administration. |
| The EC2 instance | `tag:infra` | Runs the API (gunicorn, tcp/5000) and Postgres 16 (tcp/5432), both in Docker. Also the subnet router. |
| The Fly machine | `tag:monitor` | Runs this service. Checks the other two. |

Nothing here needs a paid plan. The free Personal tier covers six users,
unlimited devices, 50 tagged resources, the policy file, subnet routers and
exit nodes.

## The deployment this was written against

Confirmed from the instance itself on 2026-09-07, not assumed. Every command
below carries these values, so there is nothing to substitute.

| Thing | Value |
| --- | --- |
| Tailnet | `tail422656.ts.net` |
| Instance | `i-0f88fbc1a72c890c7`, Ubuntu 22.04.5 LTS, x86_64, `eu-central-1b` |
| Addresses | public `18.193.101.255`, private `172.31.41.235` |
| VPC | `vpc-0917f7fd736b26485`, CIDR `172.31.0.0/16` |
| Security group | `sg-0554419a1c11d5bd9` |
| Services | `tcp/5000` api-debugging-toolkit via gunicorn, `tcp/5432` postgres:16-alpine, both in Docker Compose |
| SSH | `ubuntu@18.193.101.255`, needed once and then replaced by `ssh ec2-api` |

`net.ipv4.ip_forward` is already `1` on this instance, because Docker sets it.
Step 3 still writes it into `sysctl.d`, so the subnet router survives a reboot
that happens before Docker starts.

## 1. Create the tailnet and join the laptop

```bash
brew install --cask tailscale
tailscale up
tailscale status
```

`tailscale status` listing one device is a working tailnet. The `100.x.y.z`
address it shows is the laptop's stable address on it: unlike its LAN address,
that one does not change when the laptop moves between networks, which is most
of the point.

## 2. Apply the policy file

Admin console, Access Controls, replace the contents with
[`docs/tailnet-policy.hujson`](tailnet-policy.hujson), Save.

Do this before adding any other node. The default policy allows every device to
reach every other device on every port, and a tailnet built under it grows
habits that the real policy then breaks. Applying the restrictive policy first
means every step after this one is proving the policy is right.

The console validates the file on save and refuses one that would lock you out.

## 3. Put the EC2 instance on the tailnet

Getting a shell: port 22 is closed to the internet on this instance, so EC2
Instance Connect cannot work (it connects over SSH from AWS's own address
range). Use **SSM Session Manager**: EC2 console, the instance, Connect, SSM
Session Manager, Connect. It reaches the box over the SSM agent's outbound
channel and needs no inbound port at all. That is also the out-of-band way back
in if anything here goes wrong, and it does not depend on the tailnet.

```bash
# Ubuntu 22.04.5 LTS, x86_64.
curl -fsSL https://tailscale.com/install.sh | sh

# Forwarding is what makes a subnet router a router rather than a host.
# Without it the routes are advertised, approved, installed on every client,
# and silently dropped at this box. ALREADY 1 on this instance (Docker sets
# it), but set it in sysctl.d so it survives a reboot without Docker.
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
echo 'net.ipv6.conf.all.forwarding = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf

sudo tailscale up \
  --advertise-tags=tag:infra \
  --advertise-routes=172.31.0.0/16 \
  --ssh
```

`--advertise-tags=tag:infra` is what makes the policy file apply to this node.
Without it the instance is owned by whoever ran the command and the grants
naming `tag:infra` match nothing.

The route needs no manual approval: `autoApprovers` in the policy file already
authorises `tag:infra` to advertise exactly this CIDR. Confirm from the laptop:

```bash
tailscale status                 # the instance should appear, tagged
tailscale ping ec2-api           # by MagicDNS name
curl http://ec2-api:5000/health  # over the tailnet, no public address involved
```

The subnet router earns its place on the next line, not the previous one. The
EC2 box runs a Tailscale client, so reaching *it* needs no route. The route is
for everything in the VPC that will never run a client: an RDS instance, an
internal load balancer, a second private instance. Those become reachable from
any node the policy allows, by their private address, with nothing exposed.

## Order of operations, and why it changed

The numbered steps below run 3, 5, 6, 4, 7. Establish the new path, prove it
carries the traffic, and only then remove the old one. Closing port 5000 before
the monitor is on the tailnet would leave a window where the endpoint is
reachable by nothing, which is the difference between a migration and an
outage. For a portfolio box that window is harmless. For a customer it is the
whole conversation.

## 4. Close the public ingress

This is the step the whole exercise is for.

```bash
# What is currently open:
aws ec2 describe-security-groups --group-ids sg-0554419a1c11d5bd9 \
  --query 'SecurityGroups[].IpPermissions'

# Remove public access to the API port:
aws ec2 revoke-security-group-ingress \
  --group-id sg-0554419a1c11d5bd9 --protocol tcp --port 5000 --cidr 0.0.0.0/0

# And to SSH, which Tailscale SSH now replaces:
aws ec2 revoke-security-group-ingress \
  --group-id sg-0554419a1c11d5bd9 --protocol tcp --port 22 --cidr 0.0.0.0/0
```

Leave UDP 41641 alone if it is open, and do not open it if it is not. Tailscale
does not need an inbound allow rule: both sides send outbound packets and the
NAT mappings they create are what the direct path runs over. An inbound rule
makes a direct path more likely, not possible.

Verify from outside the tailnet, which is the only verification that counts:

```bash
curl --max-time 5 http://18.193.101.255:5000/health   # must now time out
tailscale down && curl --max-time 5 http://ec2-api:5000/health  # must fail too
tailscale up  && curl --max-time 5 http://ec2-api:5000/health   # must succeed
```

If the first command still returns 200, something else is publishing the port:
a second security group on the instance, or a load balancer in front of it.

## 5. Put the Fly machine on the tailnet

Create the auth key in the admin console (Settings, Keys, Generate auth key)
with **Reusable**, **Ephemeral**, **Pre-approved** and the tag **tag:monitor**.

Each of those four is load-bearing:

- **Reusable**, because every deploy creates a new machine that needs to join.
- **Ephemeral**, because that machine deregisters itself when it stops. Without
  it the console fills with dead nodes, and each dead node still matches the
  `tag:monitor` grants.
- **Pre-approved**, because otherwise a human has to approve each new machine
  before it can do anything, and a deploy at 2am has no human.
- **tag:monitor**, because that is what the policy grants are written against.

```bash
fly secrets set TAILSCALE_AUTHKEY=tskey-auth-...
fly deploy
fly logs | grep tailscale        # expect: tailscale: joined as apihealthchecker-fly
```

The container starts `tailscaled` in userspace networking mode, which needs
neither `/dev/net/tun` nor `NET_ADMIN` and therefore runs as the image's
non-root user. See the comments in `entrypoint.sh` for why the app's own
sockets do not see the tailnet, and why only monitors whose target is a tailnet
address are routed through the local proxy.

With no `TAILSCALE_AUTHKEY` set, none of this happens and the service behaves
exactly as it did before. The tunnel is opt-in at runtime, not baked into the
image.

## 6. Add the monitors

Two new things to check, and one existing thing to move.

```bash
TOKEN=...   # the API_TOKEN secret

# Is the path to the API direct, or relayed?
curl -sS -X POST https://apihealthchecker.fly.dev/api/monitors \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"tailnet path to ec2-api","target":"ec2-api",
       "type":"tailscale_path","interval_seconds":300}'

# The API itself, over the tailnet rather than the public address it no
# longer has. Nothing in the payload says "tailnet": the target is a
# MagicDNS name, and the service routes it through the tunnel because of
# that. See tailnet_proxy_for in runner.py.
curl -sS -X POST https://apihealthchecker.fly.dev/api/monitors \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"ec2-api health (private)","target":"http://ec2-api:5000/health",
       "type":"http","interval_seconds":60}'
```

`tailscale_path` is operator-only. `sandbox.py` refuses any visitor-created
monitor whose type is not `http`, so a visitor on the public status page cannot
create one.

Locally, the same two checks from the CLI in the other repo:

```bash
infra-health tailscale ec2-api
infra-health http http://ec2-api:5000/health
infra-health config tailnet.yml
```

## 7. Diagnose the path

Run this from the Fly machine, which is the interesting end: it sits behind
Fly's NAT and has no tun device, so it is the side most likely to be relayed.

```bash
fly ssh console
/app/tailscale --socket=/tmp/tailscaled.sock status
/app/tailscale --socket=/tmp/tailscaled.sock ping ec2-api
/app/tailscale --socket=/tmp/tailscaled.sock netcheck
```

Read them in that order:

- **`status`** names the current path. A peer line ending in an `ip:port` is a
  direct connection to that address. One ending in `relay "lhr"` is going
  through the DERP server in that region, adding its round trip to every packet
  in both directions.
- **`ping`** shows the path being negotiated. Tailscale starts a connection
  over DERP because DERP always works, then attempts a direct path in the
  background and switches if it succeeds. Output that reads `via DERP(lhr)` for
  the first two pings and `via 203.0.113.5:41641` for the third is the normal,
  healthy sequence, not a fault. This is why the check pings before it judges:
  classifying a connection on its first packet reports every healthy path as a
  bad one.
- **`netcheck`** explains a path that never upgrades. `UDP: false` means no
  direct path is possible from here at all. `MappingVariesByDestIP: true` means
  a hard NAT on this side, where the external port differs per destination, so
  an address discovered for one peer is useless for another. Neither is
  something the far end can fix.

The check automates exactly this reading: `tailscale_path` pings, classifies
from `tailscale status --json`, and on a relayed verdict runs `netcheck` and
attaches the interpretation to the result. `docs/TAILNET_PATH_REPORT.md` is the
template for writing down what you found.

## Rollback

```bash
fly secrets unset TAILSCALE_AUTHKEY && fly deploy   # monitor leaves the tailnet
sudo tailscale down                                 # on EC2
aws ec2 authorize-security-group-ingress \
  --group-id sg-0554419a1c11d5bd9 --protocol tcp --port 5000 --cidr 0.0.0.0/0
```

The tailnet monitors will report UNKNOWN rather than FAIL once the tunnel is
gone, which is deliberate: "I could not determine this" is a different
statement from "this is broken", and the distinction is carried from the
engine's three-state result all the way to the status page.


---

# What actually happened, 2026-09-07

Recorded from the build itself rather than from the plan.

## Step 2, policy

Saved to `tail422656.ts.net`. Replaced the default single rule (every device,
every device, every port) with three grants, tag ownership, a scoped route
auto-approver and a Tailscale SSH rule at `check`.

## Step 3, the EC2 node

Shell obtained through **SSM Session Manager**, not SSH. Tailscale **1.102.3**
installed from the official script onto Ubuntu 22.04.5 LTS (kernel
6.8.0-1063-aws).

```
sudo tailscale up --advertise-tags=tag:infra --advertise-routes=172.31.0.0/16 \
  --ssh --hostname=ec2-api
```

Result, confirmed in the admin console:

| Check | Result |
| --- | --- |
| Tailscale IPv4 | `100.100.229.4` |
| MagicDNS name | `ec2-api.tail422656.ts.net` |
| ACL tag | `tag:infra` applied |
| Key expiry | **No expiry**, because the node is tagged |
| Subnet route | `172.31.0.0/16` **Approved**, nothing awaiting approval |
| Tailscale SSH | Enabled |
| Exit node | Not allowed, which is correct: none was requested |

The route needed no human approval. `autoApprovers` in the policy file
authorised `tag:infra` to advertise exactly that CIDR, so writing the policy
first turned a manual console step into a property of the configuration. That
is the difference between a tailnet that scales to a hundred rebuilt instances
and one that does not.

## A real finding: UDP GRO forwarding

`tailscale up` warned that UDP GRO forwarding was suboptimally configured on
`ens5`. This is a throughput limit specific to subnet routers on interfaces
that support GRO, and it surfaces to a customer as "the VPN is slow through the
gateway" rather than as an error. Fixed and made persistent:

```bash
NETDEV=$(ip -o route get 8.8.8.8 | cut -f5 -d' ')
sudo ethtool -K $NETDEV rx-udp-gro-forwarding on rx-gro-list off

# ethtool settings do not survive a reboot, so the same change is written as a
# networkd-dispatcher hook that reruns whenever the link becomes routable.
sudo mkdir -p /etc/networkd-dispatcher/routable.d
printf '#!/bin/sh\n\nethtool -K %s rx-udp-gro-forwarding on rx-gro-list off\n' "$NETDEV" \
  | sudo tee /etc/networkd-dispatcher/routable.d/50-tailscale
sudo chmod 755 /etc/networkd-dispatcher/routable.d/50-tailscale
```

IP forwarding was already `1` because Docker sets it. It is still written to
`/etc/sysctl.d/99-tailscale.conf`, because "Docker happens to set it" is not a
configuration, and a reboot where Docker starts late gives a subnet router that
advertises routes and silently drops every packet.

## Connectivity profile of the EC2 side

From the machine detail page, which is the same data `tailscale netcheck`
reports:

| Property | Value | What it means |
| --- | --- | --- |
| UDP | Yes | Direct paths are possible from this side |
| Varies | No | Easy NAT: one external mapping for all destinations |
| UPnP / PCP / NAT-PMP | No | No port mapping, so discovery has to do the work |
| Preferred relay | Frankfurt, 1.62 ms | Nuremberg 4.43 ms second |
| Endpoints | `18.193.101.255:41641` plus three private | Advertised candidates for a direct path |

`UDP: Yes` with `Varies: No` means this end is not the constraint. If the Fly
machine ends up relayed, the cause is on the Fly side, and that is a finding
rather than a guess.

# Claim-Agent remote MCP server on OCI (claude.ai custom connector)

`claim_agent.mcp_server` exposes the web workspace to Claude as MCP tools over streamable HTTP with OAuth. Requests
from Claude take the same path as the web chat: automatic routing, the gated pipeline, the same `runs/` records.

| Piece | Where |
|---|---|
| Tools (`claim_new_conversation`, `claim_attach_text`, `claim_attach_file`, `claim_send`, `claim_status`, `claim_full_report`, `claim_resume`, `claim_stop`, `claim_list_conversations`) | `claim_agent/mcp_service.py`, `claim_agent/mcp_server.py` |
| OAuth (dynamic client registration, PKCE S256, passphrase login page, refresh rotation, tokens stored as SHA-256 digests) | `claim_agent/mcp_server.py` → `.tui/mcp/oauth.json` |
| VM bootstrap (clone, venv, iptables 80/443, Caddy + Let's Encrypt on `<ip-with-dashes>.sslip.io`, systemd unit) | `deploy/oci/cloud-init.yaml` |

## 1. Deploy

1. Push this repository to GitHub. The VM clones `https://github.com/tony816/Claim-Agent.git` at boot and installs `.[mcp]`.
2. Launch an Always Free `VM.Standard.A1.Flex` (2 OCPU / 12 GB, Ubuntu aarch64, 50 GB boot volume) in the public
   subnet with a public IP, your SSH key, and `cloud-init.yaml` as user data.
3. Watch the bootstrap (a few minutes): `ssh ubuntu@<public-ip> sudo tail -f /var/log/claim-agent-bootstrap.log`
   until it prints `== ready: https://<host>/mcp`.

### 1-b. Or add it to a VM that already runs

When a new VM cannot be created (for example `LimitExceeded` on A1 cores), install on an existing VM over SSH. The
script uses the same layout as the cloud-init path, adds one Caddy site block for the VM's sslip.io name (backing up
the Caddyfile and rolling back if validation fails), and leaves other sites, firewall rules and services alone.

```bash
curl -fsSL https://raw.githubusercontent.com/tony816/Claim-Agent/main/deploy/oci/install.sh | sudo sh
```

It stops after printing the connector URL if the secrets are still empty. Fill them (section 2), then run the same
command again: it starts the service. `CLAIM_AGENT_MCP_PORT=...` changes the local port if 8765 is taken.

The script supports Ubuntu (`apt`) and Oracle Linux (`dnf`; the SSH user is `opc`, and Python 3.12/3.11 is installed
because the system `python3` on Oracle Linux 9 is 3.9). On an SELinux-enforcing host where Caddy runs confined as
`httpd_t`, the script prints the `setsebool -P httpd_can_network_connect 1` command instead of changing the policy;
run it yourself if the connector URL answers 502.

## 2. Secrets (only you, over SSH)

```bash
sudo nano /etc/claim-agent/claim-agent.env
```

Set `GEMINI_API_KEY` and `CLAIM_AGENT_MCP_PASSPHRASE` (20+ random characters). The unit refuses to start while either
is missing or the passphrase is shorter than 20 characters.

```bash
sudo systemctl enable --now claim-agent-mcp
```

```bash
curl -s https://<host>/.well-known/oauth-protected-resource/mcp
```

The response's `resource` must be exactly `https://<host>/mcp`.

## 3. Connect Claude

claude.ai → **Customize → Connectors → Add custom connector** → URL `https://<host>/mcp` → **Connect**. The browser
opens the Claim-Agent login page; enter the passphrase. Then ask Claude to use Claim-Agent, e.g. "Claim-Agent로 이
발명의 1항을 작성해줘" with the material attached or pasted.

A pipeline run takes minutes, longer than one tool call may last (240 s on claude.ai), so Claude calls `claim_send` and
then `claim_status` with `wait_seconds=120` until the turn finishes.

## Operations

Update the code:

```bash
cd /opt/claim-agent/app && sudo -u claimagent git pull && sudo -u claimagent ../venv/bin/pip install -e '.[mcp]' && sudo systemctl restart claim-agent-mcp
```

Revoke every Claude session (for example after changing the passphrase):

```bash
sudo systemctl stop claim-agent-mcp && sudo rm /opt/claim-agent/app/.tui/mcp/oauth.json && sudo systemctl start claim-agent-mcp
```

Logs: `journalctl -u claim-agent-mcp -f`; Caddy: `journalctl -u caddy -f`.

## What lives on the VM

Invention material, drawings, claim text and reports sent through Claude are stored under
`/opt/claim-agent/app/.tui/` and `/opt/claim-agent/app/runs/`, like the desktop app stores them locally. Every call to
the pipeline spends the Gemini API key's quota. `claim-agent runs purge --older-than N` removes old runs.

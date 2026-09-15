#!/bin/sh
# Install or update the Claim-Agent remote MCP server on a VM that is already running (e.g. one that serves other sites
# through Caddy). Same layout as cloud-init.yaml: /opt/claim-agent, the claim-agent-mcp systemd unit, secrets in
# /etc/claim-agent/claim-agent.env, and a Caddy site on the sslip.io name of the public IP.
#
#   curl -fsSL https://raw.githubusercontent.com/tony816/Claim-Agent/main/deploy/oci/install.sh | sudo sh
#
# Re-running is safe: it updates the code, keeps the secrets, and adds its Caddy site block only once. Other Caddy
# sites, firewall rules and services are not touched; an invalid Caddy config is rolled back from the backup.
set -eu

REPO=https://github.com/tony816/Claim-Agent.git
BASE=/opt/claim-agent
PORT=${CLAIM_AGENT_MCP_PORT:-8765}
ENV_FILE=/etc/claim-agent/claim-agent.env
UNIT=/etc/systemd/system/claim-agent-mcp.service
CADDYFILE=/etc/caddy/Caddyfile
MARK="# claim-agent-mcp site (managed by deploy/oci/install.sh)"

log() { printf '\n== %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "sudo로 실행하세요: curl -fsSL .../install.sh | sudo sh"

log "packages"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q git curl python3 python3-venv python3-pip
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q git curl python3 python3-pip
  dnf install -y -q python3.12 python3.12-pip 2>/dev/null || dnf install -y -q python3.11 python3.11-pip 2>/dev/null || true
else
  die "apt-get 또는 dnf가 없는 배포판은 지원하지 않습니다."
fi

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY=$(command -v "$candidate"); break
  fi
done
[ -n "$PY" ] || die "Python 3.11 이상이 필요합니다."
log "python: $PY"

if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$PORT" | grep -q LISTEN && ! systemctl is-active -q claim-agent-mcp; then
  die "포트 $PORT를 다른 프로그램이 쓰고 있습니다. CLAIM_AGENT_MCP_PORT=다른포트 로 다시 실행하세요."
fi

log "code and venv in $BASE"
id claimagent >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin claimagent
mkdir -p "$BASE"
if [ -d "$BASE/app/.git" ]; then
  git -c safe.directory="$BASE/app" -C "$BASE/app" pull --ff-only
else
  git clone --depth 1 "$REPO" "$BASE/app"
fi
[ -x "$BASE/venv/bin/python" ] || "$PY" -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q --upgrade pip
"$BASE/venv/bin/pip" install -q -e "$BASE/app[mcp]"
chown -R claimagent:claimagent "$BASE"

log "public URL"
ip=$(curl -fsS --retry 5 https://checkip.amazonaws.com | tr -d '[:space:]')
host="$(printf '%s' "$ip" | tr . -).sslip.io"
mkdir -p /etc/claim-agent
printf 'CLAIM_AGENT_MCP_PUBLIC_URL=https://%s\n' "$host" > /etc/claim-agent/public-url.env
chmod 0644 /etc/claim-agent/public-url.env
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  cat > "$ENV_FILE" <<'EOF'
# Fill both values, then re-run install.sh or: sudo systemctl enable --now claim-agent-mcp
GEMINI_API_KEY=
# Passphrase you type on the connector login page when claude.ai connects (20+ random characters).
CLAIM_AGENT_MCP_PASSPHRASE=
EOF
  umask 022
fi
chmod 0600 "$ENV_FILE"

log "systemd unit"
# Quoted heredoc: `$$` must reach systemd verbatim (it hands a literal `$` to the shell).
cat > "$UNIT" <<'EOF'
[Unit]
Description=Claim-Agent remote MCP server
After=network-online.target
Wants=network-online.target

[Service]
User=claimagent
Group=claimagent
WorkingDirectory=/opt/claim-agent/app
EnvironmentFile=/etc/claim-agent/claim-agent.env
EnvironmentFile=/etc/claim-agent/public-url.env
ExecStartPre=/bin/sh -c 'test -n "$$GEMINI_API_KEY" && test $${#CLAIM_AGENT_MCP_PASSPHRASE} -ge 20'
ExecStart=/opt/claim-agent/venv/bin/python -m claim_agent.mcp_server --project-root /opt/claim-agent/app --host 127.0.0.1 --port __PORT__
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/claim-agent

[Install]
WantedBy=multi-user.target
EOF
sed -i "s/__PORT__/$PORT/" "$UNIT"
systemctl daemon-reload

log "Caddy site https://$host"
block=$(printf '%s\n%s {\n\tencode gzip\n\treverse_proxy 127.0.0.1:%s {\n\t\tflush_interval -1\n\t}\n}\n' "$MARK" "$host" "$PORT")
if ! command -v caddy >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy
  else
    die "Caddy가 없습니다. Caddy를 설치한 뒤 다시 실행하세요."
  fi
fi
if [ ! -f "$CADDYFILE" ]; then
  printf '%s\n' "$block"
  die "$CADDYFILE 이 없습니다(Docker 등 별도 설정). 위 사이트 블록을 사용 중인 Caddy 설정에 직접 추가하세요."
fi
if grep -qF "$MARK" "$CADDYFILE"; then
  echo "site block already present"
else
  backup="$CADDYFILE.bak.$(date +%Y%m%d%H%M%S)"
  cp "$CADDYFILE" "$backup"
  printf '\n%s\n' "$block" >> "$CADDYFILE"
  if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    cp "$backup" "$CADDYFILE"
    die "추가한 사이트 블록으로 Caddy 설정 검증이 실패해 원래 설정으로 되돌렸습니다($backup)."
  fi
  echo "added site block (backup: $backup)"
fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# Oracle Linux enforces SELinux. A Caddy confined as httpd_t may not open connections to 127.0.0.1:$PORT; that is a
# security setting of this VM, so it is reported for the owner to decide rather than changed here.
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = Enforcing ] \
   && ps -eZ 2>/dev/null | grep -w caddy | grep -q httpd_t \
   && [ "$(getsebool httpd_can_network_connect 2>/dev/null | awk '{print $3}')" != on ]; then
  echo "SELinux가 Caddy(httpd_t)의 로컬 포트 연결을 막을 수 있습니다. 연결이 502로 실패하면 다음을 직접 실행하세요:"
  echo "  sudo setsebool -P httpd_can_network_connect 1"
fi

log "service"
key=$(sed -n 's/^GEMINI_API_KEY=//p' "$ENV_FILE")
phrase=$(sed -n 's/^CLAIM_AGENT_MCP_PASSPHRASE=//p' "$ENV_FILE")
if [ -n "$key" ] && [ "${#phrase}" -ge 20 ]; then
  systemctl enable -q claim-agent-mcp
  systemctl restart claim-agent-mcp
  sleep 3
  systemctl is-active -q claim-agent-mcp && echo "claim-agent-mcp is running" || { journalctl -u claim-agent-mcp -n 30 --no-pager; die "서비스가 시작되지 않았습니다."; }
  echo "connector URL for claude.ai: https://$host/mcp"
else
  echo "비밀값이 아직 없습니다. 다음을 실행한 뒤 이 스크립트를 다시 실행하세요:"
  echo "  sudo nano $ENV_FILE    # GEMINI_API_KEY, CLAIM_AGENT_MCP_PASSPHRASE(20자 이상)"
  echo "connector URL (after that): https://$host/mcp"
fi

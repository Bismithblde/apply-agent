#!/usr/bin/env bash
# One-shot, re-runnable setup for apply-agent. Safe to run again at any time; it only fills in what's missing.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
PY=.venv/bin/python

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ask()  { local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
env_set() {
  "$PY" - "$1" "$2" <<'EOF'
import sys, re, pathlib
key, val = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); txt = p.read_text() if p.exists() else ""
line = f"{key}={val}"
txt = re.sub(rf"^{re.escape(key)}=.*$", line, txt, flags=re.M) if re.search(rf"^{re.escape(key)}=", txt, re.M) else txt.rstrip("\n") + ("\n" if txt else "") + line + "\n"
p.write_text(txt); p.chmod(0o600)
EOF
}

say "Python environment"
if [ ! -x "$PY" ]; then
  cat >&2 <<'EOF'
Missing .venv/bin/python. This project setup uses the existing .venv and will not create a new one.
Restore the project's existing Python 3.14 environment at .venv, then rerun ./setup.sh.
EOF
  exit 1
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt
"$PY" -m playwright install chromium >/dev/null
echo "dependencies installed"

say "Secrets (.env)"
touch .env && chmod 600 .env
mkdir -p secrets data/screenshots logs && chmod 700 secrets
[ -n "$(env_get VAULT_KEY)" ] || env_set VAULT_KEY "$("$PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
[ -n "$(env_get NTFY_TOPIC)" ] || env_set NTFY_TOPIC "apply-agent-$("$PY" -c 'import secrets; print(secrets.token_hex(8))')"
[ -n "$(env_get APPLY_EMAIL)" ] || env_set APPLY_EMAIL "$("$PY" -c 'import yaml; print(yaml.safe_load(open("profile.yaml"))["personal"]["email"])')"
[ -n "$(env_get GEMINI_MODEL)" ] || env_set GEMINI_MODEL "gemini-3.1-flash-lite"
[ -n "$(env_get GMAIL_CLIENT_SECRET_FILE)" ] || env_set GMAIL_CLIENT_SECRET_FILE "secrets/gmail_client.json"
[ -n "$(env_get GMAIL_TOKEN_FILE)" ] || env_set GMAIL_TOKEN_FILE "secrets/gmail_token.json"
if [ -z "$(env_get GEMINI_API_KEY)" ]; then
  read -r -s -p "Gemini API key (aistudio.google.com/apikey): " k; echo; [ -n "$k" ] && env_set GEMINI_API_KEY "$k"
fi
if [ -z "$(env_get CAPSOLVER_API_KEY)" ]; then
  read -r -s -p "CapSolver API key (capsolver.com, Enter to skip): " k; echo; [ -n "$k" ] && env_set CAPSOLVER_API_KEY "$k"
fi
echo "ntfy topic generated; subscribe to the NTFY_TOPIC value in .env using the ntfy app"

say "Hidden browser display (Xvfb)"
if command -v xvfb-run >/dev/null; then echo "Xvfb installed"
elif ask "Install Xvfb with sudo dnf (recommended)?"; then sudo dnf install -y xorg-x11-server-Xvfb
else echo "skipped; the browser will run headless"; fi

say "Gmail OAuth (reads verification emails)"
CLIENT="$(env_get GMAIL_CLIENT_SECRET_FILE)"; TOKEN="$(env_get GMAIL_TOKEN_FILE)"
if [ ! -f "$CLIENT" ]; then
  cat <<EOF
Missing $CLIENT. To create it (about 5 minutes):
  1. console.cloud.google.com -> new project -> APIs & Services -> Library -> enable "Gmail API"
  2. OAuth consent screen -> External -> Testing -> add $(env_get APPLY_EMAIL) as a test user
  3. Credentials -> Create credentials -> OAuth client ID -> Desktop app -> Download JSON
  4. Save it as $HERE/$CLIENT
EOF
  read -r -p "Press Enter once the file is saved (or type 'skip'): " a
fi
if [ -f "$CLIENT" ] && [ ! -f "$TOKEN" ]; then
  "$PY" -m agent auth gmail || echo "Gmail auth failed; re-run: $PY -m agent auth gmail"
fi

say "Background service (systemd --user)"
mkdir -p ~/.config/systemd/user
sed "s#/home/ryanchen/Downloads/repos/solana/apply-agent#$HERE#g" deploy/apply-agent.service > ~/.config/systemd/user/apply-agent.service
chmod +x run.sh
systemctl --user daemon-reload
echo "installed ~/.config/systemd/user/apply-agent.service"

say "Fetching listings"
"$PY" -m agent fetch | head -3 || true

say "Health check"
"$PY" -m agent doctor || true

cat <<EOF

Next steps:
  Try one job without submitting:   $PY -m agent apply --limit 1 --dry-run --visible
  Start applying in the background:  systemctl --user enable --now apply-agent
  Watch it:                          journalctl --user -u apply-agent -f
  Morning report:                    $PY -m agent report
EOF
if ask "Start the background service now?"; then
  systemctl --user enable --now apply-agent && echo "running. Stop with: systemctl --user stop apply-agent"
fi

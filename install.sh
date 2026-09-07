#!/usr/bin/env bash
# Cairn first-run setup.
#
# Generates .env with strong random secrets. Safe to re-run: existing values are
# never overwritten, so it will not silently rotate CAIRN_MASTER_KEY and render
# every stored credential undecryptable.

set -euo pipefail

ENV_FILE=".env"
EXAMPLE_FILE=".env.example"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
fail() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 || fail "openssl is required but not installed."
[ -f "$EXAMPLE_FILE" ] || fail "$EXAMPLE_FILE not found. Run this from the repository root."

gen_b64() { openssl rand -base64 32 | tr -d '\n'; }
gen_pw()  { openssl rand -base64 24 | tr -d '\n=+/' | cut -c1-24; }

if [ -f "$ENV_FILE" ]; then
    warn "$ENV_FILE already exists — filling in only the empty values."
else
    bold "Creating $ENV_FILE from $EXAMPLE_FILE"
    cp "$EXAMPLE_FILE" "$ENV_FILE"
fi

set_if_empty() {
    local key="$1" value="$2"
    if grep -qE "^${key}=.+$" "$ENV_FILE"; then
        return 0
    fi
    if grep -qE "^#?\s*${key}=" "$ENV_FILE"; then
        # Portable in-place edit: BSD sed (macOS) requires an argument to -i.
        sed -i.bak -E "s|^#?\s*${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
    echo "  generated ${key}"
}

bold "Generating secrets"
set_if_empty "CAIRN_MASTER_KEY"     "$(gen_b64)"
set_if_empty "POSTGRES_PASSWORD"    "$(gen_pw)"
set_if_empty "MINIO_ROOT_PASSWORD"  "$(gen_pw)"

# Point the app at the generated database password.
PG_USER=$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2-)
PG_PASS=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
PG_DB=$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)
DB_URL="postgresql+asyncpg://${PG_USER:-cairn}:${PG_PASS}@postgres:5432/${PG_DB:-cairn}"
sed -i.bak -E "s|^CAIRN_DATABASE_URL=.*|CAIRN_DATABASE_URL=${DB_URL}|" "$ENV_FILE" \
    && rm -f "${ENV_FILE}.bak"

chmod 600 "$ENV_FILE"

cat <<'EOF'

Setup complete.

  1.  docker compose up -d
  2.  docker compose logs migrate      <- the admin password is printed here, ONCE
  3.  open http://localhost:8080

The generated password is displayed a single time and is not recoverable. You
will be required to change it, and to choose a username, at first login.

EOF

warn "Keep .env out of version control. Losing CAIRN_MASTER_KEY makes every"
warn "stored credential permanently undecryptable."

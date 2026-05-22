#!/bin/bash
# Bootstrap Authentik for the agent stack.
#
# Runs ONCE after `docker compose up -d`. Idempotent — safe to re-run.
# Sequence:
#   1. Wait for Authentik to be healthy
#   2. Create initial admin via the env-var seed (AUTHENTIK_BOOTSTRAP_*)
#   3. Create an OIDC Application + Provider for the agent
#   4. Print the issued client_id / secret to stdout (operator stores them
#      in `.env`'s OIDC_CLIENT_ID / OIDC_CLIENT_SECRET)
#
# Auth: uses Authentik's bootstrap API token (set via
# AUTHENTIK_BOOTSTRAP_TOKEN env var; falls back to the admin pwd).

set -euo pipefail

AUTHENTIK_URL="${AUTHENTIK_URL:-http://localhost:9000}"
ADMIN_USER="${AUTHENTIK_BOOTSTRAP_USER:-akadmin}"
ADMIN_PASS="${AUTHENTIK_BOOTSTRAP_PASSWORD:?set AUTHENTIK_BOOTSTRAP_PASSWORD}"
ADMIN_TOKEN="${AUTHENTIK_BOOTSTRAP_TOKEN:-}"
APP_NAME="${APP_NAME:-workspace-agent}"
APP_SLUG="${APP_SLUG:-workspace-agent}"

echo "Waiting for Authentik at $AUTHENTIK_URL ..."
for i in $(seq 1 60); do
    if curl -fsS "$AUTHENTIK_URL/-/health/live/" >/dev/null 2>&1; then
        echo "  ready"
        break
    fi
    sleep 2
done

# Get a session token if AUTHENTIK_BOOTSTRAP_TOKEN wasn't supplied.
if [ -z "$ADMIN_TOKEN" ]; then
    echo "Logging in as $ADMIN_USER to fetch a session token..."
    ADMIN_TOKEN=$(curl -fsS -X POST "$AUTHENTIK_URL/api/v3/core/users/me/" \
        -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
        -H "Content-Type: application/json" 2>/dev/null | jq -r '.token // empty')
fi

if [ -z "$ADMIN_TOKEN" ]; then
    echo "ERROR: could not obtain Authentik admin token. Open $AUTHENTIK_URL/if/flow/initial-setup/" >&2
    exit 1
fi

# Create OIDC provider
echo "Creating OIDC provider for $APP_NAME..."
PROVIDER_ID=$(curl -fsS -X POST "$AUTHENTIK_URL/api/v3/providers/oauth2/" \
    -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d "{
      \"name\": \"$APP_NAME-oidc\",
      \"authorization_flow\": \"default-provider-authorization-implicit-consent\",
      \"client_type\": \"confidential\",
      \"sub_mode\": \"hashed_user_id\",
      \"redirect_uris\": \"http://localhost:8765/auth/callback\"
    }" | jq -r '.pk')

if [ -z "$PROVIDER_ID" ] || [ "$PROVIDER_ID" = "null" ]; then
    echo "ERROR: provider creation failed" >&2
    exit 1
fi

# Read the auto-generated client_id + secret
PROVIDER=$(curl -fsS "$AUTHENTIK_URL/api/v3/providers/oauth2/$PROVIDER_ID/" \
    -H "Authorization: Bearer $ADMIN_TOKEN")
CLIENT_ID=$(echo "$PROVIDER" | jq -r '.client_id')
CLIENT_SECRET=$(echo "$PROVIDER" | jq -r '.client_secret')

# Bind the provider to a new Application
echo "Creating Application linking to provider $PROVIDER_ID..."
curl -fsS -X POST "$AUTHENTIK_URL/api/v3/core/applications/" \
    -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"$APP_NAME\",\"slug\":\"$APP_SLUG\",\"provider\":$PROVIDER_ID}" \
    >/dev/null

echo
echo "============================================"
echo "Authentik bootstrap complete."
echo "Add to your .env file:"
echo "  OIDC_CLIENT_ID=$CLIENT_ID"
echo "  OIDC_CLIENT_SECRET=$CLIENT_SECRET"
echo "  OIDC_ISSUER=$AUTHENTIK_URL/application/o/$APP_SLUG/"
echo "============================================"

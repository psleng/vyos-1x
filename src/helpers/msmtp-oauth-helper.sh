#!/bin/bash

set -e

PROFILE="$1"

CFG="/run/msmtp/oauth/${PROFILE}.conf"
CACHE="/run/msmtp/oauth/${PROFILE}.token"

source "$CFG"

NOW=$(date +%s)

if [ -f "$CACHE" ]; then
    source "$CACHE"

    if [ "${EXPIRES_AT:-0}" -gt $((NOW+300)) ]; then
        echo "$ACCESS_TOKEN"
        exit 0
    fi
fi

ARGS=(
    -s
    -d "grant_type=refresh_token"
    -d "client_id=$CLIENT_ID"
    -d "refresh_token=$REFRESH_TOKEN"
)

if [ -n "$CLIENT_SECRET" ]; then
    ARGS+=(-d "client_secret=$CLIENT_SECRET")
fi

RESPONSE=$(curl "${ARGS[@]}" "$TOKEN_URL")

ERROR=$(echo "$RESPONSE" | jq -r '.error // empty')

if [ -n "$ERROR" ]; then
    echo "$RESPONSE" >&2
    exit 1
fi

ACCESS_TOKEN=$(echo "$RESPONSE" | jq -r '.access_token')
EXPIRES_IN=$(echo "$RESPONSE" | jq -r '.expires_in')

EXPIRES_AT=$((NOW+EXPIRES_IN))

cat > "$CACHE" <<EOF
ACCESS_TOKEN='$ACCESS_TOKEN'
EXPIRES_AT='$EXPIRES_AT'
EOF

chmod 600 "$CACHE"

echo "$ACCESS_TOKEN"

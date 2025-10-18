#!/usr/bin/env bash
set -euo pipefail

: "${PORT:=8080}"
: "${TELEGRAM_LOCAL:=1}"

exec /usr/local/bin/telegram-bot-api       --http-port "$PORT"       --temp-dir /var/lib/telegram-bot-api       --max-webhook-connections 40

#!/bin/sh
set -eu
mkdir -p /alertmanager/secrets
printf '%s' "${TELEGRAM_BOT_TOKEN:-disabled}" > /alertmanager/secrets/bot_token
chat_id="${TELEGRAM_CHAT_ID:-0}"
case "$chat_id" in ''|*[!0-9-]*) chat_id=0 ;; esac
sed "s/^        chat_id: 1$/        chat_id: ${chat_id}/" /etc/alertmanager/alertmanager.yml > /alertmanager/alertmanager.yml
exec /bin/alertmanager --config.file=/alertmanager/alertmanager.yml --storage.path=/alertmanager

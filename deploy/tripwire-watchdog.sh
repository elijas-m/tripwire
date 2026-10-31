#!/usr/bin/env bash
# External dead-man check (§3.1): runs from a systemd timer, independently of the service.
# Restarts Tripwire when it is inactive or its main heartbeat is older than MAX_AGE seconds,
# then reports directly to Telegram. Never prints secrets.
set -u

DB="${TRIPWIRE_DB:-/var/lib/tripwire/tripwire.db}"
PY="${TRIPWIRE_PYTHON:-/opt/tripwire/.venv/bin/python}"
SECRETS="${TRIPWIRE_SECRETS:-/etc/tripwire/secrets.env}"
MAX_AGE="${TRIPWIRE_MAX_AGE:-900}"
SERVICE="${TRIPWIRE_SERVICE:-tripwire}"

active="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
hb="$("$PY" - "$DB" <<'PY' 2>/dev/null
import sqlite3
import sys

try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
    row = conn.execute(
        "SELECT MAX(ts) FROM heartbeats WHERE component='main'"
    ).fetchone()
    print(int(row[0] or 0))
except Exception:
    print(0)
PY
)"
case "$hb" in
  ''|*[!0-9]*) hb=0 ;;
esac

now="$(date +%s)"
age=$(( now - hb ))
if [ "$hb" -eq 0 ]; then
  age=-1
fi

if [ "$active" = "active" ] && [ "$age" -ge 0 ] && [ "$age" -le "$MAX_AGE" ]; then
  exit 0
fi

logger -t tripwire-watchdog "unhealthy: service=${active:-unknown} heartbeat_age=${age}s; restarting"
systemctl restart "$SERVICE" >/dev/null 2>&1 || true
sleep 10
after="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"

if [ "$age" -lt 0 ]; then
  heartbeat_text="нет данных"
else
  heartbeat_text="${age} c назад"
fi
TEXT="🚨 Tripwire watchdog
Сервис: ${active:-unknown}
Heartbeat: ${heartbeat_text}
Перезапуск выполнен, состояние: ${after:-unknown}
Проверьте: journalctl -u ${SERVICE} -n 100"

if [ -f "$SECRETS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SECRETS"
  set +a
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    args=(
      --silent --show-error --max-time 15
      -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}"
      --data-urlencode "text=${TEXT}"
    )
    if [ -n "${TG_THREAD_DIGEST:-}" ]; then
      args+=(--data-urlencode "message_thread_id=${TG_THREAD_DIGEST}")
    fi
    curl "${args[@]}" >/dev/null 2>&1 \
      || logger -t tripwire-watchdog "telegram notification failed"
  fi
fi

exit 1

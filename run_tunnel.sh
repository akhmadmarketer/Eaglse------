#!/bin/bash
# Публичный HTTPS-туннель к локальному обработчику и автоматическая привязка
# адреса к боту Bitrix24.
#
# Зачем: адрес localhost.run меняется при каждом подключении, и после обрыва
# Bitrix24 продолжает стучаться на мёртвый адрес — бот молча перестаёт отвечать.
# Скрипт поднимает туннель заново и сразу переобновляет адрес у бота.
#
# Запуск:  ./run_tunnel.sh
# Без записи в Bitrix24 (только туннель):  EAGLES_SKIP_UPDATE=1 ./run_tunnel.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
LOG_DIR="$ROOT/logs"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
mkdir -p "$LOG_DIR"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "$(stamp) $*" | tee -a "$TUNNEL_LOG"; }

cleanup() {
    [ -n "${SSH_PID:-}" ] && kill "$SSH_PID" 2>/dev/null
    say "туннель остановлен"
    exit 0
}
trap cleanup INT TERM

while true; do
    SESSION_LOG="$(mktemp)"

    # ngrok и localhost.run не работают через прокси разработчика.
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
        ssh -o StrictHostKeyChecking=accept-new \
            -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=30 \
            -o ServerAliveCountMax=3 \
            -R "80:localhost:$PORT" nokey@localhost.run > "$SESSION_LOG" 2>&1 &
    SSH_PID=$!

    URL=""
    for _ in $(seq 1 30); do
        URL=$(grep -o 'https://[a-z0-9-]*\.lhr\.life' "$SESSION_LOG" | head -1)
        [ -n "$URL" ] && break
        kill -0 "$SSH_PID" 2>/dev/null || break
        sleep 1
    done

    if [ -z "$URL" ]; then
        say "адрес не получен, повтор через 10 секунд:"
        tail -3 "$SESSION_LOG" | tee -a "$TUNNEL_LOG"
        kill "$SSH_PID" 2>/dev/null
        rm -f "$SESSION_LOG"
        sleep 10
        continue
    fi

    say "туннель поднят: $URL"

    if [ "${EAGLES_SKIP_UPDATE:-0}" = "1" ]; then
        say "привязка к боту пропущена (EAGLES_SKIP_UPDATE=1)"
    else
        if python3 "$ROOT/register_bitrix_bot.py" --update "$URL/bitrix/events" >> "$TUNNEL_LOG" 2>&1; then
            say "адрес обработчика обновлён у бота"
        else
            say "не удалось обновить адрес у бота, подробности в $TUNNEL_LOG"
        fi
    fi

    wait "$SSH_PID"
    say "туннель оборван, переподключение через 5 секунд"
    rm -f "$SESSION_LOG"
    sleep 5
done

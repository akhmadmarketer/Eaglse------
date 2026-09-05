#!/bin/bash
# Публичный HTTPS-туннель к локальному обработчику с внешней проверкой и
# автоматической привязкой адреса к боту Bitrix24.
#
# Зачем сторожевая проверка: localhost.run снимает проброс на своей стороне,
# при этом ssh-клиент остаётся жив, а TCP-соединение — в состоянии ESTAB.
# Внутренними средствами такой обрыв не виден: Bitrix24 стучится на мёртвый
# адрес, обработчик молчит, в его журнале пусто. Поэтому адрес проверяется
# снаружи, и при отказе туннель поднимается заново с новым адресом.
#
# Запуск:  ./run_tunnel.sh
# Только туннель, без записи в Bitrix24:  EAGLES_SKIP_UPDATE=1 ./run_tunnel.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
LOG_DIR="$ROOT/logs"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
mkdir -p "$LOG_DIR"

SSH_PID=""

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$TUNNEL_LOG"; }

noproxy() {
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy "$@"
}

cleanup() {
    [ -n "$SSH_PID" ] && kill "$SSH_PID" 2>/dev/null
    say "остановлен по сигналу"
    exit 0
}
trap cleanup INT TERM

if ! curl -s --noproxy '*' -m 5 -o /dev/null "http://127.0.0.1:$PORT/health"; then
    say "обработчик на порту $PORT не отвечает — сначала запустите app.py"
    exit 1
fi

while true; do
    SESSION_LOG="$(mktemp)"

    noproxy ssh -o StrictHostKeyChecking=accept-new \
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
        say "адрес не получен, повтор через 10 секунд. Последние строки:"
        tail -3 "$SESSION_LOG" | tee -a "$TUNNEL_LOG"
        kill "$SSH_PID" 2>/dev/null
        rm -f "$SESSION_LOG"
        sleep 10
        continue
    fi

    say "туннель поднят: $URL"

    if [ "${EAGLES_SKIP_UPDATE:-0}" = "1" ]; then
        say "привязка к боту пропущена (EAGLES_SKIP_UPDATE=1)"
    elif python3 "$ROOT/register_bitrix_bot.py" --update "$URL/bitrix/events" >> "$TUNNEL_LOG" 2>&1; then
        say "адрес обработчика обновлён у бота"
    else
        say "не удалось обновить адрес у бота, подробности в $TUNNEL_LOG"
    fi

    # Сторожевая проверка снаружи: два отказа подряд считаем обрывом.
    FAILURES=0
    while kill -0 "$SSH_PID" 2>/dev/null; do
        sleep "$CHECK_INTERVAL"
        if noproxy curl -s -m 15 -o /dev/null -f "$URL/health"; then
            FAILURES=0
        else
            FAILURES=$((FAILURES + 1))
            say "туннель не отвечает снаружи ($FAILURES из 2)"
            [ "$FAILURES" -ge 2 ] && break
        fi
    done

    say "переподключение"
    kill "$SSH_PID" 2>/dev/null
    wait "$SSH_PID" 2>/dev/null
    rm -f "$SESSION_LOG"
    sleep 3
done

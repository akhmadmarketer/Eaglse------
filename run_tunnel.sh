#!/bin/bash
# Публичный HTTPS-туннель к локальному обработчику с внешней проверкой и
# автоматической привязкой адреса к боту Bitrix24.
#
# Провайдер — pinggy.io по SSH: регистрация и установка не нужны.
# Прежний localhost.run отклоняет и анонимный вход, и ключи
# ("Permission denied (publickey)"), ngrok блокирует IP этой машины
# (ERR_NGROK_9040), serveo.net не отвечает. Проверено 5 сентября 2026 года.
#
# Особенность бесплатного туннеля pinggy: он живёт 60 минут. Скрипт сам
# переподключается заранее и заново привязывает новый адрес к боту.
#
# Прямой доступ к a.pinggy.io:443 из этой сети пропадает: TCP-соединение
# зависает, не доходя до приветствия SSH. Поэтому при неудаче скрипт повторяет
# попытку через socks-прокси, который уже используется для OpenAI. Адрес прокси
# берётся из EAGLES_SOCKS_PROXY, иначе из ALL_PROXY, иначе 127.0.0.1:10808.
# Маршрут на публичный адрес не влияет: туннель отдаёт сам pinggy.
#
# Зачем сторожевая проверка: туннель может перестать работать на стороне
# провайдера, при этом ssh-клиент остаётся жив, а TCP-соединение — в состоянии
# ESTAB. Внутренними средствами такой обрыв не виден: Bitrix24 стучится на
# мёртвый адрес, обработчик молчит, в его журнале пусто. Поэтому адрес
# проверяется снаружи.
#
# Запуск:  ./run_tunnel.sh
# Только туннель, без записи в Bitrix24:  EAGLES_SKIP_UPDATE=1 ./run_tunnel.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
# 55 минут: бесплатный туннель pinggy закрывается через 60.
TUNNEL_MAX_AGE="${TUNNEL_MAX_AGE:-3300}"
LOG_DIR="$ROOT/logs"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
mkdir -p "$LOG_DIR"

SOCKS_PROXY="${EAGLES_SOCKS_PROXY:-}"
if [ -z "$SOCKS_PROXY" ]; then
    for candidate in "${ALL_PROXY:-}" "${all_proxy:-}"; do
        case "$candidate" in
            socks*://*) SOCKS_PROXY="${candidate#*://}"; SOCKS_PROXY="${SOCKS_PROXY%/}"; break ;;
        esac
    done
fi
SOCKS_PROXY="${SOCKS_PROXY:-127.0.0.1:10808}"

SSH_PID=""
# Маршрут, который сработал в прошлый раз: с него начинаем следующую попытку.
PREFERRED_ROUTE="direct"

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

start_tunnel() {
    # $1 — маршрут: direct или socks. Запускает ssh и печатает найденный адрес.
    local route="$1" log="$2"

    if [ "$route" = "socks" ]; then
        if ! command -v nc > /dev/null; then
            say "socks-маршрут недоступен: нет утилиты nc"
            return 1
        fi
        ssh -o ProxyCommand="nc -X 5 -x $SOCKS_PROXY %h %p" \
            -o StrictHostKeyChecking=accept-new \
            -o ServerAliveInterval=30 \
            -o ServerAliveCountMax=3 \
            -p 443 -R "0:localhost:$PORT" a.pinggy.io > "$log" 2>&1 < /dev/null &
    else
        noproxy ssh -o StrictHostKeyChecking=accept-new \
                    -o ServerAliveInterval=30 \
                    -o ServerAliveCountMax=3 \
                    -p 443 -R "0:localhost:$PORT" a.pinggy.io > "$log" 2>&1 < /dev/null &
    fi
    SSH_PID=$!

    local found=""
    for _ in $(seq 1 15); do
        found=$(grep -oE 'https://[a-z0-9.-]+\.(free\.pinggy\.net|pinggy-free\.link)' "$log" | head -1)
        [ -n "$found" ] && break
        kill -0 "$SSH_PID" 2>/dev/null || break
        sleep 1
    done

    if [ -z "$found" ]; then
        kill "$SSH_PID" 2>/dev/null
        SSH_PID=""
        return 1
    fi
    URL="$found"
    return 0
}

while true; do
    SESSION_LOG="$(mktemp)"
    URL=""

    if [ "$PREFERRED_ROUTE" = "socks" ]; then
        ROUTES="socks direct"
    else
        ROUTES="direct socks"
    fi

    for route in $ROUTES; do
        if start_tunnel "$route" "$SESSION_LOG"; then
            PREFERRED_ROUTE="$route"
            [ "$route" = "socks" ] && say "прямой маршрут не сработал, идём через socks $SOCKS_PROXY"
            break
        fi
        say "маршрут $route не дал адреса"
    done

    if [ -z "$URL" ]; then
        say "туннель не поднялся ни напрямую, ни через socks. Последние строки:"
        tail -3 "$SESSION_LOG" | tee -a "$TUNNEL_LOG"
        rm -f "$SESSION_LOG"
        sleep 10
        continue
    fi

    STARTED=$SECONDS
    say "туннель поднят: $URL (маршрут $PREFERRED_ROUTE)"

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
        if [ $((SECONDS - STARTED)) -ge "$TUNNEL_MAX_AGE" ]; then
            say "туннелю почти час — переподключаемся до истечения срока"
            break
        fi
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

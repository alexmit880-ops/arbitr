#!/bin/bash
# Запуск арбитражного бота в paper-режиме на Hive OS (Шаг 1.1).
#
# ЗАЧЕМ ЭТОТ СКРИПТ, А НЕ ПРЯМОЙ ExecStart=/usr/bin/python3 app.py
#
# 1) КОД ВЫХОДА. Это главная причина. В app.py нет ни sys.exit, ни
#    os._exit: бот завершается обычным возвратом из main(), то есть
#    ВСЕГДА с кодом 0 - даже если упал. При "Restart=on-failure"
#    systemd такой процесс считает успешным и НЕ перезапустит.
#    Итог: бот один раз молча умер, риг работает, деньги не считаются,
#    и это видно только через journalctl.
#    Здесь код возвращается наружу и скрипт завершается с ним, а
#    "упал с 0" превращается в честный ненулевой код.
#
# 2) ПРАВА. Каталог проекта принадлежит user, поэтому сервис работает
#    от user, а не от root: иначе бот создаст hunter.db и логи
#    от root, и после смены прав доступ к ним пропадёт.
#
# 3) ЗАЩИТА ОТ LIVE. Проверка TRADING_MODE явная, а не по дефолту:
#    случайно поднять бота в реальной торговле на ферме значит
#    отправить настоящие ордера без присмотра.

set -uo pipefail

APP_DIR="/home/user/arbitr"
LOG_DIR="${APP_DIR}/logs"
PYTHON="${APP_DIR}/venv/bin/python"

cd "$APP_DIR" || { echo "FATAL: нет каталога $APP_DIR"; exit 78; }
mkdir -p "$LOG_DIR"

if [ ! -x "$PYTHON" ]; then
    echo "FATAL: нет интерпретатора $PYTHON"
    echo "Создайте venv: python3 -m venv ${APP_DIR}/venv"
    echo "И поставьте зависимости: ${PYTHON} -m pip install -r requirements.txt"
    exit 78
fi

# ─── Проверка режима торговли ───
TRADING_MODE="$(grep -E '^TRADING_MODE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'[:space:]')"
TRADING_MODE="${TRADING_MODE:-paper}"
if [ "$TRADING_MODE" != "paper" ]; then
    echo "FATAL: TRADING_MODE=$TRADING_MODE, а на ферме разрешён только paper."
    echo "Бот не запущен. Это защита от реальных ордеров без присмотра."
    exit 78
fi

echo "=== Запуск бота $(date '+%F %T') | режим=$TRADING_MODE | pid=$$ ==="

# Защита от двух копий: две копии держат открытые пары в своей памяти и
# при обновлении БД затирают состояние друг друга.
if [ -f "${LOG_DIR}/bot.pid" ]; then
    OLD_PID="$(cat "${LOG_DIR}/bot.pid" 2>/dev/null)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "FATAL: уже работает экземпляр pid=$OLD_PID. Запуск отменён."
        exit 75
    fi
    echo "Найден устаревший pid-файл (процесс мёртв), продолжаю."
fi
echo $$ > "${LOG_DIR}/bot.pid"
trap 'rm -f "${LOG_DIR}/bot.pid"' EXIT

"$PYTHON" -u app.py 2>&1 | tee -a "${LOG_DIR}/service.log"
CODE="${PIPESTATUS[0]}"

DURATION=$(( SECONDS ))
echo "=== Бот завершился кодом $CODE за ${DURATION}с $(date '+%F %T') ==="

if [ "$CODE" -eq 0 ]; then
    # Ровно тот случай, о котором написано в п.1: упал, но сообщил 0.
    if [ "$DURATION" -lt 120 ]; then
        echo "ВНИМАНИЕ: бот завершился с кодом 0 меньше чем через 2 минуты."
        echo "Почти наверняка это падение, а не штатная остановка."
        echo "Передаю systemd код 1, чтобы сервис перезапустился."
        CODE=1
    else
        echo "Длительная работа и штатная остановка (Ctrl+C / SIGTERM) - код 0."
    fi
fi

exit "$CODE"

# Развёртывание бота на Hive OS (Шаг 1.1)

Проверено на коде 2026-09-30, коммит 68eda1e. Все расхождения с типовой
инструкцией помечены и объяснены - они не косметические.

---

## Что нужно поменять относительно типовой инструкции

### 1. `Restart=always`, а НЕ `on-failure`

В `app.py` нет ни `sys.exit`, ни `os._exit` - бот завершается обычным
возвратом из `main()` и **всегда отдаёт код 0, даже если упал**.

При `Restart=on-failure` systemd такой процесс считает успешным и не
перезапустит. Практический итог: бот один раз молча умирает, риг
продолжает работать, 24-часовой прогон не состоялся, и это видно
только если специально открыть `journalctl`.

Проверено: подставной "бот", завершившийся с кодом 0 за 0 секунд,
скриптом запуска превращается в код 1 - и systemd его перезапустит.

### 2. `After=network-online.target`, а НЕ `hive.service`

`hive.service` - сервис майнера (он же инициализирует GPU). Бот GPU не
использует вообще, он сетевой. На части прошивок Hive OS такого юнита
просто нет, и systemd пишет "Unit hive.service not found" при каждой
загрузке - то есть шум именно в момент старта.

### 3. Нужен `WorkingDirectory` и запись только туда

Бот пишет `logs/hunter.log` и `hunter.db` относительно текущего каталога
и сам создаёт `logs/`. Без `WorkingDirectory` он создаст их в корне
файловой системы или в `/`, и при `ProtectSystem=full` - упадёт.

### 4. Ограничение ресурсов обязательно

На ферме с майнером бот не должен вытеснять его по RAM. `MemoryMax=1500M`
и `Nice=10` (при нехватке CPU вытеснится бот, а не горячая точка).

### 5. `KillSignal=SIGTERM` + `TimeoutStopSec=60`

У бота свой обработчик сигналов: он гасит задачи и пишет состояние
портфеля. По умолчанию systemd даёт 90 секунд, потом SIGKILL - и состояние
не сохранится. 60 секунд достаточно, SIGKILL не случится.

### 6. Защита от повторного запуска

Две копии бота держат открытые пары в своей памяти и затирают состояние
друг друга - это ровно те "осиротевшие" пары, которые мы разбирали.
Скрипт запуска проверяет `logs/bot.pid`.

---

## Порядок установки

### Шаг 1. Копирование проекта

С рабочей машины (имя пользователя и IP вашего рига):

```bash
# Сначала убедитесь, что на риге есть python3 и venv
ssh user@IP_РИГА 'python3 --version'
```

Копируем только нужное (без .git, БД и логов - на риге будет своя БД):

```bash
rsync -av --exclude='.git' --exclude='__pycache__' --exclude='*.db' \
      --exclude='*.db-wal' --exclude='*.db-shm' --exclude='logs/' \
      --exclude='.env' ./ user@IP_РИГА:/home/user/arbitr/
```

`.env` исключён намеренно: на риге он должен быть свой.

### Шаг 2. Виртуальное окружение

```bash
ssh user@IP_РИГА
cd /home/user/arbitr
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

### Шаг 3. Конфигурация

```bash
cat > .env <<'EOF'
TRADING_MODE=paper
PAPER_BALANCE=10000
PAPER_EXECUTION_MODEL=spot_futures
# TG_TOKEN=            # раскомментируйте, если нужен Telegram
# TG_CHAT=
EOF
```

Проверьте, что режим именно paper:

```bash
grep TRADING_MODE .env
```

### Шаг 4. Установка сервиса

```bash
mkdir -p ~/scripts
cp deploy/run_bot.sh ~/scripts/
chmod +x ~/scripts/run_bot.sh
sudo cp deploy/arb-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Шаг 5. Первый запуск ВРУЧНУЮ (обязательно до systemd)

```bash
~/scripts/run_bot.sh
```

Смотрите, что пошло подключение бирж и пошли циклы. Только после этого
включайте сервис - иначе вы не отличите «не заработало из-за systemd»
от «не заработало вообще».

Прерывание: `Ctrl+C`. Код выхода должен быть 0 (это штатная остановка
дольше 2 минут).

### Шаг 6. Включение сервиса

```bash
sudo systemctl enable arb-bot.service
sudo systemctl start arb-bot.service
systemctl status arb-bot.service
```

### Шаг 7. Проверка, что перезапуск работает

Это стоит проверить ДО прогона, а не во время:

```bash
sudo kill -9 $(pgrep -f 'python.*app.py')
sleep 20
systemctl status arb-bot.service      # должен снова быть active
```

Если после `kill -9` сервис не поднялся - значит проблема в коде выхода,
и прогон запускать рано.

---

## Ежедневный контроль

```bash
systemctl status arb-bot.service        # жив ли
journalctl -u arb-bot.service -f        # логи вживую
journalctl -u arb-bot.service --since "24 hours ago" | grep -c ERROR
tail -f ~/arbitr/logs/hunter.log        # лог самого бота
```

Ротация логов настроена в коде: 25 МБ, хранится 3 дня. Мониторить диск
на риге всё равно стоит - SD-карты там любят заполняться:

```bash
df -h /home/user
```

---

## Остановка прогона

```bash
sudo systemctl stop arb-bot.service
```

Бот получит SIGTERM, корректно сохранит состояние и выйдет с кодом 0.
После `stop` сервис НЕ поднимется обратно (Restart=always не срабатывает
при штатной остановке) - чтобы продолжить, нужен `start`.

---

## Чего делать НЕ надо

- `Restart=always` + `StartLimitBurst` не убраны "чтобы не рестартился
  лишний раз": это единственная защита от бесконечного цикла
  перезапусков, который истрепает логи и маскирует настоящую аварию.
- Не запускайте сервис от `root`: бот создаст `hunter.db` и логи от
  root, и после смены прав вы потеряете к ним доступ.
- Не ставьте `TRADING_MODE=live` на ферме. Скрипт запуска это
  проверяет и откажется стартовать, но проверять руками всё равно
  не стоит.
- Не копируйте `hunter.db` с рабочей машины: там 2 осиротевшие пары с
  неполными данными, они засорят новый прогон.
---

## Шаг 1.11: сбор данных 24-часового прогона

Прогон запущен (`Active: active (running)`). Через 24 часа нужно
снять результаты. Одна команда собирает всё:

```bash
python3 - <<'EOF'
import sqlite3, time, os
c = sqlite3.connect('/root/arbitr/hunter.db')
c.row_factory = sqlite3.Row
now = int(time.time())
since = now - 24*3600
q = lambda s, *a: c.execute(s, a).fetchall()
print("=== ПРОГОН за 24 часа ===")
r = q("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(AVG(pnl_pct),0) pct,"
      " SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins FROM trades"
      " WHERE status='closed' AND ts>=?", since)[0]
print(f"сделок: {r['n']}  PnL: {r['pnl']:+.2f} USDT  средний: {r['pct']:+.3f}%")
print(f"прибыльных: {r['wins']}  WR: {100*r['wins']/max(r['n'],1):.1f}%")
print()
print("=== по категориям (базис входа) ===")
for row in q("SELECT category, COUNT(*) n, AVG(pnl_pct) a FROM trades"
             " WHERE status='closed' AND ts>=? GROUP BY category"
             " ORDER BY n DESC", since):
    print(f"  {row['category']:<12} {row['n']:>4}  средний {row['a']:+.3f}%")
print()
print("=== открытые пары (hold_hours из Шага 1.0) ===")
for row in q("SELECT symbol, buy_ex, sell_ex, size_usdt, hold_hours,"
             " fut_reserved, entry_basis, ts FROM trades WHERE status='open'"):
    age = (now - row['ts'])/3600
    flag = "  <-- ПЕРЕЖИЛА ТАЙМАУТ" if age > (row['hold_hours'] or 4) else ""
    print(f"  {row['symbol']} {row['buy_ex']}->{row['sell_ex']} "
          f"${row['size_usdt']:.0f} возраст {age:.2f}ч "
          f"таймаут {row['hold_hours']:.1f}ч "
          f"резерв ${row['fut_reserved']:.2f} "
          f"базис {row['entry_basis']:.2f}%{flag}")
print()
print("=== последние 10 закрытых ===")
for row in q("SELECT symbol,buy_ex,sell_ex,pnl,pnl_pct,size_usdt,ts"
             " FROM trades WHERE status='closed' AND ts>=? ORDER BY id DESC"
             " LIMIT 10", since):
    print(f"  {row['symbol']:<12} {row['buy_ex']}->{row['sell_ex']} "
          f"${row['size_usdt']:.0f} PnL {row['pnl']:+.3f} ({row['pnl_pct']:+.2f}%)")
print()
print("=== ОШИБКИ в логе за сутки ===")
c.close()
os.system("grep -c ERROR /root/arbitr/logs/hunter.log || true")
os.system("grep -c 'Бот упал' /root/arbitr/logs/hunter.log || true")
print("=== память сервиса сейчас ===")
os.system("systemctl show arb-bot -p MemoryCurrent --value")
EOF
```

Что смотреть в первую очередь:

1. **Сделок: 0** - значит бот не открывает пары. Тогда прогон бессмыслен,
   надо смотреть почему (см. лог, ищи "blocked" и "max_trades").
2. **hold_hours в открытых парах = 4.0 у всех** - значит новая колонка
   не заполнилась, то есть пары пишутся старым путём.
3. **"ПЕРЕЖИЛА ТАЙМАУТ"** - пары не закрываются вовремя, таймаут
   не работает.
4. **Ошибки > 0** - смотреть `grep ERROR logs/hunter.log`.

## Если память всё же убьёт бота

OOMPolicy=kill + Restart=means бот поднимется сам, но прогон
прервётся. Проверить:

```bash
journalctl -u arb-bot | grep -i "killed\|oom\|memory"
```

Если OOM повторяется - снизить нагрузку в config.py:
MAX_SYMBOLS = 400 (было 800). Меньше символов = меньше памяти.
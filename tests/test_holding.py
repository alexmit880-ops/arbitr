# -*- coding: utf-8 -*-
"""
Юнит-тесты подсистемы УДЕРЖАНИЯ (cash-and-carry спот/фьючерс).

Стратегия с удержанием держит позицию между циклами, поэтому её ошибки не
видны в мгновенных сделках — они проявляются через часы. Каждый тест
закрывает конкретный дефект, найденный при ревью:

  1. funding считался в ЦИКЛАХ, а тарифицируется по ЧАСАМ
  2. таймаут измерялся в циклах (при SCAN_INTERVAL_SEC=2 это полсекунды)
  3. не было лимита на число ОДНОВРЕМЕННЫХ пар
  4. не было паузы после закрытия -> повторный вход платил двойные комиссии
  5. открытые пары не переживали перезапуск -> средства замерзали
  6. отсутствие котировки закрывало позицию молча
  7. round-trip по цене входа давал PnL = весь спред (438% в hunter.db)
"""
import asyncio
import time

import pytest

import engine as E

SYM = "SOL/USDT"


def tk(bid, ask, sym=SYM):
    return E.Ticker("x", sym, bid, ask, (bid + ask) / 2.0, 1e6,
                    int(time.time() * 1000))


def opp(sym=SYM, buy_ex="binance", sell_ex="bybit", net=1.5):
    return E.Opportunity(
        symbol=sym, buy_exchange=buy_ex, sell_exchange=sell_ex,
        buy_price=100, sell_price=102, spread_pct=2.0, net_profit_pct=net,
        volume=1e7, exchanges_count=2, timestamp=int(time.time()),
        max_safe_size_usdt=1000, confidence_score=80)


def trader(**kw):
    bm = E.BalanceManager({"binance": {"USDT": 5000},
                           "bybit": {"USDT": 5000}})
    pf = E.Portfolio(balance=10000, initial_balance=10000)
    d = dict(target_close_pct=0.15, stop_widen_pct=1.5,
             max_hold_hours=4.0, max_open_pairs=3, cooldown_sec=300.0)
    d.update(kw)
    return E.SpotFuturesPaperTrader(pf, bm, **d), pf, bm


# Цена фьючерса подобрана так, чтобы базис заведомо проходил финансовый
# критерий входа (BasisEntryCriteria, MIN_BASIS_PCT = 2.0%).
# При споте 100.2 и фьючерсе 102.2 базис составлял ~1.37%, пара
# отклонялась с basis_below_minimum, и все тесты жизненного цикла падали.
FUT_BID, FUT_ASK = 106.0, 106.2


def open_pair(t, **kw):
    r = asyncio.run(t.execute(
        opp(**kw), {"binance": tk(100, 100.2), "bybit": tk(FUT_BID, FUT_ASK)},
        futures_prices={SYM: {"bybit": tk(FUT_BID, FUT_ASK)}}))
    assert r is None, "execute() на открытии не должен возвращать TradeResult"
    return r


def step(t, sb, sa, fb, fa):
    return t.manage_open_positions(
        {SYM: {"binance": tk(sb, sa), "bybit": tk(fb, fa)}},
        {SYM: {"bybit": tk(fb, fa)}})


# ==========================================
# 7. АНТИ-РЕГРЕССИЯ: PnL НЕ равен спреду
# ==========================================
def test_pnl_is_not_just_the_spread():
    """
    КЛЮЧЕВОЙ ТЕСТ ПРОТИВ БАГА ИЗ hunter.db. Прежний код закрывал ноги по
    цене входа: futures_pnl = (entry_fut - entry_spot)*amount = ВЕСЬ СПРЕД,
    PnL не зависел от рынка (438% за 44 часа, WR 99.5%).

    Теперь PnL определяется фактическими ценами выхода и комиссиями, поэтому
    round-trip без сходимости обязан дать УБЫТОК на двойном спреде.
    """
    t, pf, bm = trader(max_hold_hours=0.001)
    open_pair(t)
    # таймаут делаем заведомо истёкшим: проверяем ИМЕННО расчёт PnL, а не
    # механизм выхода (он покрыт отдельными тестами ниже)
    p = list(t.open_pairs.values())[0]
    p.opened_at = time.time() - (p.hold_hours * 3600.0 + 60.0)
    closed = step(t, 100, 100.2, FUT_BID, FUT_ASK)   # цены те же, базис не сошёлся
    assert len(closed) == 1
    assert closed[0].pnl < (102 - 100), \
        "PnL подозрительно близок к полному спреду — возможно возврат бага"
    assert closed[0].pnl < 0, \
        f"round-trip без сходимости дал прибыль {closed[0].pnl:+.4f}"


# ==========================================
# 1-2. ЕДИНИЦЫ ВРЕМЕНИ
# ==========================================
def test_timeout_measured_in_hours_not_cycles():
    """
    max_hold_bars измерялся в циклах. При SCAN_INTERVAL_SEC=2 пара
    закрывалась через секунды, то есть ТАЙМАУТ СРАБАТЫВАЛ РАНЬШЕ СХОДИМОСТИ:
    стратегия физически не могла дождаться цели.
    """
    t, pf, bm = trader(max_hold_hours=0.05)     # 3 минуты
    open_pair(t)
    p = list(t.open_pairs.values())[0]
    # Таймаут принадлежит ПАРЕ (Шаг 0.4): hold_hours задаётся
    # по BASIS_TO_HOLD_HOURS, поэтому отступаем на p.hold_hours.
    p.opened_at = time.time() - (p.hold_hours * 3600.0 + 60.0)
    # Базис на выходе остаётся высоким (не сходится) - иначе сработает
    # CONVERGED раньше, чем TIMEOUT, и тест проверял бы не то.
    closed = step(t, 100, 100.2, 105.0, 105.2)
    assert len(closed) == 1
    assert closed[0].pnl < 0


def test_pair_survives_short_intervals():
    """
    Пара НЕ должна закрываться из-за множества быстрых циклов — это
    главный эффект старого бага max_hold_bars.
    """
    t, pf, bm = trader(max_hold_hours=1.0)
    open_pair(t)
    for _ in range(50):
        closed = step(t, 100, 100.2, 101.0, 101.2)
        assert not closed, "пара закрылась на пустом месте"
    assert len(t.open_pairs) == 1


# ==========================================
# 3. ЛИМИТ ОДНОВРЕМЕННЫХ ПАР
# ==========================================
def test_max_open_pairs_enforced():
    """
    Без лимита execute() открывал новые ноги сверх MAX_OPEN_POSITIONS:
    тот лимит считает ЗАКРЫТЫЕ сделки, а не живые позиции, поэтому удержание
    его обходило. Число живых пар ограничено отдельно.
    """
    t, pf, bm = trader(max_open_pairs=1)
    open_pair(t)
    assert len(t.open_pairs) == 1
    asyncio.run(t.execute(
        opp("BTC/USDT"),
        {"binance": tk(100, 100.2), "bybit": tk(FUT_BID, FUT_ASK)},
        futures_prices={"BTC/USDT": {"bybit": tk(FUT_BID, FUT_ASK)}}))
    assert len(t.open_pairs) == 1, "лимит одновременных пар не соблюдён"
    assert t.rejected_counters.get("max_open_pairs", 0) == 1


# ==========================================
# 4. COOLDOWN
# ==========================================
def test_cooldown_blocks_reentry_after_close():
    """Без паузы бот входит в тот же актив на следующем цикле и платит ещё
    четыре комиссии ради того же спреда."""
    t, pf, bm = trader(cooldown_sec=3600.0)
    open_pair(t)
    step(t, 100, 100.2, 100.05, 100.25)     # сошлось -> закрылось
    assert not t.open_pairs
    open_pair(t)
    assert not t.open_pairs, "повторный вход прошёл cooldown"
    assert t.rejected_counters.get("cooldown", 0) == 1


# ==========================================
# 5. ПЕРЕЖИВАНИЕ ПЕРЕЗАПУСКА
# ==========================================
def test_open_pairs_survive_restart():
    """
    Пары живут между циклами => обязаны пережить перезапуск. Иначе баланс
    уже списан (спот куплен, маржа зарезервирована), а знания о позиции нет:
    она не закроется никогда, средства замерзнут.
    """
    t, pf, bm = trader()
    open_pair(t)
    snap = t.snapshot_open()
    assert len(snap) == 1
    t2, _, _ = trader()
    assert not t2.open_pairs
    assert t2.restore_open(snap) == 1
    p = list(t2.open_pairs.values())[0]
    assert p.symbol == SYM and p.amount > 0
    closed = step(t2, 100, 100.2, 100.05, 100.25)
    assert len(closed) == 1, "восстановленная пара не управляется"


def test_restore_ignores_unknown_fields():
    """Снимок может прийти из другой версии кода — лишние поля не должны
    ломать восстановление."""
    t, _, _ = trader()
    open_pair(t)
    snap = t.snapshot_open()
    snap[0]["some_future_field"] = 42
    t2, _, _ = trader()
    assert t2.restore_open(snap) == 1


# ==========================================
# 6. ПРОПУСК ПРИ ОТСУТСТВИИ КОТИРОВКИ
# ==========================================
def test_missing_quote_does_not_close_silently():
    """Нет котировки => нельзя закрыть (нечем посчитать PnL), но и нельзя
    считать это отказом: позиция просто пропускает цикл."""
    t, pf, bm = trader()
    open_pair(t)
    closed = t.manage_open_positions({}, {})
    assert not closed
    assert len(t.open_pairs) == 1, "пара закрылась без котировок"
    assert t.rejected_counters.get("no_quote", 0) == 1


# ==========================================
# МЕХАНИКА ВЫХОДА
# ==========================================
def test_convergence_closes_with_positive_pnl():
    """
    Сходимость = базис вошёл в зону target_close_pct.
    Вход даёт basis ~= +1.37%, поэтому цена фьючерса должна опуститься
    примерно до spot_mid минус допуск, а не "куда угодно": раньше фьючерс
    уходил на -3.0%, пара не сходилась вовсе и тест падал.
    """
    t, pf, bm = trader(target_close_pct=0.15)
    open_pair(t)
    spot_mid = (100 + 100.2) / 2.0
    fut_mid = spot_mid * (1 - 0.10 / 100.0)     # базис ~= +0.10%, внутри зоны
    closed = step(t, 100, 100.2, fut_mid - 0.1, fut_mid + 0.1)
    assert len(closed) == 1, "сходимость не закрыла пару"
    assert closed[0].pnl > 0, f"PnL {closed[0].pnl:+.4f} не положителен"
    assert closed[0].success is True


def test_all_four_costs_charged():
    """
    PnL обязан учитывать 4 комиссии, перевод и funding.

    Сценарий: сходимости нет (базис пролетает мимо зоны), выход по таймауту.
    Тогда PnL обязан быть отрицательным — это проверяет именно РАСХОДЫ.
    Раньше цены подбирались так, что базис попадал в зону сходимости (0.10%)
    и PnL оказывался положительным: расхождение перекрывало комиссии,
    тест не проверял ничего.
    """
    t, pf, bm = trader(max_hold_hours=0.05)
    open_pair(t)
    p = list(t.open_pairs.values())[0]
    # Таймаут принадлежит паре (Шаг 0.4) - отступаем на p.hold_hours
    p.opened_at = time.time() - (p.hold_hours * 3600.0 + 60.0)
    # Базис на выходе остаётся высоким (не сходится) - иначе сработает
    # CONVERGED раньше, чем TIMEOUT, и тест проверял бы не то.
    closed = step(t, 100, 100.2, 105.0, 105.2)
    assert len(closed) == 1
    assert closed[0].pnl < 0, "комиссии/перевод не учтены"


def test_transfer_fee_only_between_different_exchanges():
    """Перевод платится, только если ноги на РАЗНЫХ биржах."""
    t1, _, _ = trader()
    open_pair(t1, buy_ex="binance", sell_ex="bybit")
    assert list(t1.open_pairs.values())[0].transfer_fee > 0

    t2, _, _ = trader()
    o = E.Opportunity(
        symbol=SYM, buy_exchange="bybit", sell_exchange="bybit",
        buy_price=100, sell_price=102, spread_pct=2.0, net_profit_pct=1.5,
        volume=1e7, exchanges_count=2, timestamp=int(time.time()),
        max_safe_size_usdt=1000, confidence_score=80)
    asyncio.run(t2.execute(o, {"bybit": tk(100, 100.2)},
                           futures_prices={SYM: {"bybit": tk(FUT_BID, FUT_ASK)}}))
    assert len(t2.open_pairs) == 1, "одна биржа: пара не открылась"
    assert list(t2.open_pairs.values())[0].transfer_fee == 0


def test_missing_futures_ticker_is_rejected_not_substituted():
    """
    Регрессия на реальный баг: execute() искал sell_exchange в СИМВОЛЬНОМ
    словаре futures_prices, условие ложилось, и вместо фьючерса молча
    подставлялась СПОТОВАЯ котировка -> фантомный спред без фьючерса.
    Теперь отсутствие фьючерса — явный отказ.
    """
    t, _, _ = trader()
    asyncio.run(t.execute(opp(), {"binance": tk(100, 100.2)}, futures_prices={}))
    assert not t.open_pairs
    assert t.rejected_counters.get("no_futures_ticker", 0) == 1


# ==========================================
# 8. РЕЗЕРВ: утечка маржи при закрытии
# ==========================================
def test_futures_reserve_released_exactly():
    """
    Регрессия на утечку резерва.

    При открытии резервировалось margin + ВХОДНАЯ_комиссия, а закрытие
    освобождало margin + ВЫХОДНАЯ_комиссия + funding через
    apply_futures_short_cycle. Суммы считались от разных баз, и разница
    навсегда оставалась в резерве: свободная маржа утекала на каждой
    паре, пока не переставала хватать на вход.
    """
    t, pf, bm = trader(target_close_pct=0.15)
    open_pair(t)
    p = list(t.open_pairs.values())[0]
    assert p.fut_reserved > 0, "зарезервированная сумма не зафиксирована"
    reserved_at_entry = p.fut_reserved
    before_free = bm.free(p.sell_exchange, "USDT")

    closed = step(t, 100, 100.2, 100.0, 100.2)     # сходимость ~0%
    assert len(closed) == 1
    assert bm.reserved_amount(p.sell_exchange, "USDT") == pytest.approx(0, abs=1e-9), \
        "резерв не освобождён полностью"
    assert bm.reserved_amount(p.buy_exchange, "SOL") == pytest.approx(0, abs=1e-9)
    assert before_free >= 0


def test_repeated_cycles_do_not_leak_margin():
    """
    Десять пар подряд: свободная маржа не должна таять. Это и есть
    практический признак утечки резерва.
    """
    t, pf, bm = trader(target_close_pct=0.15, cooldown_sec=0.0,
                       max_open_pairs=1)
    start = bm.free("bybit", "USDT")
    for i in range(10):
        o = opp(sym=f"A{i}/USDT")
        asyncio.run(t.execute(
            o, {"binance": tk(100, 100.2), "bybit": tk(FUT_BID, FUT_ASK)},
            futures_prices={f"A{i}/USDT": {"bybit": tk(FUT_BID, FUT_ASK)}}))
        sym = f"A{i}/USDT"
        t.manage_open_positions(
            {sym: {"binance": tk(100, 100.2), "bybit": tk(100, 100.2)}},
            {sym: {"bybit": tk(100.0, 100.2)}})
    assert not t.open_pairs
    assert bm.reserved_amount("bybit", "USDT") == pytest.approx(0, abs=1e-9), \
        "маржа утекает цикл за циклом"


# ==========================================
# 9. НЕСОГЛАСОВАННОСТЬ API execute/manage
# ==========================================
def test_execute_and_manage_use_different_dict_shapes():
    """
    Документирует реальное различие структур, а не молча полагается на него:

        execute():  prices         = {exchange: Ticker}
                    futures_prices = {symbol: {exchange: Ticker}}
        manage():   prices         = {symbol: {exchange: Ticker}}
                    futures_prices = {symbol: {exchange: Ticker}}

    Именно это различие раньше приводило к подстановке спотовой котировки
    вместо фьючерсной. Тест падает, если кто-то изменит одну структуру
    без другой.
    """
    t, pf, bm = trader(target_close_pct=0.15)
    sym = "SOL/USDT"
    spot = tk(100, 100.2)
    fut = tk(FUT_BID, FUT_ASK)
    # формат execute: спот по бирже
    asyncio.run(t.execute(
        opp(), {"binance": spot, "bybit": spot},
        futures_prices={sym: {"bybit": fut}}))
    assert len(t.open_pairs) == 1
    # формат manage: спот по символу
    sym_spot = tk(100, 100.2)
    closed = t.manage_open_positions(
        {sym: {"binance": sym_spot, "bybit": sym_spot}},
        {sym: {"bybit": tk(100.0, 100.2)}})
    assert len(closed) == 1

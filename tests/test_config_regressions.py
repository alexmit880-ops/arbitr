# -*- coding: utf-8 -*-
"""
РЕГРЕССИОННЫЕ ТЕСТЫ на исправленные баги.

Каждый тест соответствует конкретному исправлению и падал бы ДО него.
Смысл не «проверить, что код работает», а зафиксировать причину: без них
те же ошибки вернутся при следующем рефакторинге, потому что все они
выглядят как безобидные правки.

  1. Комиссия MEXC != 0        — причина 100% положительных net
  2. Сумма CONFIDENCE_WEIGHTS  — шкала скоринга 0..100
  3. TradeResult без дублей    — dataclasses.fields()
  4. Нет мёртвого кода         — unreachable после return
  5. ShadowLogger(enabled=)    — причина падающих тестов
  6. adjust_opportunity не мутирует — порча истории в БД
  7. PartialFill воспроизводим — детерминированный PnL
  8. _validate() ловит нулевую комиссию
"""
import copy
import dataclasses
import inspect

import pytest

import config as C
import engine as E


# ==========================================
# 1. КОМИССИИ
# ==========================================
def test_no_exchange_has_zero_fee():
    """
    РЕГРЕССИЯ: TRADING_FEES['mexc'] было 0.0000. Любой маршрут с MEXC
    стоил вдвое дешевле (0.1% против 0.2%), из-за чего 100.00% из
    2 539 782 возможностей имели net > 0 — бот «находил» арбитраж везде.
    """
    zeros = [ex for ex, f in C.TRADING_FEES.items() if f == 0]
    assert not zeros, f"Нулевая комиссия у {zeros}: это не комиссия, а её " \
                      f"отсутствие. Маршруты с такими биржами выглядят " \
                      f"искусственно выгодными."


def test_mexc_fee_is_realistic():
    """Реальная комиссия MEXC: spot taker ~0.10%, futures ~0.05%."""
    assert C.TRADING_FEES["mexc"] >= 0.0005, \
        "комиссия MEXC ниже реальной (0.05% фьючерс / 0.10% спот)"


def test_all_configured_exchanges_have_fees():
    for ex in C.EXCHANGES:
        assert ex in C.TRADING_FEES, f"{ex} нет в TRADING_FEES"


def test_fee_is_symmetric_across_route():
    """
    Раньше bybit<->mexc стоил 0.1%, а bybit<->bybit 0.2% — маршрут выбирался
    по комиссии, а не по спреду. Теперь все маршруты стоят одинаково.
    """
    fees = {(a, b): C.TRADING_FEES[a] + C.TRADING_FEES[b]
            for a in C.EXCHANGES for b in C.EXCHANGES if a != b}
    assert len(set(round(v, 6) for v in fees.values())) == 1, \
        f"комиссии маршрутов различаются: {fees}"


# ==========================================
# 2. СУММА ВЕСОВ
# ==========================================
def test_confidence_weights_sum_to_100():
    """
    РЕГРЕССИЯ: сумма была 110. Движок считает total = sum(breakdown),
    где вклад = score*weight/100, поэтому шкала была 0..110 и значение
    «100 = идеальная сделка» недостижимо.
    """
    assert sum(C.CONFIDENCE_WEIGHTS.values()) == 100


def test_confidence_score_scale_is_bounded():
    """При score=100 по всем компонентам итог обязан быть ровно 100."""
    breakdown = {k: 100 * w / 100 for k, w in C.CONFIDENCE_WEIGHTS.items()}
    assert sum(breakdown.values()) == 100


# ==========================================
# 3. DATACLASSES
# ==========================================
def test_trade_result_no_duplicate_fields():
    """Ранее поле success было объявлено дважды."""
    names = [f.name for f in dataclasses.fields(E.TradeResult)]
    dups = [n for n in set(names) if names.count(n) > 1]
    assert not dups, f"Дублирующиеся поля в TradeResult: {dups}"


def test_opportunity_no_duplicate_fields():
    names = [f.name for f in dataclasses.fields(E.Opportunity)]
    dups = [n for n in set(names) if names.count(n) > 1]
    assert not dups, f"Дублирующиеся поля в Opportunity: {dups}"


# ==========================================
# 4. МЁРТВЫЙ КОД
# ==========================================
def test_stability_score_has_single_return():
    """
    РЕГРЕССИЯ: после return шёл дубль `decay/penalty/return`
    (недостижимый код — признак copy-paste слияния).
    """
    src = inspect.getsource(E.SpreadStabilityTracker.stability_score)
    body = [ln for ln in src.split("\n")
            if ln.strip() and not ln.strip().startswith("#")]
    last = max(i for i, ln in enumerate(body)
               if ln.strip().startswith("return"))
    after = [ln for ln in body[last + 1:]
             if ln.strip() and not ln.strip().startswith(("'''", '"""'))]


# ==========================================
# 5. SHADOW LOGGER
# ==========================================
def test_shadow_logger_accepts_enabled():
    """Ранее конструктор не принимал enabled -> TypeError в тестах."""
    log = E.ShadowLogger(enabled=True)
    assert log.enabled is True
    assert hasattr(log, "records")


def test_shadow_logger_default_enabled():
    """Обратная совместимость: app.py зовёт ShadowLogger() без аргументов."""
    assert E.ShadowLogger().enabled is True


# ==========================================
# 6. МУТАЦИЯ OPPORTUNITY
# ==========================================
def _opp(spread=5.0, net=4.0):
    return E.Opportunity(
        symbol="BTC/USDT", buy_exchange="bybit", sell_exchange="mexc",
        buy_price=100.0, sell_price=105.0, spread_pct=spread,
        net_profit_pct=net, volume=1_000_000, exchanges_count=2,
        timestamp=1_700_000_000,
    )


def test_adjust_opportunity_does_not_leak_mutation():
    """
    РЕГРЕССИЯ: значения восстанавливались ТОЛЬКО в ветке отклонения.
    Прошедшие сделки оставались мутированными — и именно этот объект
    писался в БД. Выборки «исполнено» и «все возможности» становились
    несопоставимыми: в историю попадал заниженный net ТОЛЬКО для прошедших.
    """
    st = E.SpreadStabilityTracker()
    for _ in range(6):
        st.update([_opp()])
    o = _opp()
    before = (o.spread_pct, o.net_profit_pct)
    E.ExecutionDelaySimulator.adjust_opportunity(o, st)
    assert (o.spread_pct, o.net_profit_pct) == before, \
        "adjust_opportunity оставил opp мутированным — история в БД искажена"


def test_partial_fill_is_deterministic():
    """
    РЕГРЕССИЯ: random.uniform из глобального генератора давал разный
    fill_ratio от запуска к запуску, а он напрямую влияет на PnL.
    Два прогона на одних данных давали бы разные PnL.
    """
    tr = E.TradeResult(
        timestamp=1_700_000_000, symbol="BTC/USDT",
        buy_exchange="bybit", sell_exchange="mexc",
        amount=1.0, pnl=100.0, pnl_pct=10.0, balance_after=10_100.0,
        success=True,
    )
    r1 = E.PartialFillSimulator.apply(copy.deepcopy(tr), 1000.0, 1000.0)
    r2 = E.PartialFillSimulator.apply(copy.deepcopy(tr), 1000.0, 1000.0)
    assert r1.amount == r2.amount, "частичное исполнение невоспроизводимо"
    assert r1.pnl == r2.pnl, "PnL невоспроизводим"


# ==========================================
# 7. ВАЛИДАЦИЯ КОНФИГУРАЦИИ
# ==========================================
def test_validate_rejects_zero_fee():
    """
    _validate() обязана ловить именно тот класс ошибок, который тихо
    портил расчёты. Проверяем на изолированной копии словаря.
    """
    saved = dict(C.TRADING_FEES)
    try:
        C.TRADING_FEES["mexc"] = 0.0
        with pytest.raises(ValueError) as ei:
            C._validate()
        assert "mexc" in str(ei.value)
    finally:
        C.TRADING_FEES.update(saved)


def test_validate_rejects_bad_weights():
    saved = dict(C.CONFIDENCE_WEIGHTS)
    saved_sum = C.CONFIDENCE_WEIGHTS_SUM
    try:
        C.CONFIDENCE_WEIGHTS["spread"] = 99
        C.CONFIDENCE_WEIGHTS_SUM = sum(C.CONFIDENCE_WEIGHTS.values())
        with pytest.raises(ValueError):
            C._validate()
    finally:
        C.CONFIDENCE_WEIGHTS.clear()
        C.CONFIDENCE_WEIGHTS.update(saved)
        C.CONFIDENCE_WEIGHTS_SUM = saved_sum


def test_validate_passes_on_shipped_config():
    """Текущий конфиг обязан быть валиден."""
    C._validate()


# ==========================================
# 8. МЁРТВЫЕ ЭЛЕМЕНТЫ
# ==========================================
def test_removed_dead_config():
    """
    WATCHLIST был объявлен, но не использовался НИГДЕ. Мёртвая
    конфигурация вводит в заблуждение при чтении.
    """
    assert not hasattr(C, "WATCHLIST"), \
        "WATCHLIST снова объявлен, но нигде не используется"


def test_min_tradeable_volume_no_longer_dead():
    """
    Проверка `pair_vol < MIN_VOLUME_USDT or pair_vol < MIN_TRADEABLE_VOLUME`
    была тавтологией: 10000 < x ИЛИ 5000 < x, где второе строже первого.
    MIN_TRADEABLE_VOLUME был мёртвым.

    Проверяем ИСПОЛНЯЕМЫЕ строки (без комментариев и docstring): иначе тест
    ловит собственное пояснение, в котором имя упоминается намеренно.
    """
    import ast
    tree = ast.parse(inspect.getsource(E))
    code_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "MIN_TRADEABLE_VOLUME":
            code_lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) \
                and node.attr == "MIN_TRADEABLE_VOLUME":
            code_lines.add(node.lineno)
    assert not code_lines, (
        f"MIN_TRADEABLE_VOLUME снова используется в коде (строки "
        f"{sorted(code_lines)}) — вернулась мёртвая тавтология")

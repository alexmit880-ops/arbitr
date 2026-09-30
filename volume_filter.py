"""
Фильтр по объёму через анализ реальных тикеров.

A1.5 ПРАВКА ПО РЕВЬЮ:
Этот модуль нигде не импортируется и не используется в app.py — а при
попытке подключить его по инлайн-инструкции в конце файла он немедленно
падал с AttributeError: вызывал `pool.get_smart_symbols()` и
`pool.fetch_prices()`, которых у ExchangePool не существует (и, судя по
всему, никогда не существовало в этой версии кода — явный остаток от
более ранней архитектуры).

ВАЖНЕЕ: его функциональность уже полностью реализована в
`Hunter.select()` (engine.py) — тот же критерий
`MIN_VOLUME_USDT <= median_vol <= MAX_VOLUME_USDT`, та же идея с медианой
по биржам, и он реально используется в app.py каждый цикл обновления
символов. Так что это не только несовместимый, но и попросту дублирующий
код.

Ниже — версия, приведённая в соответствие с текущим API (на случай, если
понадобится отдельно от Hunter, например для разового анализа не через
основной цикл сканирования), но явная рекомендация: если новых причин
использовать именно этот класс отдельно от Hunter не найдётся, этот файл
стоит удалить, а не поддерживать two параллельные реализации одной и той
же идеи.
"""
import time
from typing import Dict, List, Set
from loguru import logger

from config import MIN_VOLUME_USDT, MAX_VOLUME_USDT
from engine import PriceCache, Ticker


class VolumeFilter:
    """Динамически определяет хорошие монеты по объёму.

    Принимает уже заполненный PriceCache (тот же, что использует основной
    цикл сканирования) вместо попытки самостоятельно тянуть данные с бирж —
    ExchangePool сам по себе не хранит цены/объёмы, этим занимается
    PriceCache (см. engine.py).
    """

    def __init__(self, cache: PriceCache):
        self.cache = cache
        self.good_symbols: Set[str] = set()
        self.symbol_volumes: Dict[str, float] = {}

    def update(self, sample_size: int = 200) -> Set[str]:
        """
        Обновить список хороших монет на основе объёмов, уже накопленных
        в PriceCache (см. cache.volumes, заполняется PriceCache.update_one
        на каждый цикл обновления тикеров).
        """
        all_syms = list(self.cache.volumes.keys())[:sample_size]
        logger.info(f"📊 Анализ объёмов {len(all_syms)} монет...")

        good = set()
        for sym in all_syms:
            per_exchange_volumes = self.cache.volumes.get(sym, {})
            if len(per_exchange_volumes) < 2:
                continue

            volumes = list(per_exchange_volumes.values())
            avg_vol = sum(volumes) / len(volumes)

            if MIN_VOLUME_USDT <= avg_vol <= MAX_VOLUME_USDT:
                good.add(sym)
                self.symbol_volumes[sym] = avg_vol

        self.good_symbols = good
        logger.info(
            f"✅ Отобрано {len(good)} монет "
            f"(из {len(all_syms)} просканированных)"
        )
        return good


# Использование (при наличии отдельной причины не полагаться на Hunter.select):
# volume_filter = VolumeFilter(cache)  # cache — тот же PriceCache, что в app.py
# volume_filter.update()
# symbols = volume_filter.good_symbols

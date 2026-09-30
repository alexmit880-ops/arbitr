"""🚀 Arbitrage Scanner v10.2 — Auto-Recovery Edition."""
import asyncio
import signal
import sys
import time
import copy            # FIX BUG #3: deepcopy для исполнения на копии Opportunity
from datetime import datetime
from collections import deque
from typing import List, Dict, Deque, Optional, Set

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from loguru import logger

from config import *
from engine import (
    ExchangePool, PriceCache, Hunter, OpportunityFinder, PaperTrader,
    Telegram, Database, PipelineStats,
    Portfolio, Ticker, Opportunity, TradeResult,
    LifetimeTracker, ExchangePairRanker, ConfidenceScorer,
    ReplaySystem, ZombieDetector, TradeVerifier, SystemHealthMonitor,
    ExchangeHealthMonitor, ExchangeStatusMonitor, PartialFillSimulator, ExecutionDelaySimulator,
    BalanceManager, SpotFuturesPaperTrader, RealityCheck, OrderBookExecutionAnalyzer,
    SpreadStabilityTracker, ShadowLogger, OrderValidator, OrderValidationResult,
    FundingRateTracker,
)


class Dashboard:
    def __init__(self):
        self.log_buffer: Deque[str] = deque(maxlen=LOG_PANEL_HEIGHT * 3)
        self.current_opportunities: List[Opportunity] = []
        self.stats = {}
        self.cache_stats = {}
        self.pipeline_stats = PipelineStats()
        self.best_found = None
        self.top_pairs: List[tuple] = []
        self.selected_opp: Optional[Opportunity] = None
        self.health: Dict = {}
        self.trade_verification: Dict = {}
        self.zombie_alerts: List[str] = []
        self.recovered_count = 0  # NEW
        self.cycle = 0
        self.start_time = time.time()
        
        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="metrics", size=9),
            Layout(name="middle", size=14),
            Layout(name="health", size=6),
            Layout(name="scanner", size=6),
            Layout(name="logs", size=LOG_PANEL_HEIGHT + 2),
        )
        self.layout["middle"].split_row(
            Layout(name="opportunities", ratio=2),
            Layout(name="card", ratio=1),
        )
    
    def update(self, cycle, opportunities, stats, cache_stats, pipeline,
               best_found=None, top_pairs=None, health=None,
               trade_verification=None, zombie_alerts=None, recovered_count=0):
        self.cycle = cycle
        self.current_opportunities = opportunities or []
        self.stats = stats
        self.cache_stats = cache_stats
        self.pipeline_stats = pipeline
        if best_found:
            self.best_found = best_found
            self.selected_opp = best_found
        if top_pairs is not None:
            self.top_pairs = top_pairs
        if health is not None:
            self.health = health
        if trade_verification is not None:
            self.trade_verification = trade_verification
        if zombie_alerts is not None:
            self.zombie_alerts = zombie_alerts[-5:]
        self.recovered_count = recovered_count
    
    def add_log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_buffer.append(f"[{ts}] {message}")
    
    def render_header(self):
        uptime = int(time.time() - self.start_time)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        text = Text()
        text.append("🎯 ", style="bold")
        text.append("HUNTER MODE v10.2", style="bold cyan")
        text.append(f"  |  Cycle: ", style="dim")
        text.append(f"#{self.cycle}", style="bold yellow")
        text.append(f"  |  Uptime: ", style="dim")
        text.append(f"{h:02d}:{m:02d}:{s:02d}", style="bold green")
        text.append(f"  |  Mode: ", style="dim")
        mode_style = "bold green" if TRADING_MODE == "paper" else "bold red"
        text.append(TRADING_MODE.upper(), style=mode_style)
        return Panel(text, border_style="cyan")
    
    def render_metrics(self):
        s = self.stats
        cache = self.cache_stats
        pipeline = self.pipeline_stats
        
        col1 = Table(box=None, show_header=False, padding=(0, 1))
        col1.add_column(style="cyan", justify="right")
        col1.add_column(style="white", justify="left")
        col1.add_row("Balance:", f"${s.get('balance', PAPER_BALANCE):.2f}")
        pnl = s.get('pnl', 0)
        col1.add_row("PnL:", Text(f"${pnl:+.2f}", style="green" if pnl >= 0 else "red"))
        col1.add_row("PnL %:", Text(f"{s.get('pnl_pct', 0):+.2f}%", style="green" if pnl >= 0 else "red"))
        col1.add_row("Peak:", f"${s.get('peak_equity', PAPER_BALANCE):.2f}")
        dd = s.get('drawdown_pct', 0)
        col1.add_row("DD:", Text(f"{dd:.1f}%", style="red" if dd > 10 else "yellow" if dd > 5 else "green"))
        
        col2 = Table(box=None, show_header=False, padding=(0, 1))
        col2.add_column(style="cyan", justify="right")
        col2.add_column(style="white", justify="left")
        col2.add_row("Trades:", f"{s.get('total', 0)}")
        wr = s.get('winrate', 0)
        col2.add_row("Win Rate:", Text(f"{wr:.1f}%", style="green" if wr >= 60 else "yellow" if wr >= 50 else "red"))
        col2.add_row("W / L:", f"{s.get('wins', 0)} / {s.get('losses', 0)}")
        col2.add_row("Rolling WR:", f"{s.get('rolling_wr', 0):.1f}%")
        col2.add_row("Max Streak:", f"{s.get('max_consecutive_losses', 0)}")
        
        col3 = Table(box=None, show_header=False, padding=(0, 1))
        col3.add_column(style="cyan", justify="right")
        col3.add_column(style="white", justify="left")
        col3.add_row("Cache Age:", f"{cache.get('age', 0):.1f}s")
        col3.add_row("Cache Upd:", f"{cache.get('duration', 0):.2f}s")
        col3.add_row("Symbols:", f"{cache.get('symbols', 0)}")
        col3.add_row("Anomalies:", f"{pipeline.anomalies_skipped}")
        col3.add_row("Pipeline:", f"{pipeline.spreads_found}→{pipeline.after_fees}→{pipeline.profitable}")
        
        col4 = Table(box=None, show_header=False, padding=(0, 1))
        col4.add_column(style="cyan", justify="right")
        col4.add_column(style="white", justify="left")
        if self.best_found:
            col4.add_row("Best:", f"{self.best_found.symbol}")
            conf = self.best_found.confidence_score
            col4.add_row("  Score:", Text(f"{conf:.0f}/100", style="green" if conf >= 70 else "yellow"))
            col4.add_row("  Net:", f"{self.best_found.net_profit_pct:.2f}%")
            route_str = f"{self.best_found.buy_exchange}→{self.best_found.sell_exchange}"
            if self.best_found.route_type == "route3" and self.best_found.mid_exchange:
                route_str = f"{self.best_found.buy_exchange}→{self.best_found.mid_exchange}→{self.best_found.sell_exchange}"
            col4.add_row("  Path:", route_str)
        else:
            for label in ["Best:", "  Score:", "  Net:", "  Path:"]:
                col4.add_row(label, "-")
        
        grid = Table.grid(expand=True, padding=1)
        grid.add_column(); grid.add_column(); grid.add_column(); grid.add_column()
        grid.add_row(col1, col2, col3, col4)
        return Panel(grid, title="📊 METRICS", border_style="green")
    
    def render_opportunities(self):
        if not self.current_opportunities:
            return Panel(Text("No opportunities...", style="dim italic"),
                        title="🔥 TOP OPPORTUNITIES", border_style="yellow")
        
        table = Table(box=None, show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("#", style="dim", width=3)
        table.add_column("Symbol", style="bold white", width=13)
        table.add_column("Buy", style="green", width=8)
        table.add_column("Sell", style="red", width=8)
        table.add_column("Spread", justify="right", width=8)
        table.add_column("Net", justify="right", width=7)
        table.add_column("Conf", justify="center", width=6)
        table.add_column("MaxSize", justify="right", width=9)
        table.add_column("Life", justify="right", width=6)
        
        for i, opp in enumerate(self.current_opportunities[:DASHBOARD_TOP_N], 1):
            net_style = "green" if opp.net_profit_pct >= 1 else "yellow"
            conf = opp.confidence_score
            conf_style = "bold green" if conf >= 70 else "yellow" if conf >= 50 else "red"
            bar_len = 4
            filled = int(conf / 100 * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            
            if opp.first_seen > 0:
                life = int(time.time() - opp.first_seen)
                life_str = f"{life}s"
                life_style = "green" if life < 30 else "yellow" if life < 60 else "dim"
            else:
                life_str = "-"
                life_style = "dim"
            
            table.add_row(
                str(i), opp.symbol, opp.buy_exchange, opp.sell_exchange,
                f"{opp.spread_pct:.2f}%",
                Text(f"{opp.net_profit_pct:.2f}%", style=net_style),
                Text(f"{bar} {conf:.0f}", style=conf_style),
                f"${opp.max_safe_size_usdt:.0f}" if opp.max_safe_size_usdt > 0 else "-",
                Text(life_str, style=life_style),
            )
        return Panel(table, title=f"🔥 TOP {DASHBOARD_TOP_N} OPPORTUNITIES", border_style="yellow")
    
    def render_opportunity_card(self):
        if not self.selected_opp:
            return Panel(Text("No opportunities yet...", style="dim italic"),
                        title="📋 OPPORTUNITY CARD", border_style="cyan")
        
        opp = self.selected_opp
        
        price_table = Table(box=None, show_header=False, padding=(0, 1))
        price_table.add_column(style="cyan", justify="right")
        price_table.add_column(style="white", justify="left")
        price_table.add_row("Symbol:", f"{opp.symbol}")
        price_table.add_row("Buy:", f"{opp.buy_exchange.upper()} @ {opp.buy_price:.8g}")
        price_table.add_row("Sell:", f"{opp.sell_exchange.upper()} @ {opp.sell_price:.8g}")
        price_table.add_row("Spread:", f"{opp.spread_pct:.2f}%")
        price_table.add_row("Volume:", f"${opp.volume:,.0f}")
        
        fees_table = Table(box=None, show_header=False, padding=(0, 1))
        fees_table.add_column(style="cyan", justify="right")
        fees_table.add_column(style="white", justify="left")
        fees_table.add_row("Fees:", f"{opp.fee_pct:.2f}%")
        fees_table.add_row("Slippage:", f"{opp.slippage_pct:.2f}%")
        fees_table.add_row("Withdrawal:", f"{opp.withdrawal_pct:.2f}%")
        fees_table.add_row("Drift:", f"{opp.drift_pct:.2f}%")
        fees_table.add_row("NET:", Text(f"{opp.net_profit_pct:.2f}%", style="bold green"))
        
        timing_table = Table(box=None, show_header=False, padding=(0, 1))
        timing_table.add_column(style="cyan", justify="right")
        timing_table.add_column(style="white", justify="left")
        timing_table.add_row(f"Buy age:", f"{opp.buy_ticker_age:.1f}s")
        timing_table.add_row(f"Sell age:", f"{opp.sell_ticker_age:.1f}s")
        if opp.first_seen > 0:
            life = int(time.time() - opp.first_seen)
            timing_table.add_row("Life:", f"{life}s")
        timing_table.add_row("Conf:", f"{opp.confidence_score:.0f}/100")
        timing_table.add_row("Max Size:", Text(f"${opp.max_safe_size_usdt:.0f}", style="bold yellow"))
        
        conf_text = Text()
        conf_text.append("CONFIDENCE:\n", style="bold cyan")
        for key, value in sorted(opp.confidence_breakdown.items(), key=lambda x: -x[1]):
            if value > 0.1:
                bar_len = 10
                filled = int(value / max(opp.confidence_score, 1) * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                conf_text.append(f"  {key:<12}", style="dim")
                conf_text.append(f"{bar} ", style="white")
                conf_text.append(f"{value:.1f}\n", style="green" if value > 5 else "yellow")
        
        grid = Table.grid(expand=True, padding=1)
        grid.add_column(); grid.add_column(); grid.add_column()
        grid.add_row(price_table, fees_table, timing_table)
        grid.add_row(conf_text, "", "")
        
        return Panel(grid, title=f"📋 {opp.symbol} CARD", border_style="cyan")
    
    def render_health(self):
        h = self.health
        tv = self.trade_verification
        
        sys_table = Table(box=None, show_header=False, padding=(0, 1))
        sys_table.add_column(style="cyan", justify="right")
        sys_table.add_column(style="white", justify="left")
        avg_cycle = h.get("avg_cycle_time", 0)
        fresh = h.get("fresh_prices", 0)
        stale = h.get("stale_prices", 0)
        total = fresh + stale
        errors = h.get("recent_errors", 0)
        
        sys_table.add_row("Cycle:", f"{avg_cycle:.2f}s")
        sys_table.add_row("Fresh:", Text(f"{fresh}/{total}", style="green" if fresh > stale else "yellow"))
        sys_table.add_row("Errors:", Text(f"{errors}", style="red" if errors > 5 else "green"))
        
        tv_table = Table(box=None, show_header=False, padding=(0, 1))
        tv_table.add_column(style="cyan", justify="right")
        tv_table.add_column(style="white", justify="left")
        status = tv.get("status", "unknown")
        status_style = "green" if status == "healthy" else "yellow" if status == "warning" else "dim"
        tv_table.add_row("Trade:", Text(status.upper(), style=status_style))
        tv_table.add_row("PnL var:", f"{tv.get('checks', {}).get('pnl_variance', 0):.4f}")
        warnings = tv.get("warnings", [])
        tv_table.add_row("Warnings:", Text(f"{len(warnings)}", style="bold red" if warnings else "green"))
        if warnings:
            for w in warnings[:2]:
                tv_table.add_row("  └", Text(w[:40], style="red"))
        
        zombie_text = Text()
        zombie_text.append("ZOMBIES:\n", style="bold cyan")
        zombie_count = len(self.zombie_alerts)
        zombie_text.append(f"  Active: ", style="dim")
        zombie_text.append(f"{zombie_count}", style="red" if zombie_count > 10 else "green")
        zombie_text.append(f"  |  Recovered: ", style="dim")
        zombie_text.append(f"{self.recovered_count}", style="green")
        if self.zombie_alerts:
            for z in self.zombie_alerts[:3]:
                zombie_text.append(f"\n  ⚰️ {z[:45]}", style="red")
        else:
            zombie_text.append("\n  ✓ All clean", style="green")
        
        grid = Table.grid(expand=True, padding=1)
        grid.add_column(); grid.add_column(); grid.add_column()
        grid.add_row(sys_table, tv_table, zombie_text)
        
        return Panel(grid, title="🏥 HEALTH MONITOR", border_style="red")
    
    def render_scanner_stats(self):
        pipeline = self.pipeline_stats
        pairs_text = Text()
        if self.top_pairs:
            pairs_text.append("TOP PAIRS:\n", style="bold cyan")
            for i, (pair, count, avg) in enumerate(self.top_pairs[:5], 1):
                pairs_text.append(f"  {i}. {pair} ({count}x, {avg:.2f}%)\n", style="white")
        else:
            pairs_text.append("Collecting...", style="dim italic")
        
        scanner_text = Text()
        scanner_text.append("SCANNER:\n", style="bold cyan")
        scanner_text.append(f"  Checked: {pipeline.total_symbols}\n", style="white")
        scanner_text.append(f"  Candidates: {pipeline.spreads_found}\n", style="white")
        scanner_text.append(f"  Opps: {pipeline.profitable}\n", style="green")
        scanner_text.append(f"  Anomalies: {pipeline.anomalies_skipped}\n", style="red")
        scanner_text.append(f"  Vol skip: {pipeline.volume_skipped}\n", style="yellow")
        scanner_text.append(f"  Net skip: {pipeline.net_negative + pipeline.net_low_skipped}\n", style="yellow")
        scanner_text.append(f"  Zombie/Stale: {pipeline.zombie_skipped}/{pipeline.stale_skipped}\n", style="yellow")
        
        grid = Table.grid(expand=True, padding=1)
        grid.add_column(); grid.add_column()
        grid.add_row(pairs_text, scanner_text)
        return Panel(grid, title="🔍 SCANNER", border_style="magenta")
    
    def render_logs(self):
        text = Text()
        for line in list(self.log_buffer)[-LOG_PANEL_HEIGHT:]:
            text.append(line + "\n")
        return Panel(text, title="📝 LOGS", border_style="blue")
    
    def render(self):
        self.layout["header"].update(self.render_header())
        self.layout["metrics"].update(self.render_metrics())
        self.layout["opportunities"].update(self.render_opportunities())
        self.layout["card"].update(self.render_opportunity_card())
        self.layout["health"].update(self.render_health())
        self.layout["scanner"].update(self.render_scanner_stats())
        self.layout["logs"].update(self.render_logs())
        return self.layout


async def cache_updater(cache, pool, shutdown):
    while not shutdown.is_set():
        try:
            await cache.update_all(pool)
        except Exception as e:
            logger.warning(f"Cache: {e}")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=CACHE_REFRESH_SEC)
        except asyncio.TimeoutError:
            pass


async def emergency_check(portfolio, shutdown, telegram):
    # A2.1 FIX: добавлен промежуточный WARN-уровень (EMERGENCY_WARN_PNL_THRESHOLD,
    # по умолчанию 25%) — раньше между блокировкой новых сделок при 15%
    # просадки (RiskManager) и полной остановкой процесса при 50% убытка не
    # было НИ ОДНОГО промежуточного сигнала оператору. warned-флаг не даёт
    # спамить одним и тем же алертом каждые EMERGENCY_CHECK_INTERVAL, пока
    # состояние не вернётся выше порога.
    warned = False
    while not shutdown.is_set():
        await asyncio.sleep(EMERGENCY_CHECK_INTERVAL)
        try:
            pnl = portfolio.pnl_pct
            if pnl < -EMERGENCY_PNL_THRESHOLD:
                logger.critical(f"🚨 EMERGENCY LOSS: PnL {pnl:.1f}%")
                await telegram.send(f"🚨 EMERGENCY LOSS: PnL {pnl:.1f}%! Останавливаю бота.")
                shutdown.set()
            elif pnl < -EMERGENCY_WARN_PNL_THRESHOLD:
                if not warned:
                    logger.warning(f"⚠️ WARN LOSS: PnL {pnl:.1f}% (порог остановки: -{EMERGENCY_PNL_THRESHOLD}%)")
                    await telegram.send(
                        f"⚠️ Просадка {pnl:.1f}% от исходного депозита. "
                        f"Полная остановка сработает на -{EMERGENCY_PNL_THRESHOLD}%. Стоит проверить бота вручную."
                    )
                    warned = True
            else:
                warned = False
        except Exception:
            pass


async def futures_cache_updater(cache, pool, shutdown):
    while not shutdown.is_set():
        try:
            if pool.futures_clients:
                await cache.update_futures_all(pool)
        except Exception as e:
            logger.debug(f"Futures cache: {e}")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=CACHE_REFRESH_SEC)
        except asyncio.TimeoutError:
            pass


async def reconnect_loop(pool, exchange_health, shutdown):
    """Периодически переподключает проблемные биржи."""
    while not shutdown.is_set():
        try:
            await asyncio.sleep(60)
            for ex_name in list(pool.clients.keys()):
                if not exchange_health.is_healthy(ex_name):
                    logger.warning(f"🔄 Reconnecting {ex_name}...")
                    ok = await pool.reconnect(ex_name)
                    if ok:
                        exchange_health.record_success(ex_name)
                        logger.info(f"✅ {ex_name} reconnected")
                    else:
                        logger.error(f"❌ {ex_name} reconnect failed")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Reconnect loop: {e}")

async def main():
    tg = Telegram()
    await tg.start()
    db = Database()
    await db.start()
    
    pool = ExchangePool()
    if not await pool.init():
        await tg.stop()
        await db.stop()
        return
    
    cache = PriceCache()
    hunter = Hunter(pool)
    lifetime_tracker = LifetimeTracker()
    pair_ranker = ExchangePairRanker()
    confidence_scorer = ConfidenceScorer(pair_ranker, lifetime_tracker)
    finder = OpportunityFinder(confidence_scorer)
    replay = ReplaySystem()
    spread_stability = SpreadStabilityTracker()
    zombie_detector = ZombieDetector()
    trade_verifier = TradeVerifier()
    system_health = SystemHealthMonitor()
    exchange_health = ExchangeHealthMonitor()
    exchange_status = ExchangeStatusMonitor()
    shadow = ShadowLogger()
    funding_tracker = FundingRateTracker()
    
    portfolio = Portfolio(balance=PAPER_BALANCE, initial_balance=PAPER_BALANCE)
    trader = None
    # FIX BUG #1 (UnboundLocalError в live-режиме): переменная использовалась
    # ниже в цикле `finder.find(..., balance_manager=balance_manager)`, но была
    # определена ТОЛЬКО внутри `if TRADING_MODE == "paper"`. При любом другом
    # режиме первый же вызов падал с UnboundLocalError — то есть live-режим
    # не был рабочим вообще. Значение None честно: finder обязан корректно
    # обработать отсутствие баланс-менеджера.
    balance_manager = None
    if TRADING_MODE == "paper":
        per_exchange_balance = PAPER_BALANCE / max(len(pool.clients), 1)
        paper_balances = {ex: {"USDT": per_exchange_balance} for ex in pool.clients}
        balance_manager = BalanceManager(paper_balances)
        if PAPER_EXECUTION_MODEL == "spot_futures":
            trader = SpotFuturesPaperTrader(portfolio, balance_manager)
        else:
            trader = PaperTrader(portfolio, balance_manager=balance_manager)
        
        # Restore saved state if available
        try:
            bal, init_bal, peak, ex_balances = await db.load_portfolio_state()
            if bal is not None and bal > 0:
                portfolio.balance = bal
                portfolio.initial_balance = init_bal or bal
                portfolio.peak_equity = peak or bal
                # Restore per-exchange balances
                if ex_balances:
                    for ex, assets in ex_balances.items():
                        for asset, amount in assets.items():
                            balance_manager.balances.setdefault(ex, {})[asset] = amount
                logger.info(f"💾 Restored portfolio: ${bal:.2f} across {len(ex_balances)} exchanges")
                await db.delete_portfolio_state()
        except Exception as e:
            logger.debug(f"State restore: {e}")
    
    dashboard = Dashboard()
    logger.remove()
    logger.add("logs/hunter.log", rotation="50 MB", retention="7 days", level="INFO")
    
    shutdown = asyncio.Event()
    def signal_handler():
        shutdown.set()
    
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass
    
    try:
        # Warm up ticker cache. Some exchanges may need more than one attempt
        # to return 24h volumes on startup; without this Hunter can start with 0 symbols.
        for attempt in range(3):
            await cache.update_all(pool)
            logger.info(
                f"✅ Кэш прогрев {attempt + 1}/3 за {cache.update_duration:.1f}с | "
                f"vol_symbols={len(cache.volumes)}"
            )
            if cache.volumes:
                break
            await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"❌ Cache init failed: {e}")
        return
    
    symbols = hunter.select(cache.volumes)
    if not symbols:
        symbols = list(pool.symbols_per_exchange.keys())[:MAX_SYMBOLS]
        logger.warning(
            f"⚠️ Hunter returned 0 symbols; fallback to exchange markets: {len(symbols)}"
        )
    pool.update_hunter_symbols(symbols)
    logger.info(f"🎯 Hunter targets: {len(symbols)}")
    await tg.send(f"🎯 v10.2 started | Auto-Recovery | {len(symbols)} targets")
    
    # Проверка orphan-сделок после предыдущего запуска
    try:
        open_trades = await db.get_open_trades()
        if open_trades:
            for ot in open_trades:
                logger.warning(
                    f"⚠️ ORPHAN TRADE on startup: {ot['symbol']} "
                    f"{ot['buy_ex']}→{ot['sell_ex']} ${ot['size_usdt']:.0f}"
                )
            dashboard.add_log(
                f"⚠️ {len(open_trades)} orphan trades from previous run"
            )
    except Exception as e:
        logger.debug(f"Startup orphan check: {e}")
    
    # Initial funding rate fetch
    try:
        await funding_tracker.fetch_all(pool)
        if funding_tracker.rates:
            rates_summary = ", ".join(f"{k}: {v*100:.4f}%" for k, v in funding_tracker.rates.items())
            logger.info(f"💵 Funding rates: {rates_summary}")
    except Exception as e:
        logger.debug(f"Funding init: {e}")
    
    cache_task = asyncio.create_task(cache_updater(cache, pool, shutdown))
    emergency_task = asyncio.create_task(emergency_check(portfolio, shutdown, tg))
    reconnect_task = asyncio.create_task(reconnect_loop(pool, exchange_health, shutdown))
    cache_futures_task = asyncio.create_task(futures_cache_updater(cache, pool, shutdown))
    
    last_alert: Dict[str, float] = {}
    cycle = 0
    best_found = None
    # Счётчики для алерта «возможности есть, сделок нет» (Шаг 0.3).
    # Раньше в плане упоминались metrics.counters и recent_opportunities —
    # таких объектов в коде нет: `metrics` здесь имя панели dashboard, а
    # список последних возможностей не вёлся. Счётчики заведены здесь,
    # по реальным данным цикла.
    opps_seen_last_hour: int = 0
    best_conf_last_hour: float = 0.0
    no_trade_warned_at: float = 0.0
    zombie_alerts: List[str] = []
    known_zombies: Set[str] = set()
    zombie_since: Dict[str, float] = {}
    last_recovery_time = 0  # NEW
    
    with Live(dashboard.render(), refresh_per_second=2, screen=True) as live:
        try:
            while not shutdown.is_set():
                cycle += 1
                start = time.monotonic()
                system_health.record_cycle()
                uptime = time.time() - dashboard.start_time
                in_warmup = uptime < WARMUP_PERIOD_SEC
                
                try:
                    prices = await cache.get_prices(symbols)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    system_health.record_error(f"get_prices: {e}")
                    exchange_health.record_failure("all", str(e)[:60])
                    continue
                
                exchange_health.record_success("all")

                # ═══════════════════════════════════════════════════════════
                # УПРАВЛЕНИЕ ОТКРЫТЫМИ ПАРАМИ (Шаг 0.2, 2026-09-30)
                #
                # SpotFuturesPaperTrader НЕ закрывает ноги в execute(): он
                # открывает пару (спот + шорт фьючерса) и держит её до
                # сходимости базиса, стопа или таймаута. Закрытие происходит
                # здесь, каждый цикл.
                #
                # До этой правки вызова не было вообще: пары открывались и
                # жили бесконечно, PnL не считался, баланс оставался
                # замороженным, а состояние не сохранялось.
                #
                # ВАЖНО про структуры словарей — они разные:
                #   execute():  prices = {exchange: Ticker}
                #               futures_prices = {symbol: {exchange: Ticker}}
                #   здесь:      prices = {symbol: {exchange: Ticker}}
                #               futures_prices = {symbol: {exchange: Ticker}}
                # То есть prices здесь берётся из cache.prices напрямую,
                # а не из результата cache.get_prices(symbols).
                # ═══════════════════════════════════════════════════════════
                if trader is not None and hasattr(trader, "manage_open_positions"):
                    try:
                        # Блокировка НЕ берётся: manage_open_positions()
                        # синхронный (def, не async def) и внутри нет
                        # await, поэтому цикл не может переключиться
                        # посередине — гонок с другими задачами здесь нет.
                        # Попытка взять trader.risk.lock (asyncio.Lock)
                        # синхронным `with` падала с
                        #   "'Lock' object does not support the context
                        #    manager protocol",
                        # а `async with` здесь потребовал бы await внутри
                        # синхронного метода. Оба варианта неверны.
                        closed_pairs = trader.manage_open_positions(
                            prices=cache.prices,
                            futures_prices=cache.futures_prices,
                            cycle=cycle,
                        )
                        for closed in closed_pairs:
                            await db.close_trade(
                                closed.symbol, closed.buy_exchange,
                                closed.sell_exchange, closed.pnl,
                                closed.pnl_pct, closed.balance_after,
                            )
                            # ВАЖНО: PnL НЕ прибавляем к portfolio.balance
                            # вручную. Это уже сделал manage_open_positions()
                            # при расчёте сделки. А record_trade() ниже
                            # прибавляет trade.pnl ЕЩЁ РАЗ, поэтому
                            # дополнительное `portfolio.balance += closed.pnl`
                            # давало двойной счёт (проверено: +0.2531 на
                            # сделке с PnL +0.2531).
                            trader.risk.record_trade(closed)
                            emoji = "\U0001F7E2" if closed.pnl > 0 else "\U0001F534"
                            dashboard.add_log(
                                f"{emoji} CLOSED {closed.symbol} "
                                f"{closed.buy_exchange}\u2192{closed.sell_exchange} "
                                f"PnL: ${closed.pnl:+.4f} ({closed.pnl_pct:+.3f}%)"
                            )
                            logger.info(
                                f"CLOSED {closed.symbol} "
                                f"{closed.buy_exchange}->{closed.sell_exchange} "
                                f"pnl={closed.pnl:+.4f} pnl_pct={closed.pnl_pct:+.3f}% "
                                f"balance={closed.balance_after:.2f}"
                            )
                            if closed.pnl < 0:
                                system_health.record_error(
                                    f"closed loss {closed.symbol} {closed.pnl:.4f}")
                    except Exception as e:
                        # Ошибка управления НЕ должна ронять цикл: иначе
                        # одна битая пара останавливает весь бот.
                        logger.error(
                            f"manage_open_positions failed: {e}", exc_info=True)
                        system_health.record_error(f"manage: {str(e)[:60]}")

                # Update zombie detector
                try:
                    zombie_detector.update(prices)
                except Exception as e:
                    logger.debug(f"Zombie detector: {e}")
                
                stats = PipelineStats()
                all_opps = []
                # Позиции (для MAX_OPEN_POSITIONS/MAX_CORRELATED_POSITIONS)
                # относятся к пакету сделок ОДНОГО цикла — см. комментарий в
                # RiskManager.record_trade про атомарный round-trip execute().
                portfolio.positions.clear()
                
                # Пропускаем zombie check в warmup
                if not in_warmup:
                    now = time.time()
                    for zombie_symbol, first_seen in list(zombie_since.items()):
                        if now - first_seen >= ZOMBIE_TTL_SEC:
                            known_zombies.discard(zombie_symbol)
                            zombie_since.pop(zombie_symbol, None)
                            zombie_detector.clear_zombie(zombie_symbol)
                            logger.info(f"🔄 ZOMBIE TTL RETEST: {zombie_symbol}")

                    for symbol, sym_prices in prices.items():
                        try:
                            is_zombie, reason = zombie_detector.is_zombie(symbol, sym_prices)
                            if is_zombie and symbol not in known_zombies:
                                zombie_alerts.append(f"{symbol}: {reason}")
                                known_zombies.add(symbol)
                                zombie_since[symbol] = time.time()
                                logger.warning(f"⚰️ ZOMBIE: {symbol} - {reason}")
                            elif not is_zombie and symbol in known_zombies:
                                known_zombies.discard(symbol)
                                zombie_since.pop(symbol, None)
                                zombie_detector.mark_recovered(symbol)
                                logger.info(f"✅ RECOVERED: {symbol}")
                        except Exception:
                            pass
                
                # Find opportunities
                for symbol, sym_prices in prices.items():
                    try:
                        opps = finder.find(symbol, sym_prices, stats, skip_zombies=known_zombies, balance_manager=balance_manager, funding_tracker=funding_tracker)
                        all_opps.extend(opps)
                    except Exception as e:
                        logger.debug(f"Find {symbol}: {e}")
                
                try:
                    lifetime_tracker.update(all_opps)
                    for opp in all_opps:
                        replay.record(opp)
                except Exception as e:
                    logger.warning(f"Tracker: {e}")
                
                top = sorted(all_opps, key=lambda x: x.confidence_score, reverse=True)[:DASHBOARD_TOP_N]

                # A0.1 FIX: route3 ("A через B к C") считается по экономике
                # ДВУХХОДОВОГО пути через mid_exchange, но PaperTrader/
                # SpotFuturesPaperTrader.execute() понятия не имеют о поле
                # mid_exchange — они бы тихо исполнили ПРЯМУЮ сделку между
                # buy_exchange и sell_exchange по их сырым ценам, что не имеет
                # отношения к посчитанной (и показанной в дашборде) экономике
                # route3. До полноценной реализации 3-ходового paper-исполнения
                # (отдельная задача — три ноги, три комиссии, реалистичная
                # задержка трансфера между биржами) route3 остаётся видимым в
                # дашборде для анализа, но не участвует в исполнении.
                executable_top = [o for o in top if o.route_type != "route3"]

                # ✅ AUTO-RECOVERY: если >50% зомби — перезагрузить символы
                zombie_pct = (len(known_zombies) / max(len(symbols), 1)) * 100
                now = time.time()
                if (zombie_pct > ZOMBIE_AUTO_RECOVERY_PCT 
                    and now - last_recovery_time > ZOMBIE_AUTO_RECOVERY_INTERVAL
                    and not in_warmup):
                    logger.warning(
                        f"⚰️ AUTO-RECOVERY: {zombie_pct:.0f}% зомби — перезагрузка"
                    )
                    dashboard.add_log(f"⚰️ AUTO-RECO: {zombie_pct:.0f}% зомби")
                    known_zombies.clear()
                    zombie_since.clear()
                    zombie_detector.clear_all()
                    symbols = hunter.select(cache.volumes)
                    pool.update_hunter_symbols(symbols)
                    last_recovery_time = now
                    dashboard.add_log(f"🔄 Reloaded: {len(symbols)} symbols")
                
                if cycle % 5 == 0:
                    dashboard.add_log(
                        f"#{cycle}: pipe=[{stats.spreads_found}→"
                        f"{stats.after_fees}→{stats.profitable}] | "
                        f"zombies={len(known_zombies)}/{len(symbols)} | top={len(top)}"
                    )
                
                # FIX BUG #8 (сделки в warmup). Warmup существует РОВНО для того,
                # чтобы кэш цен стабилизировался: первые циклы дают нулевые/мусорные
                # объёмы и артефактные спреды. Zombie-проверку этот блок пропускал,
                # а вот исполнение сделок — НЕТ: значит, в самый нестабильный период
                # бот торговал по сырым данным. Это гарантированный способ получить
                # убытки на старте и испортить статистику сессии.
                if trader and executable_top and not in_warmup:
                    spread_stability.update(all_opps)
                    # A2.3 FIX (осознанно урезанный скоуп): раньше обрабатывалась
                    # только САМАЯ уверенная возможность за цикл (`top[:1]`), даже
                    # если all_opps содержал десятки других валидных прибыльных
                    # возможностей на непересекающихся активах/биржах — то есть
                    # почти всегда "деньги оставались на столе". Полноценное
                    # ПАРАЛЛЕЛЬНОЕ исполнение (asyncio.gather нескольких сделок
                    # одновременно) сознательно НЕ делается в этом проходе — это
                    # отдельная задача, требующая полноценного нагрузочного
                    # тестирования конкурентности, которое здесь не провести без
                    # реальной биржи. Вместо этого — безопасное расширение:
                    # ПОСЛЕДОВАТЕЛЬНО (один `await` за раз, без gather) пробуем
                    # исполнить до MAX_OPEN_POSITIONS возможностей за цикл, а не
                    # только первую. can_trade() (лимит открытых позиций за
                    # цикл, cooldown по активу, лимит коррелированных категорий)
                    # теперь реально ограничивает пакет — см. фикс в
                    # RiskManager.record_trade/portfolio.positions.clear() рядом,
                    # который чинит найденный по ходу этой правки баг: раньше
                    # p.positions вообще никогда не заполнялся, и оба этих лимита
                    # были мертвы (len(p.positions) всегда 0).
                    for opp in executable_top[:MAX_OPEN_POSITIONS]:
                        # FIX BUG #2 (утечка l2 между итерациями): ниже стояло
                        # `l2_obj = locals().get('l2')`. locals() отдаёт ПЕРЕМЕННЫЕ
                        # ФУНКЦИИ, а не итерации цикла, поэтому если в прошлой
                        # итерации `l2` был получен, а в текущей — нет (L2 выключен
                        # или ветка не сработала), в shadow_log писались ЧУЖИЕ данные
                        # от другой сделки. Это тихо искажало expected EV и fill
                        # probability — то есть ломало именно ту статистику,
                        # которая должна была ловить обман.
                        # Теперь переменная явная и обнуляется на каждой итерации.
                        l2_obj = None
                        # Порог берётся из RiskManager, а не из литерала 50.
                        # Раньше здесь стояло `opp.confidence_score >= 50`,
                        # из-за чего динамический порог в RiskManager был
                        # бы мёртвым: app.py отсекал бы возможности ДО того,
                        # как дошло до вызова can_trade(). Два разных порога
                        # в двух местах — источник расхождений.
                        #
                        # Владелец RiskManager здесь — trader.risk, отдельной
                        # переменной `risk` в main() нет.
                        conf_threshold = trader.risk.get_confidence_threshold()
                        if opp.confidence_score >= conf_threshold and opp.net_profit_pct > 0:
                            try:
                                if not exchange_health.is_healthy(opp.buy_exchange) or not exchange_health.is_healthy(opp.sell_exchange):
                                    if cycle % 5 == 0:
                                        bad = opp.buy_exchange if not exchange_health.is_healthy(opp.buy_exchange) else opp.sell_exchange
                                        dashboard.add_log(f"⏭️ {opp.symbol} blocked: {bad} unhealthy")
                                    continue
                                delay_reason = ExecutionDelaySimulator.adjust_opportunity(opp, spread_stability)
                                if delay_reason:
                                    replay.record_decision(
                                        opp, "blocked", delay_reason,
                                        stability_score=spread_stability.stability_score(opp),
                                        decay_rate=spread_stability.decay_rate(opp),
                                    )
                                    if cycle % 5 == 0:
                                        dashboard.add_log(f"⏭️ {opp.symbol} blocked: {delay_reason}")
                                    continue
                                reality = RealityCheck.check(
                                    opp, prices.get(opp.symbol, {}), portfolio,
                                    confidence_min=conf_threshold,
                                )
                                if not reality.allowed:
                                    if (L2_REALITY_CHECK_ENABLED
                                            and reality.reason in {"spread_requires_l2", "wide_internal_spread"}):
                                        l2_obj = await OrderBookExecutionAnalyzer.check(
                                            pool, opp, reality.planned_size_usdt
                                        )
                                        if not l2_obj.allowed:
                                            replay.record_decision(
                                                opp, "blocked", l2_obj.reason,
                                                planned_size_usdt=reality.planned_size_usdt,
                                                fill_probability=l2_obj.fill_probability,
                                                expected_value_usdt=reality.expected_value_usdt,
                                                stability_score=spread_stability.stability_score(opp),
                                                decay_rate=spread_stability.decay_rate(opp),
                                            )
                                            pair_ranker.record(opp, executed=False)
                                            if cycle % 5 == 0:
                                                dashboard.add_log(f"⏭️ {opp.symbol} L2 blocked: {l2_obj.reason}")
                                            continue
                                        # FIX BUG #12: было `min(opp.max_safe_size_usdt
                                        # or l2.max_executable_usdt, ...)`. При
                                        # max_safe_size_usdt == 0 выражение `0 or X`
                                        # даёт X — то есть НУЛЕВОЙ размер заменялся на
                                        # положительный, и сделка проходила с размером,
                                        # который сценарий признал недопустимым.
                                        # Теперь None отделён от валидного нуля.
                                        _cur = opp.max_safe_size_usdt
                                        opp.max_safe_size_usdt = (
                                            l2_obj.max_executable_usdt
                                            if _cur is None
                                            else min(_cur, l2_obj.max_executable_usdt)
                                        )
                                        opp.net_profit_pct = l2_obj.expected_net_pct
                                        opp.execution_quality = max(50, min(100, l2_obj.fill_probability * 100))
                                    else:
                                        replay.record_decision(
                                            opp, "blocked", reality.reason,
                                            planned_size_usdt=reality.planned_size_usdt,
                                            fill_probability=reality.fill_probability,
                                            expected_value_usdt=reality.expected_value_usdt,
                                            stability_score=spread_stability.stability_score(opp),
                                            decay_rate=spread_stability.decay_rate(opp),
                                        )
                                        pair_ranker.record(opp, executed=False)
                                        if cycle % 5 == 0:
                                            dashboard.add_log(
                                                f"⏭️ {opp.symbol} blocked: {reality.reason} | "
                                                f"conf={opp.confidence_score:.1f} net={opp.net_profit_pct:.2f}% "
                                                f"spread={opp.spread_pct:.2f}% ev=${reality.expected_value_usdt:.2f}"
                                            )
                                        continue
                                elif L2_REALITY_CHECK_ENABLED:
                                    l2_obj = await OrderBookExecutionAnalyzer.check(
                                        pool, opp, reality.planned_size_usdt
                                    )
                                    if not l2_obj.allowed:
                                        replay.record_decision(
                                            opp, "blocked", l2_obj.reason,
                                            planned_size_usdt=reality.planned_size_usdt,
                                            fill_probability=l2_obj.fill_probability,
                                            expected_value_usdt=reality.expected_value_usdt,
                                            stability_score=spread_stability.stability_score(opp),
                                            decay_rate=spread_stability.decay_rate(opp),
                                        )
                                        pair_ranker.record(opp, executed=False)
                                        if cycle % 5 == 0:
                                            dashboard.add_log(f"⏭️ {opp.symbol} L2 blocked: {l2_obj.reason}")
                                        continue
                                    # FIX BUG #12 — см. комментарий в первой ветке.
                                    _cur2 = opp.max_safe_size_usdt
                                    opp.max_safe_size_usdt = (
                                        l2_obj.max_executable_usdt
                                        if _cur2 is None
                                        else min(_cur2, l2_obj.max_executable_usdt)
                                    )
                                    opp.net_profit_pct = l2_obj.expected_net_pct
                                    opp.execution_quality = max(50, min(100, l2_obj.fill_probability * 100))
                                buy_ticker = prices.get(opp.symbol, {}).get(opp.buy_exchange)
                                if buy_ticker:
                                    valid = OrderValidator.check(
                                        pool, opp.buy_exchange, opp.symbol,
                                        reality.planned_size_usdt, buy_ticker.ask
                                    )
                                    if not valid.valid:
                                        if cycle % 5 == 0:
                                            dashboard.add_log(
                                                f"⏭️ {opp.symbol} blocked: {valid.reason}"
                                            )
                                        continue
                                # FIX BUG #3 (мутация opp в цикле -> bias в БД).
                                # L2-анализ ПРАВИТ opp.net_profit_pct/max_safe_size/
                                # execution_quality. Но те же объекты лежат в
                                # all_opps, который ниже целиком сохраняется в БД и
                                # скармливается pair_ranker. В итоге в историю
                                # попадали значения ПОСЛЕ исполненческого уточнения,
                                # а для отклонённых сделок — исходные. Систематический
                                # bias: выборка «исполнено» и выборка «все возможности»
                                # становились несопоставимыми, а любая ML-модель на
                                # этих фичах обучалась бы на подправленных данных.
                                # Исполняем КОПИЮ, оригинал не трогаем.
                                exec_opp = copy.deepcopy(opp)
                                exec_opp.net_profit_pct = opp.net_profit_pct
                                trade = await trader.execute(
                                    exec_opp, prices.get(opp.symbol, {}),
                                    # Словарь фьючерсов передаётся ЦЕЛИКОМ, а не
                                    # по ключу opp.symbol: спот называется
                                    # "SOL/USDT", перпетуал — "SOL/USDT:USDT",
                                    # и .get(opp.symbol) молча возвращал {}.
                                    # Ключ подбирает resolve_futures_symbol()
                                    # внутри execute().
                                    futures_prices=cache.futures_prices,
                                    funding_tracker=funding_tracker,
                                )

                                # ═════════════════════════════════════════════
                                # РАЗДЕЛЕНИЕ МОДЕЛЕЙ ИСПОЛНЕНИЯ (Шаг 0.2)
                                #
                                # Два трейдера с РАЗНЫМ контрактом:
                                #
                                #  PaperTrader (спот против спота) —
                                #      execute() атомарен: купил и продал, вернул
                                #      готовую TradeResult. Старый путь ниже
                                #      остаётся без изменений.
                                #
                                #  SpotFuturesPaperTrader (спот + шорт фьючерса) —
                                #      execute() ОТКРЫВАЕТ пару и возвращает None.
                                #      Ноги держатся до сходимости базиса,
                                #      закрываются в manage_open_positions()
                                #      в начале следующего цикла.
                                #
                                # Раньше для обоих стоял один путь `if trade:`.
                                # Для spot_futures он не срабатывал НИКОГДА
                                # (execute отдаёт None), из-за чего:
                                #   - открытые пары не попадали в БД;
                                #   - при старте бот не видел своих позиций;
                                #   - close_trade вызывался бы сразу после
                                #     открытия, то есть на пустом месте.
                                # ═════════════════════════════════════════════
                                is_holding_trader = hasattr(trader, "open_pairs")

                                if is_holding_trader:
                                    # Пара могла открыться, а могла быть
                                    # отклонена. Сверяемся с состоянием,
                                    # а не с возвратом execute().
                                    if (opp.symbol in trader.open_pairs
                                            and not trader.open_pairs[opp.symbol].notes):
                                        pair = trader.open_pairs[opp.symbol]
                                        # Помечаем, чтобы повторно не писать
                                        await db.save_open_trade(
                                            opp, pair.size_usdt, opp.category)
                                        pair.notes = "saved"
                                        pair_ranker.record(opp, executed=True)
                                        replay.record_decision(
                                            opp, "executed", "opened",
                                            planned_size_usdt=pair.size_usdt,
                                            expected_value_usdt=0.0,
                                        )
                                        dashboard.add_log(
                                            f"\U0001F4E6 OPEN {opp.symbol} "
                                            f"{pair.buy_exchange}\u2192{pair.sell_exchange} "
                                            f"${pair.size_usdt:.2f} basis "
                                            f"{pair.entry_basis:+.3f}%"
                                        )
                                        logger.info(
                                            f"OPEN {opp.symbol} "
                                            f"{pair.buy_exchange}->{pair.sell_exchange} "
                                            f"size={pair.size_usdt:.2f} "
                                            f"basis={pair.entry_basis:+.3f}%"
                                        )
                                if trade and PARTIAL_FILL_ENABLED:
                                    trade = PartialFillSimulator.apply(
                                        trade, opp.max_safe_size_usdt, reality.planned_size_usdt
                                    )
                                if trade and not is_holding_trader:
                                    # A0.3 FIX: раньше save_open_trade вызывался ДО
                                    # trader.execute() — то есть до того, как было
                                    # известно, состоится ли сделка вообще. Если
                                    # execute() отклонял сделку (risk-check, нехватка
                                    # резерва и т.п. — самый частый случай), запись
                                    # "open" оставалась в БД навсегда: ровно то, что
                                    # бот сам детектирует при старте/остановке как
                                    # "ORPHAN TRADE". Теперь open-запись создаётся
                                    # только когда сделка реально прошла, и сразу же
                                    # закрывается — в текущей paper-модели execute()
                                    # атомарен (нет await внутри), так что окна между
                                    # "открыли" и "закрыли", где могло бы упасть
                                    # что-то ещё, не существует. Если в будущем
                                    # появится реальное (Track B) исполнение с
                                    # честным окном между ногами сделки — сюда нужно
                                    # будет вернуть открытие ДО коммита средств,
                                    # именно ради recovery после краша в этом окне.
                                    await db.save_open_trade(
                                        opp, reality.planned_size_usdt, opp.category
                                    )
                                    replay.record_decision(
                                        opp, "executed", "ok",
                                        planned_size_usdt=reality.planned_size_usdt,
                                        fill_probability=reality.fill_probability,
                                        expected_value_usdt=reality.expected_value_usdt,
                                        pnl=trade.pnl,
                                        stability_score=spread_stability.stability_score(opp),
                                        decay_rate=spread_stability.decay_rate(opp),
                                    )
                                    await db.close_trade(
                                        trade.symbol, trade.buy_exchange, trade.sell_exchange,
                                        trade.pnl, trade.pnl_pct, trade.balance_after
                                    )
                                    pair_ranker.record(opp, executed=True)
                                    trade_verifier.record_trade(trade)
                                    # FIX BUG #2 (продолжение): больше НЕ
                                    # locals().get('l2'). l2_obj — явная
                                    # переменная, обнулённая в начале итерации,
                                    # поэтому в лог попадают данные ИМЕННО этой
                                    # сделки, а не предыдущей.
                                    shadow.record(
                                        opp, trade,
                                        expected_ev=getattr(reality, 'expected_value_usdt', 0),
                                        expected_fill_prob=getattr(reality, 'fill_probability', 0),
                                        planned_size=getattr(reality, 'planned_size_usdt', 0),
                                        l2_max_exec=getattr(l2_obj, 'max_executable_usdt', 0) if l2_obj else 0,
                                    )
                                    emoji = "🟢" if trade.pnl > 0 else "🔴"
                                    dashboard.add_log(
                                        f"{emoji} {trade.symbol} | "
                                        f"conf={opp.confidence_score:.0f} | "
                                        f"PnL: ${trade.pnl:+.4f}"
                                    )
                                elif cycle % 5 == 0:
                                    # Раньше здесь не было вообще никакого лога —
                                    # сделка проходила все проверки (L2, RealityCheck,
                                    # OrderValidator) и молча отклонялась внутри
                                    # trader.execute() без единого следа в дашборде.
                                    dashboard.add_log(
                                        f"⏭️ {opp.symbol} blocked: executor rejected "
                                        f"(risk-check/balance)"
                                    )
                            except Exception as e:
                                logger.warning(f"Trade: {e}")
                
                for opp in all_opps:
                    pair_ranker.record(opp, executed=False)
                    try:
                        await db.save_opportunity(opp)
                    except Exception:
                        pass
                
                for opp in top:
                    if opp.net_profit_pct >= ALERT_MIN_NET_PROFIT and opp.confidence_score >= 70:
                        if (time.time() - last_alert.get(opp.symbol, 0)) >= ALERT_COOLDOWN_SEC:
                            await tg.send(
                                f"🔥 <b>{opp.symbol}</b>\n"
                                f"Conf: <b>{opp.confidence_score:.0f}</b> | "
                                f"Net: <b>{opp.net_profit_pct:.2f}%</b>\n"
                                f"Max Size: <b>${opp.max_safe_size_usdt:.0f}</b>\n"
                                f"{opp.buy_exchange}→{opp.sell_exchange}"
                            )
                            last_alert[opp.symbol] = time.time()
                            if not best_found or opp.confidence_score > best_found.confidence_score:
                                best_found = opp
                
                # АЛЕРТ: возможности есть, сделок нет (Шаг 0.3).
                #
                # Смысл: петля «confidence ниже порога -> нет сделок -> нет
                # истории -> confidence ещё ниже» внешне неотличима от
                # «рынка нет». Этот блок делает её видимой сразу.
                try:
                    if all_opps:
                        opps_seen_last_hour += len(all_opps)
                        best_conf_last_hour = max(
                            best_conf_last_hour,
                            max(o.confidence_score for o in all_opps))
                    if cycle % 60 == 0:      # ~3 мин при цикле 3 с
                        now_ts = time.time()
                        recent_closed = sum(
                            1 for t in portfolio.closed_trades
                            if now_ts - t.timestamp < 3600)
                        if opps_seen_last_hour > 0 and recent_closed == 0:
                            if now_ts - no_trade_warned_at > 1800:
                                no_trade_warned_at = now_ts
                                thr = trader.risk.get_confidence_threshold()
                                msg = (
                                    f"0 trades/hour despite "
                                    f"{opps_seen_last_hour} opportunities | "
                                    f"best_conf={best_conf_last_hour:.1f} "
                                    f"threshold={thr} | "
                                    f"closed_total={len(portfolio.closed_trades)}"
                                )
                                logger.warning("⚠️ " + msg)
                                dashboard.add_log("⚠️ " + msg)
                                opps_seen_last_hour = 0
                                best_conf_last_hour = 0.0
                except Exception as e:
                    logger.debug(f"No-trade alert: {e}")

                try:
                    exchange_health.log_warnings(logger)
                    trader_stats = trader.stats() if trader else {}
                    cache_stats = {
                        "age": cache.get_cache_age(),
                        "duration": cache.update_duration,
                        "symbols": len(symbols),
                    }
                    top_pairs = pair_ranker.get_top_pairs(5)
                    health = system_health.get_health(prices)
                    tv_report = trade_verifier.get_health_report()
                    dashboard.update(
                        cycle, top, trader_stats, cache_stats, stats,
                        best_found, top_pairs, health, tv_report, 
                        zombie_alerts, zombie_detector.recovered_count
                    )
                    live.update(dashboard.render())
                except Exception as e:
                    logger.warning(f"Dashboard: {e}")
                
                if cycle % 50 == 0:
                    shadow.log_summary(logger)
                
                if cycle % 60 == 0:
                    try:
                        unhealthy = exchange_status.unhealthy()
                        if unhealthy:
                            for ex in unhealthy:
                                logger.warning(f"🚫 Exchange {ex} not operational")
                    except Exception:
                        pass

                if cycle % SYMBOL_RELOAD_CYCLES == 0:
                    symbols = hunter.select(cache.volumes)
                    zombie_detector.prune(set(symbols))

                # FIX BUG #7 (REST-шторм). Ниже стояло безусловное
                # `await exchange_status.check_all(pool)` — при SCAN_INTERVAL_SEC = 2
                # это 30 REST-запросов в минуту к служебным эндпоинтам всех бирж.
                # Биржи режут по rate-limit, и при блокировке сканер теряет именно
                # ту частоту данных, ради которой он существует.
                if cycle % EXCHANGE_STATUS_INTERVAL_CYCLES == 0:
                    await exchange_status.check_all(pool)
                
                if cycle % ALERT_CLEANUP_INTERVAL_CYCLES == 0:
                    # FIX BUG #5 (утечка памяти): last_alert пополнялся каждый цикл
                    # и никогда не чистился. За месяц работы на тысяче символов это
                    # сотни KB и растущий риск утечки при long-running процессе.
                    # Записи старше ALERT_COOLDOWN_SEC потеряли смысл: cooldown
                    # всё равно истёк бы.
                    _cutoff = time.time() - ALERT_COOLDOWN_SEC * 10
                    last_alert = {k: v for k, v in last_alert.items()
                                  if v > _cutoff}

                sleep = max(0.5, SCAN_INTERVAL_SEC - (time.monotonic() - start))
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=sleep)
                except asyncio.TimeoutError:
                    pass
        
        except KeyboardInterrupt:
            pass
        
        finally:
            shutdown.set()
            for task in [cache_task, emergency_task, reconnect_task, cache_futures_task]:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # Проверяем orphan-сделки при shutdown
            try:
                orphan = await db.get_open_trades()
                if orphan:
                    for ot in orphan:
                        logger.warning(
                            f"⚠️ ORPHAN at shutdown: {ot['symbol']} "
                            f"{ot['buy_ex']}→{ot['sell_ex']} ${ot['size_usdt']:.0f}"
                        )
            except Exception:
                pass
            
            # Save portfolio state before shutdown
            try:
                if trader and hasattr(trader, 'balance_manager') and trader.balance_manager:
                    await db.save_portfolio_state(portfolio, trader.balance_manager)
                else:
                    await db.save_portfolio_state(portfolio)
            except Exception:
                pass
            
            await pool.close()
            await db.stop()
            await tg.stop()
            
            if trader:
                try:
                    s = trader.stats()
                    logger.info(
                        f"\n📊 FINAL: ${s['balance']:.2f} | PnL {s['pnl']:+.2f} "
                        f"({s['pnl_pct']:+.2f}%) | Trades {s['total']}"
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

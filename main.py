import asyncio
import logging
import random
import sys
import time
from config import Config
from pacifica_client import PacificaClient
from paradex_client import ParadexClient
from telegram_bot import TelegramNotifier

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HedgingBot")

class HedgingBot:
    def __init__(self):
        self.pac = PacificaClient()
        self.par = ParadexClient()
        self.tg = TelegramNotifier()
        self.running = True

    async def setup(self):
        logger.info("🤖 Bot initializing...")
        await self.pac.fetch_exchange_info()
        connected = await self.par.connect()
        if not connected:
            logger.error("Failed to connect to Paradex. Exiting.")
            await self.tg.send_message("❌ Bot Start Failed: Paradex Connect Error", level="CRITICAL")
            sys.exit(1)
        await self.tg.send_message("✅ Bot Started Successfully", level="INFO")

    async def global_scan(self):
        """Phase 1: 全局扫描 - 检查脏仓位并清理僵尸挂单"""
        logger.info("🔍 Phase 1: Global Scan & Cleanup...")
        
        # 1. 尝试取消 Paradex 所有挂单
        cancel_tasks = [self.par.cancel_all_open_orders(s) for s in Config.TARGET_COINS]
        await asyncio.gather(*cancel_tasks)

        # 2. 获取仓位
        pac_pos_task = [self.pac.get_position(s) for s in Config.TARGET_COINS]
        par_pos_task = [self.par.get_position(s) for s in Config.TARGET_COINS]
        
        results = await asyncio.gather(*(pac_pos_task + par_pos_task))
        
        mid = len(Config.TARGET_COINS)
        pac_res = results[:mid]
        par_res = results[mid:]
        
        dirty_symbols = set()
        
        for p in pac_res:
            if abs(p['size']) > 0:
                logger.warning(f"⚠️ Pacifica has dirty pos: {p['symbol']} {p['size']}")
                dirty_symbols.add(p['symbol'])
        
        for p in par_res:
            if abs(p['size']) > 0:
                logger.warning(f"⚠️ Paradex has dirty pos: {p['symbol']} {p['size']}")
                dirty_symbols.add(p['symbol'])
                
        if dirty_symbols:
            logger.warning(f"🚨 Dirty Account Detected! Symbols: {dirty_symbols}")
            return True, list(dirty_symbols)
        
        logger.info("✅ Account is Clean and Orders Cleared.")
        return False, []

    async def analyze_funding_opportunities(self):
        """Phase 1.5: 分析套利机会"""
        logger.info("📊 Phase 1.5: Analyzing Funding Rates...")
        
        opportunities = []
        pac_tasks = [self.pac.get_funding_rate(s) for s in Config.TARGET_COINS]
        par_tasks = [self.par.get_funding_rate(s) for s in Config.TARGET_COINS]
        
        pac_rates = await asyncio.gather(*pac_tasks, return_exceptions=True)
        par_rates = await asyncio.gather(*par_tasks, return_exceptions=True)
        
        for i, symbol in enumerate(Config.TARGET_COINS):
            r_pac = pac_rates[i]
            r_par = par_rates[i]
            
            if isinstance(r_pac, Exception) or r_pac is None: continue
            if isinstance(r_par, Exception) or r_par is None: continue
            
            spread_short_pac = r_pac - r_par
            spread_long_pac = r_par - r_pac
            
            if spread_short_pac > spread_long_pac:
                best_spread_hourly = spread_short_pac
                pac_side = "SELL"
            else:
                best_spread_hourly = spread_long_pac
                pac_side = "BUY"
                
            apy = best_spread_hourly * 24 * 365
            
            if apy >= Config.MIN_OPEN_APY:
                opportunities.append({
                    "symbol": symbol,
                    "pac_side": pac_side,
                    "apy": apy,
                    "hourly_spread": best_spread_hourly
                })
        
        if not opportunities:
            return None, None, None
            
        opportunities.sort(key=lambda x: x['apy'], reverse=True)
        best = opportunities[0]
        
        logger.info(f"🏆 Target Found: {best['symbol']} (Pac {best['pac_side']}) APY {best['apy']*100:.2f}%")
        return best['symbol'], best['pac_side'], best['hourly_spread']

    async def check_price_spread(self, symbol, pac_side):
        """检查开仓价格磨损"""
        pac_bid, pac_ask = await self.pac.get_best_bid_ask(symbol)
        par_bid, par_ask = await self.par.get_best_bid_ask(symbol)
        
        if not (pac_bid and pac_ask and par_bid and par_ask):
            return False

        if pac_side == "BUY":
            cost_pct = (pac_ask - par_bid) / par_bid
        else:
            cost_pct = (par_ask - pac_bid) / pac_bid

        if cost_pct > Config.MAX_OPEN_SPREAD:
            logger.warning(f"⛔ Spread Risk: {symbol} Cost {cost_pct*100:.2f}% > Limit {Config.MAX_OPEN_SPREAD*100:.2f}%")
            return False
        return True

    async def execute_dual_open(self, symbol, pac_side):
        """Phase 2: 优化的开仓逻辑"""
        par_side = "sell" if pac_side == "BUY" else "buy"
        usd_amt = random.uniform(Config.MIN_USD, Config.MAX_USD)
        
        if not await self.check_price_spread(symbol, pac_side): return False

        logger.info(f"🚁 Pre-flight Check for {symbol}...")
        is_healthy = await self.par.check_health(required_usd=usd_amt * 1.1)
        if not is_healthy:
            logger.error("⛔ Paradex Check Failed. Abort.")
            return False

        bid, ask = await self.pac.get_best_bid_ask(symbol)
        price = (bid + ask) / 2
        target_qty = usd_amt / price
        
        logger.info(f"🚀 Executing: {symbol} Target=${usd_amt:.1f} ({target_qty:.4f})")
        
        # 3. Pacifica 开仓
        await self.pac.execute_smart_maker(symbol, pac_side, target_qty, timeout=120)
        
        pac_pos = await self.pac.get_position(symbol)
        real_pac_size = abs(pac_pos['size'])
        
        if real_pac_size < (target_qty * 0.05): 
            logger.warning(f"⚠️ Pacifica fill too small. Cancel & Skip.")
            return False

        logger.info(f"✅ Pacifica Filled: {real_pac_size:.4f}. 👉 Step 2: Paradex Hedge...")

        # 4. Paradex 对冲
        par_success = await self.par.execute_smart_maker(symbol, par_side, real_pac_size, timeout=45, aggressive=True)
        
        if par_success:
            logger.info("🎉 Dual Open Success!")
            await self.tg.send_message(f"🟢 Open Success: {symbol}\nAmt: {real_pac_size:.4f}\nPac: {pac_side}, Par: {par_side}")
            return True
        else:
            logger.critical(f"🚨 HEDGE FAILED! Triggering ROLLBACK!")
            await self.tg.send_message(f"🚨 HEDGE FAILED: {symbol}\nParadex failed to open. Rolling back Pacifica.", level="CRITICAL")
            await self.emergency_rollback(symbol)
            return False

    async def emergency_rollback(self, symbol):
        logger.info(f"🔥 ROLLBACK: Closing {symbol}...")
        await self.pac.panic_close(symbol)
        await self.par.execute_smart_maker(symbol, "sell", 0, is_close=True, aggressive=True)

    # ================= NEW: 安全平仓逻辑 =================

    async def safe_universal_close(self, target_symbols=None):
        """
        Phase 3: 安全平仓 (Safe Universal Close)
        流程：降落检查 -> 并发开火 -> 循环监控 -> 超时报警
        """
        symbols = target_symbols if target_symbols else Config.TARGET_COINS
        logger.info(f"🏁 Initiating Safe Close for: {symbols}")

        # --- Step 1: 降落检查 (The Landing Check) ---
        # 必须确保两边 API 都是活的，且认证有效，才开始动作，否则单边平仓风险极大
        check_pass = False
        while not check_pass:
            logger.info("📡 Performing Landing Check (Connectivity & Auth)...")
            
            # 并发检查 Pacifica (带签名) 和 Paradex (查余额)
            # check_auth_health 返回 True/False
            pac_ok_task = self.pac.check_auth_health()
            par_ok_task = self.par.check_health(required_usd=0) # 只查连接，不查金额

            pac_ok, par_ok = await asyncio.gather(pac_ok_task, par_ok_task)

            if pac_ok and par_ok:
                logger.info("✅ Landing Check Passed. Networks are healthy.")
                check_pass = True
            else:
                logger.warning(f"⛔ Landing Check Failed (Pac:{pac_ok}, Par:{par_ok}). Waiting 10s...")
                await asyncio.sleep(10)
                # 可以在这里加一个逻辑，如果已经持仓很久了还不能平，也需要报警，暂时略过

        # --- Step 2: 并发开火 (Simultaneous Execution) ---
        logger.info("🚀 Landing Check OK. Firing Close Commands...")
        start_close_time = time.time()
        alert_sent = False

        # --- Step 3: 循环监控与重试 (Monitor & Retry Loop) ---
        while True:
            # 1. 检查当前仓位
            pac_pos_tasks = [self.pac.get_position(s) for s in symbols]
            par_pos_tasks = [self.par.get_position(s) for s in symbols]
            
            # 获取所有仓位数据
            all_pos = await asyncio.gather(*(pac_pos_tasks + par_pos_tasks))
            mid = len(symbols)
            current_pac_pos = all_pos[:mid]
            current_par_pos = all_pos[mid:]

            # 统计剩余仓位
            remaining_symbols = []
            log_msg = []
            
            # 找出还有仓位的币种
            for i, s in enumerate(symbols):
                pac_sz = abs(current_pac_pos[i]['size'])
                par_sz = abs(current_par_pos[i]['size'])
                
                if pac_sz > 0 or par_sz > 0:
                    remaining_symbols.append(s)
                    log_msg.append(f"{s}(Pac:{pac_sz:.4f}, Par:{par_sz:.4f})")
            
            # 如果全部清零，任务完成
            if not remaining_symbols:
                logger.info("🎉 All Positions Closed Successfully.")
                # 如果之前报过警，现在解除
                if alert_sent:
                     await self.tg.send_message("✅ Emergency Resolved: All positions finally closed.", level="INFO")
                return

            # --- Step 4: 超时报警 (Watchdog) ---
            elapsed = time.time() - start_close_time
            if elapsed > Config.CLOSE_TIMEOUT_ALERT:
                if not alert_sent:
                    msg = (f"🚨 **EMERGENCY: CLOSE TIMEOUT**\n"
                           f"Bot has been trying to close for > 10 mins!\n"
                           f"Remaining: {', '.join(log_msg)}\n"
                           f"Please check exchange connectivity immediately!")
                    await self.tg.send_message(msg, level="CRITICAL")
                    alert_sent = True
                    # 即使报警了，也继续尝试平仓，死马当活马医

            logger.info(f"⏳ Closing in progress... Remaining: {', '.join(log_msg)}")

            # 对剩余的币种发送平仓指令 (并发)
            tasks = []
            for s in remaining_symbols:
                # is_close=True 会触发激进模式 (Aggressive)
                tasks.append(asyncio.create_task(self.pac.execute_smart_maker(s, "SELL", 0, is_close=True, timeout=10)))
                tasks.append(asyncio.create_task(self.par.execute_smart_maker(s, "sell", 0, is_close=True, aggressive=True, timeout=10)))
            
            # 等待一轮执行 (设置较短的 timeout 防止卡死)
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # 短暂休息进入下一轮检查
            await asyncio.sleep(5)

    async def smart_monitor_loop(self, symbol, pac_side, hold_time):
        logger.info(f"☕ Monitoring {symbol} for {hold_time}s...")
        start_time = time.time()
        while time.time() - start_time < hold_time:
            remaining = hold_time - (time.time() - start_time)
            sleep_duration = min(Config.FUNDING_CHECK_INTERVAL, remaining)
            if sleep_duration <= 0: break
            await asyncio.sleep(sleep_duration)
            
            r_pac = await self.pac.get_funding_rate(symbol)
            r_par = await self.par.get_funding_rate(symbol)
            if r_pac is not None and r_par is not None:
                spread = (r_pac - r_par) if pac_side == "SELL" else (r_par - r_pac)
                apy = spread * 24 * 365
                logger.info(f"💓 {symbol} APY: {apy*100:.2f}%")
                if apy < Config.MIN_CLOSE_APY:
                    logger.warning("📉 APY Dropped. Early Close.")
                    return 

    async def run(self):
        await self.setup()
        while self.running:
            try:
                need_rescue, dirty_symbols = await self.global_scan()
                if need_rescue:
                    # 使用新的安全平仓逻辑
                    await self.safe_universal_close(dirty_symbols)
                else:
                    symbol, pac_side, spread = await self.analyze_funding_opportunities()
                    if symbol:
                        success = await self.execute_dual_open(symbol, pac_side)
                        if success:
                            hold_time = random.randint(*Config.HOLD_RANGE)
                            await self.smart_monitor_loop(symbol, pac_side, hold_time)
                            # 使用新的安全平仓逻辑
                            await self.safe_universal_close([symbol])
                        else:
                            logger.info("⚠️ Operation aborted. Cooling down.")
                    else:
                        logger.info("😴 No opportunities. Sleeping...")
                        await asyncio.sleep(300)
                await asyncio.sleep(random.randint(10, 60))
            except KeyboardInterrupt:
                logger.info("🛑 Stopping Bot...")
                break
            except Exception as e:
                logger.error(f"Loop Error: {e}", exc_info=True)
                await self.tg.send_message(f"⚠️ Bot Main Loop Error: {str(e)}", level="WARNING")
                await asyncio.sleep(5)
        
        await self.pac.close_session()
        await self.par.close_session()
        await self.tg.close()

if __name__ == "__main__":
    bot = HedgingBot()
    asyncio.run(bot.run())
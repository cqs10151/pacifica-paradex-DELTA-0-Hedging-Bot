import asyncio
import logging
import time
import json
import ccxt.pro as ccxt
from config import Config

logger = logging.getLogger("Paradex")

class ParadexClient:
    def __init__(self):
        self.config = {
            'apiKey': Config.PAR_ADDR,
            'secret': Config.PAR_KEY,
            'privateKey': Config.PAR_KEY,
            'walletAddress': Config.PAR_ADDR,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        }
        self.exchange = None
        self.markets_loaded = False

    async def connect(self):
        try:
            self.exchange = ccxt.paradex(self.config)
            await self.exchange.load_markets()
            self.markets_loaded = True
            logger.info("Paradex Connected.")
            return True
        except Exception as e:
            logger.error(f"Paradex Connect Error: {e}")
            return False

    async def close_session(self):
        if self.exchange: 
            await self.exchange.close()

    async def check_health(self, required_usd):
        """[Optimization] 飞行前检查: 调用 raw API 获取真实购买力"""
        if not self.exchange: 
            return False
        try:
            start = time.time()
            available_usd = 0.0
            total_equity = 0.0
            source_used = "ccxt_balance"
            
            try:
                # 隐式 API 调用: GET /account
                raw_account = await self.exchange.private_get_account()
                if isinstance(raw_account, dict):
                    available_usd = float(raw_account.get('free_collateral', 0.0) or 0.0)
                    total_equity = float(raw_account.get('account_value', 0.0) or 0.0)
                    source_used = "raw_api"
            except Exception as raw_e:
                pass

            if source_used != "raw_api":
                bal = await self.exchange.fetch_balance()
                usdc = bal.get('USDC', {})
                available_usd = float(usdc.get('free', 0.0) or 0.0)
                total_equity = float(usdc.get('total', 0.0) or 0.0)

            latency = (time.time() - start) * 1000
            
            # 日志降级为 DEBUG，除非钱不够，避免刷屏
            if available_usd < Config.PAR_MIN_BALANCE or available_usd < required_usd:
                logger.warning(f"⛔ Paradex Low Funds: Avail=${available_usd:.2f} < Req=${required_usd:.2f}")
                return False
                
            if latency > 2000:
                logger.warning(f"Paradex High Latency: {latency:.0f}ms")
                
            return True
        except Exception as e:
            logger.error(f"Paradex Health Check Error: {e}")
            return False

    async def cancel_all_open_orders(self, short_symbol):
        symbol = Config.SYMBOL_MAP.get(short_symbol)
        if not symbol: return
        try:
            # 使用 fetch_open_orders 确保只在有单子的时候才调用取消，减少 API 消耗
            orders = await self.exchange.fetch_open_orders(symbol)
            if orders:
                # logger.info(f"🧹 Cancelling {len(orders)} orders for {short_symbol}")
                await self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            # logger.warning(f"Cancel error (can be ignored): {e}")
            pass

    async def get_best_bid_ask(self, short_symbol):
        symbol = Config.SYMBOL_MAP.get(short_symbol)
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=5)
            bid = ob['bids'][0][0] if ob['bids'] else None
            ask = ob['asks'][0][0] if ob['asks'] else None
            return bid, ask
        except: 
            return None, None

    async def get_funding_rate(self, short_symbol):
        full_symbol = Config.SYMBOL_MAP.get(short_symbol)
        if not full_symbol: 
            return None
        try:
            ticker = await self.exchange.fetch_ticker(full_symbol)
            if 'info' in ticker and 'funding_rate' in ticker['info']:
                return float(ticker['info']['funding_rate']) / 8.0 
            return None
        except: 
            return None

    async def get_position(self, short_symbol):
        full_symbol = Config.SYMBOL_MAP.get(short_symbol)
        if not full_symbol: 
            return {"symbol": short_symbol, "size": 0.0, "side": "flat"}
        try:
            positions = await self.exchange.fetch_positions([full_symbol])
            for p in positions:
                raw_size = float(p.get('contracts', 0))
                if raw_size == 0: continue
                direction = p.get('side', 'long')
                final_size = raw_size if direction == 'long' else -raw_size
                return {"symbol": short_symbol, "size": final_size,
                        "side": "long" if final_size > 0 else "short"}
        except Exception as e:
            logger.error(f"Paradex Pos Error: {e}")
        return {"symbol": short_symbol, "size": 0.0, "side": "flat"}

    async def execute_smart_maker(self, short_symbol, side, qty, timeout=60, is_close=False, aggressive=False):
        symbol = Config.SYMBOL_MAP.get(short_symbol)
        if not symbol: return False
        
        log_prefix = f"[Paradex][{'Close' if is_close else 'Open'}]"
        start_time = time.time()
        side = side.lower()
        target_qty = qty
        
        MAKER_TRY_WINDOW = 10 
        
        try:
            while True:
                # --- 1. 检查仓位之前，先取消所有挂单 ---
                # 这一步至关重要：确保 get_position 拿到的数据是干净的，且没有幽灵挂单在后面排队
                await self.cancel_all_open_orders(short_symbol)
                await asyncio.sleep(0.5) # 给交易所系统一点同步时间

                # 2. 检查真实仓位
                pos = await self.get_position(short_symbol)
                current_size = abs(pos['size'])
                
                if is_close:
                    qty_to_trade = current_size
                    if qty_to_trade <= 0:
                        logger.info(f"{log_prefix} {short_symbol} Cleared.")
                        return True
                    side = "sell" if pos['size'] > 0 else "buy"
                else:
                    if current_size >= target_qty * 0.98:
                        logger.info(f"{log_prefix} Filled.")
                        return True
                    if time.time() - start_time > timeout:
                        logger.error(f"{log_prefix} Timeout! {current_size}/{target_qty}")
                        return False
                    qty_to_trade = target_qty - current_size

                amt = float(self.exchange.amount_to_precision(symbol, qty_to_trade))
                if amt <= 0: return True

                elapsed = time.time() - start_time
                
                # 3. 策略判定
                use_market = False
                if aggressive or is_close:
                    use_market = (elapsed >= MAKER_TRY_WINDOW)
                else:
                    use_market = (elapsed > 30)

                try:
                    if use_market:
                        logger.info(f"{log_prefix} ☢️ FORCE MARKET (Taker): {side} {amt}")
                        await self.exchange.create_order(symbol, 'market', side, amt)
                        # 市价单通常立即成交，直接进入下一轮检查
                    else:
                        ob = await self.exchange.fetch_order_book(symbol, limit=1)
                        if not ob['bids'] or not ob['asks']:
                            await asyncio.sleep(1)
                            continue
                            
                        # 买单挂买一，卖单挂卖一
                        price = ob['bids'][0][0] if side == 'buy' else ob['asks'][0][0]
                        logger.info(f"{log_prefix} 🛡️ Maker: {side} {amt} @ {price} (Time: {int(elapsed)}s)")
                        
                        await self.exchange.create_order(symbol, 'limit', side, amt, price, {'postOnly': True})
                        # 挂单后，等待一段时间观察是否成交
                        await asyncio.sleep(3) 
                        
                except Exception as e:
                    err_str = str(e).lower()
                    if "insufficient funds" in err_str:
                        logger.critical("🚨 Paradex Funds Error!")
                        return False
                    
                    if "postonly" in err_str or "maker" in err_str:
                        # PostOnly 失败说明价格动了，无需等待直接重试
                        pass 
                    else:
                        logger.warning(f"Paradex order error: {e}")
                        await asyncio.sleep(1)
                    continue
                
        except Exception as e:
            logger.error(f"Paradex Exec Critical Error: {e}")
            return False
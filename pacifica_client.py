import json
import time
import uuid
import base58
import aiohttp
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from solders.keypair import Keypair
from config import Config

logger = logging.getLogger("Pacifica")

def round_to_tick(value, tick_size):
    d_val = Decimal(str(value))
    d_tick = Decimal(str(tick_size))
    try:
        rounded = (d_val / d_tick).to_integral_value(rounding=ROUND_DOWN) * d_tick
    except: return float(value)
    if tick_size >= 1: return int(rounded)
    return float(rounded)

def prepare_and_sign(header, payload, keypair_bytes):
    def sort_json_keys(value):
        if isinstance(value, dict): return {k: sort_json_keys(value[k]) for k in sorted(value.keys())}
        elif isinstance(value, list): return [sort_json_keys(item) for item in value]
        else: return value
    data = {**header, "data": payload}
    sorted_data = sort_json_keys(data)
    msg_str = json.dumps(sorted_data, separators=(",", ":"))
    kp = Keypair.from_bytes(keypair_bytes)
    sig = kp.sign_message(msg_str.encode("utf-8"))
    return base58.b58encode(bytes(sig)).decode("utf-8")

class PacificaClient:
    def __init__(self):
        self.base_url = Config.PAC_URL
        self.main_pubkey = Config.PAC_PUBKEY
        self.proxy = getattr(Config, 'PAC_PROXY', None)
        if self.proxy == "": self.proxy = None
        
        self.secret_bytes = base58.b58decode(Config.PAC_SECRET)
        self.agent_keypair = Keypair.from_bytes(self.secret_bytes)
        self.signer_pubkey = str(self.agent_keypair.pubkey())
        self.session = None
        self.symbol_rules = {}
        self._info_cache = None
        self._info_cache_ts = 0
        self._consecutive_errors = 0
        self._init_wallet()

    def _init_wallet(self):
        logger.info(f"Pacifica Wallet: {self.signer_pubkey[:6]}...")

    async def init_session(self):
        if not self.session:
            # 缩短连接超时，方便快速检测网络问题
            timeout = aiohttp.ClientTimeout(total=10, connect=4)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close_session(self):
        if self.session: 
            await self.session.close()
            self.session = None

    async def _handle_network_error(self, e, context):
        self._consecutive_errors += 1
        logger.warning(f"⚠️ Pacifica Net Error in {context}: {e}")
        if self._consecutive_errors >= 3:
            logger.warning("🔄 Too many errors, resetting Pacifica session...")
            await self.close_session()
            self._consecutive_errors = 0
            await asyncio.sleep(1)

    async def check_auth_health(self):
        """
        [New] 降落检查专用：强制发起一次带签名的请求。
        确保不仅仅是网络通，而且认证服务和订单服务也正常。
        """
        await self.init_session()
        try:
            # 使用 get_positions 必须带签名，适合做握手测试
            # 这里的逻辑不需要解析数据，只要状态码是 200 即可
            async with self.session.get(f"{self.base_url}/positions", 
                                        params={"account": self.main_pubkey}, 
                                        proxy=self.proxy,
                                        timeout=5) as r: # 强制5秒超时
                if r.status == 200:
                    self._consecutive_errors = 0
                    return True
                else:
                    logger.warning(f"Pacifica Auth Check Failed: HTTP {r.status}")
                    return False
        except Exception as e:
            logger.warning(f"Pacifica Auth Check Exception: {e}")
            return False

    async def _get_cached_info(self):
        await self.init_session()
        now = time.time()
        if self._info_cache is None or (now - self._info_cache_ts > 5):
            try:
                async with self.session.get(f"{self.base_url}/info", proxy=self.proxy) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._info_cache = data.get('data', [])
                        self._info_cache_ts = now
                        self._consecutive_errors = 0
            except Exception as e:
                pass
        return self._info_cache or []

    async def fetch_exchange_info(self):
        data = await self._get_cached_info()
        for m in data:
            if m.get('symbol') in Config.TARGET_COINS:
                self.symbol_rules[m['symbol']] = {
                    "price_tick": float(m.get('tick_size', 0.01)),
                    "min_size": float(m.get('lot_size', 0.001))
                }

    async def get_funding_rate(self, symbol):
        data = await self._get_cached_info()
        for item in data:
            if item.get('symbol') == symbol:
                return float(item.get('funding_rate', 0))
        return None

    async def get_best_bid_ask(self, symbol):
        await self.init_session()
        try:
            async with self.session.get(f"{self.base_url}/book", params={"symbol": symbol}, proxy=self.proxy) as r:
                if r.status == 200: 
                    res = await r.json()
                    self._consecutive_errors = 0
                    data = res.get('data', {})
                    if 'l' in data and len(data['l']) >= 2:
                        bids, asks = data['l'][0], data['l'][1]
                        return (float(bids[0]['p']), float(asks[0]['p'])) if bids and asks else (None, None)
                else:
                    logger.warning(f"Pacifica Book Status: {r.status}")
        except Exception as e:
            await self._handle_network_error(e, "get_best_bid_ask")
        return None, None

    async def get_position(self, symbol):
        await self.init_session()
        try:
             async with self.session.get(f"{self.base_url}/positions", params={"account": self.main_pubkey}) as r:
                 if r.status == 200:
                     self._consecutive_errors = 0
                     d = await r.json()
                     for p in d.get('data', []):
                         if p['symbol'] == symbol:
                             raw = float(p.get('amount', 0))
                             if raw == 0: continue
                             if p.get('side') == 'ask': raw = -raw
                             return {"symbol": symbol, "size": raw, "side": "long" if raw > 0 else "short"}
                     return {"symbol": symbol, "size": 0.0, "side": "flat"}
        except Exception as e: 
            await self._handle_network_error(e, "get_position")
        return {"symbol": symbol, "size": 0.0, "side": "flat"}

    async def create_order(self, symbol, side, amount, price, reduce_only=False, time_in_force="ALO"):
        await self.init_session()
        ts = int(time.time() * 1000)
        osic = "bid" if side.upper() == "BUY" else "ask"
        h = {"timestamp": ts, "expiry_window": 5000, "type": "create_order"}
        p = {
            "symbol": symbol, "price": str(price), "amount": str(amount),
            "side": osic, "reduce_only": reduce_only, "tif": time_in_force, 
            "client_order_id": str(uuid.uuid4())
        }
        try:
            sig = await asyncio.to_thread(prepare_and_sign, h, p, self.secret_bytes)
            req = {"account": self.main_pubkey, "signature": sig, **h, **p}
            if self.signer_pubkey != self.main_pubkey: req["agent_wallet"] = self.signer_pubkey
            
            async with self.session.post(f"{self.base_url}/orders/create", json=req, proxy=self.proxy) as resp:
                t = await resp.text()
                if resp.status != 200:
                    if "PostOnly" in t: return "POST_ONLY_FAIL"
                    logger.error(f"Pac Order Fail: {t}")
                    return None
                r = await resp.json()
                self._consecutive_errors = 0
                return r['data']['order_id'] if r.get('success') else None
        except Exception as e: 
            await self._handle_network_error(e, "create_order")
        return None

    async def cancel_order(self, symbol, order_id):
        await self.init_session()
        ts = int(time.time() * 1000)
        h = {"timestamp": ts, "expiry_window": 5000, "type": "cancel_order"}
        p = {"symbol": symbol, "order_id": order_id}
        try:
            sig = await asyncio.to_thread(prepare_and_sign, h, p, self.secret_bytes)
            req = {"account": self.main_pubkey, "signature": sig, **h, **p}
            if self.signer_pubkey != self.main_pubkey: req["agent_wallet"] = self.signer_pubkey
            async with self.session.post(f"{self.base_url}/orders/cancel", json=req) as r: return r.status == 200
        except: return False

    async def panic_close(self, symbol):
        logger.warning(f"[Pacifica] ☢️ PANIC CLOSE triggered for {symbol}")
        return await self.execute_smart_maker(symbol, "SELL", 0, timeout=30, is_close=True)

    async def execute_smart_maker(self, symbol, side, qty, timeout=60, is_close=False):
        """
        修复版：严防 API 虚假仓位数据
        逻辑：查仓位(无挂单状态) -> 挂单 -> 等待(盲等) -> 撤单 -> 查仓位
        """
        start_time = time.time()
        rule = self.symbol_rules.get(symbol, {"price_tick": 0.01, "min_size": 0.001})
        tick = rule['price_tick']
        target_qty = qty
        log_prefix = f"[Pacifica][{'Close' if is_close else 'Open'}]"
        
        fail_count = 0

        while True:
            # 1. 检查真实仓位 (此时没有挂单，数据是可信的)
            pos = await self.get_position(symbol)
            cur_size = abs(pos['size'])
            
            if is_close:
                qty_to_trade = cur_size
                if qty_to_trade < rule['min_size']: 
                    logger.info(f"{log_prefix} Position cleared (Size: {qty_to_trade}).")
                    return True
                real_side = "SELL" if pos['size'] > 0 else "BUY"
                if side != real_side: side = real_side
            else:
                if cur_size >= target_qty * 0.98: 
                    return True
                if time.time() - start_time > timeout: 
                    return False
                qty_to_trade = target_qty - cur_size

            clean_qty = round_to_tick(qty_to_trade, rule['min_size'])
            if clean_qty <= 0: return True
            
            # 2. 获取价格
            bid, ask = await self.get_best_bid_ask(symbol)
            if not bid: 
                fail_count += 1
                if fail_count % 5 == 0:
                    logger.warning(f"{log_prefix} Waiting for price... (Attempt {fail_count})")
                await asyncio.sleep(1)
                continue
            
            fail_count = 0
            elapsed = time.time() - start_time
            # 平仓时，如果已经尝试了10秒还没成，就激进
            aggressive = is_close and (elapsed > 10) 
            
            if is_close and int(elapsed) % 10 == 0:
                logger.info(f"{log_prefix} Closing... Remaining: {clean_qty} | Aggressive: {aggressive}")

            # 3. 挂单 (Action)
            oid = None
            if aggressive:
                # 激进模式：买单挂高5%，卖单挂低5%，确保 IOC 能吃进去
                price = ask * 1.05 if side == "BUY" else bid * 0.95
                price = round_to_tick(price, tick)
                # 使用 IOC (Immediate or Cancel)，能吃多少吃多少
                oid = await self.create_order(symbol, side, clean_qty, price, reduce_only=is_close, time_in_force="IOC")
                if oid: logger.info(f"{log_prefix} ☢️ IOC: {side} {clean_qty} @ {price}")
            else:
                price = min(bid + tick, ask - tick) if side == "BUY" else max(ask - tick, bid + tick)
                price = round_to_tick(price, tick)
                oid = await self.create_order(symbol, side, clean_qty, price, reduce_only=is_close, time_in_force="ALO")
                if oid: logger.info(f"{log_prefix} 🛡️ Maker: {side} {clean_qty} @ {price}")
            
            if oid == "POST_ONLY_FAIL":
                await asyncio.sleep(0.5); continue
            if not oid: 
                await asyncio.sleep(1); continue
                
            # 4. 盲等待 (Blind Wait)
            wait_time = 5 if not aggressive else 1
            await asyncio.sleep(wait_time)
            
            # 5. 强制撤单 (Cancel)
            await self.cancel_order(symbol, oid)
            
            # 6. 冷却并回环 (Cooldown)
            await asyncio.sleep(1.0)
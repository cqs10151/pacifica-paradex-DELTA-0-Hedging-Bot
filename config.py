import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Pacifica
    PAC_PUBKEY = os.getenv("PACIFICA_MAIN_PUBKEY")
    PAC_SECRET = os.getenv("PACIFICA_AGENT_KEY")
    PAC_URL = os.getenv("PACIFICA_API_URL")

    # Paradex
    PAR_ADDR = os.getenv("PARADEX_ACCOUNT_ADDRESS")
    PAR_KEY = os.getenv("PARADEX_PRIVATE_KEY")

    # Strategy - Position
    MIN_USD = float(os.getenv("MIN_POSITION_USD", 100))
    MAX_USD = float(os.getenv("MAX_POSITION_USD", 300))
    HOLD_RANGE = (int(os.getenv("HOLD_MIN", 28850)), int(os.getenv("HOLD_MAX", 868500)))
    SYNC_TIMEOUT = int(os.getenv("SYNC_TIMEOUT", 180))

    # Strategy - Funding Arbitrage
    MIN_OPEN_APY = float(os.getenv("MIN_OPEN_APY", 0.05))
    MIN_CLOSE_APY = float(os.getenv("MIN_CLOSE_APY", 0.00))
    FUNDING_CHECK_INTERVAL = int(os.getenv("FUNDING_CHECK_INTERVAL", 600))

    # --- NEW: Risk Management & Optimization ---
    MAX_OPEN_SPREAD = float(os.getenv("MAX_OPEN_SPREAD", 0.006))
    PAR_MIN_BALANCE = float(os.getenv("PAR_MIN_BALANCE", 50.0))
    EMERGENCY_SLIPPAGE = 0.02 

    # --- NEW: Alerting & Safety ---
    # 如果平仓耗时超过此时间（秒），发送报警
    CLOSE_TIMEOUT_ALERT = 600 
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "---")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "---")

    # 币种映射（自己配置）
    SYMBOL_MAP = {
        "BNB": "BNB/USD:USDC",
        "SOL": "SOL/USD:USDC",
        "XRP": "XRP/USD:USDC",
        "SUI": "SUI/USD:USDC",
        "XPL": "XPL/USD:USDC"
    }
    
    REVERSE_MAP = {v: k for k, v in SYMBOL_MAP.items()}
    TARGET_COINS = list(SYMBOL_MAP.keys()) 
    PAC_PROXY = "http://127.0.0.1:7890"

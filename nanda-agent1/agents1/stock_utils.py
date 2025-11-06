# nanda-agent/agents/stock_utils.py
import re
import yfinance as yf

TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")

def extract_stock_symbols(text: str):
    """从文本中提取疑似股票代码"""
    candidates = set(TICKER_RE.findall(text.upper()))
    blacklist = {"HTTP", "HTTPS", "USA", "AND", "THE"}
    return [c for c in candidates if c not in blacklist]

def get_stock_price(symbol: str) -> str:
    """获取股票价格"""
    try:
        t = yf.Ticker(symbol)
        price = getattr(t, "fast_info", {}).get("last_price")
        if price is None:
            hist = t.history(period="1d")
            price = float(hist["Close"].iloc[-1]) if len(hist) else None
        return f"{symbol} 当前价格: ${price:.2f}" if price is not None else f"{symbol} 当前价格: N/A"
    except Exception as e:
        return f"{symbol} 查询失败: {e}"

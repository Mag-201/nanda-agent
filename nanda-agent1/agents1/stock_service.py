# stock_service.py
import datetime as _dt
from typing import Dict, List, Tuple
import yfinance as yf
import os, socket, requests
import requests.packages.urllib3.util.connection as urllib3_cn
import csv, io  # for Stooq fallback

# ---------------- Language toggle ----------------
LANG = (os.getenv("STOCK_LANG") or "en").lower()
def _is_en(): return LANG.startswith("en")

# ---------------- Network hardening ----------------
def _force_ipv4():
    # Force IPv4 to avoid flaky IPv6 networks
    def family():
        return socket.AF_INET
    urllib3_cn.allowed_gai_family = family

def _make_session():
    s = requests.Session()
    s.trust_env = False  # ignore system HTTP(S)_PROXY to avoid bad proxies
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/127.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    # Optional proxy from env (if you really need it)
    http  = os.getenv("HTTP_PROXY")  or os.getenv("http_proxy")
    https = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if http or https:
        s.proxies.update({
            "http":  http or https,
            "https": https or http,
        })
    return s

_FORCE_IPV4 = True
if _FORCE_IPV4:
    _force_ipv4()
_YF_SESSION = _make_session()

# ---------------- Helpers ----------------
def _norm_ticker(t: str) -> str:
    return (t or "").strip().upper()

def _fmt_usd(n: float) -> str:
    try:
        if n is None: return "—"
        if abs(n) >= 1e12: return f"${n/1e12:.2f}T"
        if abs(n) >= 1e9:  return f"${n/1e9:.2f}B"
        if abs(n) >= 1e6:  return f"${n/1e6:.2f}M"
        return f"${n:,.2f}"
    except Exception:
        return "—"

def _pct(v: float) -> str:
    try:
        return f"{v:+.2f}%"
    except Exception:
        return "—"

def _mk_box_table(headers: List[str], rows: List[List[str]]) -> str:
    """
    Pretty fixed-width table using box-drawing characters. Works great in <pre>.
    """
    # compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def hr(left: str, mid: str, right: str, fill: str = "─") -> str:
        parts = []
        for w in widths:
            parts.append(fill * (w + 2))
        return left + (mid.join(parts)) + right

    top    = hr("┌", "┬", "┐")
    sep    = hr("├", "┼", "┤")
    bottom = hr("└", "┴", "┘")

    def fmt_row(cols: List[str]) -> str:
        out = "│"
        for i, c in enumerate(cols):
            w = widths[i]
            out += f" {str(c):{w}} │"
        return out

    lines = [top, fmt_row(headers), sep]
    for r in rows:
        lines.append(fmt_row(r))
    lines.append(bottom)
    return "\n".join(lines)

# ---------------- Stooq fallback ----------------
def _stooq_symbol(t: str) -> str:
    # US stocks on stooq use <lower>.us
    return f"{t.lower()}.us"

def _stooq_last_close(t: str) -> Tuple[float, float]:
    """Return (last, prev) daily close from Stooq or (None, None)."""
    sym = _stooq_symbol(t)
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = _YF_SESSION.get(url, timeout=8)
        ctype = r.headers.get("Content-Type", "").lower()
        if not r.ok or ("text" not in ctype and "csv" not in ctype):
            return None, None
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if len(rows) == 0:
            return None, None
        last = float(rows[-1]["Close"]) if rows[-1]["Close"] else None
        prev = float(rows[-2]["Close"]) if len(rows) >= 2 and rows[-2]["Close"] else last
        return last, prev
    except Exception:
        return None, None

# ---------------- Core: /quote ----------------
def quote(ticker: str) -> str:
    t = _norm_ticker(ticker)
    if not t:
        return ("⚠️ Usage: /quote <TICKER> e.g., /quote AAPL"
                if _is_en()
                else "⚠️ 用法：/quote <TICKER> 例如：/quote AAPL")

    price = prev = None
    mcap = fifty_two_week_low = fifty_two_week_high = None

    # 1) yfinance first
    try:
        tk = yf.Ticker(t, session=_YF_SESSION)

        fi = {}
        try:
            fi = tk.fast_info or {}
        except Exception:
            fi = {}

        price = fi.get("last_price")
        prev  = fi.get("previous_close")
        mcap  = fi.get("market_cap")
        fifty_two_week_low  = fi.get("year_low")
        fifty_two_week_high = fi.get("year_high")

        if price is None or prev is None:
            hist = tk.history(period="5d", interval="1d")
            if len(hist) >= 2:
                price = float(hist["Close"].iloc[-1])
                prev  = float(hist["Close"].iloc[-2])
            elif len(hist) == 1:
                price = float(hist["Close"].iloc[-1]); prev = price
    except Exception:
        pass

    # 2) Stooq fallback for price
    if price is None or prev is None:
        s_last, s_prev = _stooq_last_close(t)
        price = price or s_last
        prev  = prev  or s_prev

    if price is None:
        return (f"⚠️ Unable to fetch price for {t} (Yahoo unreachable and Stooq fallback failed)."
                if _is_en()
                else f"⚠️ 无法获取 {t} 的价格数据（Yahoo 不可达且 Stooq 兜底失败）。")

    chg = price - (prev or 0)
    pct = (chg / prev * 100) if prev else 0.0
    arrow = '▲' if chg >= 0 else '▼'
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if _is_en():
        title = f"{t} — Snapshot (local time: {now})"
        table = _mk_box_table(
            ["Metric", "Value"],
            [
                ["Last Price", f"${price:,.2f}"],
                ["Change",     f"{arrow} {chg:+.2f} ({pct:+.2f}%)"],
                ["Market Cap", _fmt_usd(mcap)],
                ["52W Range",  f"{fifty_two_week_low} ~ {fifty_two_week_high}"],
            ]
        )
        note = "Source: yfinance (primary); Stooq (fallback for price only)."
        return f"📈 {title}\n{table}\n{note}"
    else:
        # Chinese fallback (kept for completeness)
        lines = [
            f"📈 {t}",
            f"价格：${price:,.2f}  {arrow} {chg:+.2f} ({pct:+.2f}%)",
            f"市值：{_fmt_usd(mcap)}（yfinance 不通时可能为空）",
            f"52周：{fifty_two_week_low} ~ {fifty_two_week_high}",
            f"时间：{now}（本地）",
            "数据源：yfinance 优先，失败则 Stooq 兜底",
        ]
        return "\n".join(lines)

# ---------------- Core: /compare ----------------
def compare(t1: str, t2: str) -> str:
    t1 = _norm_ticker(t1); t2 = _norm_ticker(t2)
    if not t1 or not t2:
        return ("⚠️ Usage: /compare <T1> <T2> e.g., /compare NVDA AMD"
                if _is_en()
                else "⚠️ 用法：/compare <T1> <T2> 例如：/compare NVDA AMD")

    def _get_one(t: str) -> Dict:
        price = prev = mcap = ytd = None
        try:
            tk = yf.Ticker(t, session=_YF_SESSION)

            fi = {}
            try:
                fi = tk.fast_info or {}
            except Exception:
                fi = {}

            price = fi.get("last_price")
            prev  = fi.get("previous_close")
            mcap  = fi.get("market_cap")

            if price is None or prev is None:
                hist = tk.history(period="5d", interval="1d")
                if len(hist) >= 2:
                    price = float(hist["Close"].iloc[-1]); prev = float(hist["Close"].iloc[-2])
                elif len(hist) == 1:
                    price = float(hist["Close"].iloc[-1]); prev = price

            # YTD return (best effort)
            try:
                y0 = _dt.datetime(_dt.datetime.now().year, 1, 1)
                hist_ytd = tk.history(start=y0)
                if len(hist_ytd) >= 2:
                    start = float(hist_ytd["Close"].iloc[0])
                    last  = float(hist_ytd["Close"].iloc[-1])
                    ytd = (last / start - 1) * 100
            except Exception:
                pass
        except Exception:
            pass

        if price is None or prev is None:
            s_last, s_prev = _stooq_last_close(t)
            price = price or s_last
            prev  = prev  or s_prev

        return {"t": t, "price": price, "prev": prev, "mcap": mcap, "ytd": ytd}

    a = _get_one(t1); b = _get_one(t2)
    if a["price"] is None or b["price"] is None:
        return (f"⚠️ Unable to fetch quotes for {t1} or {t2} (network restricted?)."
                if _is_en()
                else f"⚠️ 无法获取 {t1} 或 {t2} 的行情数据（网络受限？）")

    def _row(x: Dict) -> List[str]:
        chg = (x["price"] - (x["prev"] or 0)) if x["prev"] else 0.0
        pct = (chg / x["prev"] * 100) if x["prev"] else 0.0
        arrow = "▲" if chg >= 0 else "▼"
        ytd = _pct(x["ytd"]) if x["ytd"] is not None else "—"
        return [
            f"{x['t']}",
            f"${x['price']:,.2f}",
            f"{arrow} {chg:+.2f}",
            f"{pct:+.2f}%",
            f"{ytd}",
            f"{_fmt_usd(x['mcap'])}",
        ]

    headers_en = ["Ticker", "Price", "Δ", "Δ%", "YTD", "Market Cap"]
    headers_zh = ["代码", "现价", "涨跌", "涨跌幅", "年初至今", "市值"]
    headers = headers_en if _is_en() else headers_zh

    table = _mk_box_table(headers, [_row(a), _row(b)])
    note = ("Source: yfinance (primary); Stooq (fallback price only)."
            if _is_en() else "数据源：yfinance 优先；Stooq 仅兜底价格。")
    return f"{table}\n{note}"

# ---------------- Help text ----------------
def help_text() -> str:
    if _is_en():
        return (
            "🧭 Stock Commands:\n"
            "  /quote <TICKER>        → Single quote (e.g., /quote AAPL)\n"
            "  /compare <T1> <T2>     → Compare two tickers (e.g., /compare NVDA AMD)\n"
            "  @stock price <TICKER>  → Same as /quote\n"
            "  @stock help            → Show this help\n"
            "\nTip: These commands are handled locally; other messages are forwarded to your Agent."
        )
    return (
        "🧭 Stock 命令帮助：\n"
        "  /quote <TICKER>           → 单只股票报价（例：/quote AAPL）\n"
        "  /compare <T1> <T2>        → 两只股票对比（例：/compare NVDA AMD）\n"
        "  @stock price <TICKER>     → 同 /quote\n"
        "  @stock help               → 显示本帮助\n"
        "\n提示：以上命令由本地处理；其它消息会转发给你的 Agent。"
    )

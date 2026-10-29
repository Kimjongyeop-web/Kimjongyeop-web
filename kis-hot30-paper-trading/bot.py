"""KIS real-time quotes + simulated stock trades. No broker order endpoints.

For public GitHub use: credentials come ONLY from .env / OS environment.
Trading rules are experimental and NOT verified rules from any individual trader.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import zipfile
import json
import math
import os
import sqlite3
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"  # Always ignored by Git.
KST = ZoneInfo("Asia/Seoul")

# Only market-data and authentication endpoints appear in this project.
API_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
API_MINUTE = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
TR_PRICE = "FHKST01010100"
TR_MINUTE = "FHKST03010200"   # Same TR in KIS's current real/demo examples.
TR_TICK = "H0STCNT0"


VERBOSE = "--verbose" in sys.argv


def detail_print(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)


_warning_kinds = set()
_original_showwarning = warnings.showwarning


def compact_warning(message, category, filename, lineno, file=None, line=None):
    if VERBOSE:
        return _original_showwarning(message, category, filename, lineno, file, line)
    if category not in _warning_kinds:
        _warning_kinds.add(category)
        print(f"[주의] 라이브러리 경고: {category.__name__} (실행 지속, 상세 모드에서 원인 확인)")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def safe_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def safe_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


@dataclass(frozen=True)
class Config:
    app_key: str
    app_secret: str
    discord_webhook: str
    kis_env: str = "real"  # real: LIVE market-data feed; demo: virtual feed
    universe_from: int = 50
    universe_to: int = 150
    hot_count: int = 30  # INCLUDING already-open virtual positions
    daily_average_days: int = 20
    hot_refresh_seconds: int = 600
    quote_pause_seconds: float = 0.35  # slow REST scan to reduce throttling
    initial_cash: int = 10_000_000
    trade_budget: int = 600_000
    max_open_positions: int = 6
    max_buys_per_stock_per_day: int = 3
    stop_reentry_minutes: int = 20
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.035
    min_volume_ma20: float = 800
    breakout_minutes: int = 10  # Moderate entry profile: previous ten completed 1m bars
    min_bars_required: int = 30
    min_daily_avg_volume: float = 50_000

    @property
    def base_url(self) -> str:
        return (
            "https://openapi.koreainvestment.com:9443"
            if self.kis_env == "real"
            else "https://openapivts.koreainvestment.com:29443"
        )

    @property
    def ws_url(self) -> str:
        return (
            "ws://ops.koreainvestment.com:21000"
            if self.kis_env == "real"
            else "ws://ops.koreainvestment.com:31000"
        )


def load_config() -> Config | None:
    load_dotenv(ROOT / ".env")
    required = ("KIS_APP_KEY", "KIS_APP_SECRET", "DISCORD_WEBHOOK_URL")
    missing = [
        key for key in required
        if not os.getenv(key, "").strip()
        or os.getenv(key, "").strip().lower().startswith(("your_", "fill_", "여기에"))
    ]
    if missing:
        print("\n[설정 필요] API/디스코드 인증정보가 설정되지 않았습니다.")
        print("  1) README 안내에 따라 로컬 .env 파일을 만드세요.")
        for key in missing:
            print(f"     - {key}")
        print("  3) .env 파일은 절대로 GitHub에 올리지 마세요.\n")
        return None
    env = os.getenv("KIS_ENV", "real").lower().strip()
    if env not in ("real", "demo"):
        print("[설정 오류] KIS_ENV는 real 또는 demo여야 합니다.")
        return None
    hook = os.environ["DISCORD_WEBHOOK_URL"].strip()
    if not hook.startswith("https://discord.com/api/webhooks/"):
        print("[설정 오류] DISCORD_WEBHOOK_URL은 Discord HTTPS 웹훅 주소여야 합니다.")
        return None
    return Config(
        app_key=os.environ["KIS_APP_KEY"].strip(),
        app_secret=os.environ["KIS_APP_SECRET"].strip(),
        discord_webhook=hook,
        kis_env=env,
    )


class KISAPIError(Exception):
    pass


class KISClient:
    """Read-only KIS REST. Credentials and tokens are never printed."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self._token = ""
        self._token_expiry = dt.datetime.min.replace(tzinfo=KST)
        self._cache_path = LOCAL / "token_cache.json"
        LOCAL.mkdir(exist_ok=True)
        self._read_token_cache()

    def _read_token_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            obj = json.loads(self._cache_path.read_text(encoding="utf-8"))
            fingerprint = hashlib.sha256(self.cfg.app_key.encode()).hexdigest()
            expires = dt.datetime.fromisoformat(obj["expires"])
            if (obj.get("fingerprint") == fingerprint
                    and obj.get("environment") == self.cfg.kis_env
                    and expires > now_kst() + dt.timedelta(minutes=5)):
                self._token = obj["access_token"]
                self._token_expiry = expires
                print("[KIS] 로컬 토큰 캐시 재사용 (토큰값 비공개)")
        except (ValueError, KeyError, TypeError, OSError):
            pass

    def _cache_token(self) -> None:
        obj = {
            "access_token": self._token,
            "expires": self._token_expiry.isoformat(),
            "fingerprint": hashlib.sha256(self.cfg.app_key.encode()).hexdigest(),
            "environment": self.cfg.kis_env,
        }
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._cache_path)

    def access_token(self, force: bool = False) -> str:
        if not force and self._token and now_kst() < self._token_expiry - dt.timedelta(minutes=5):
            return self._token
        resp = self.session.post(
            self.cfg.base_url + "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.cfg.app_key,
                "appsecret": self.cfg.app_secret,
            },
            timeout=12,
        )
        if resp.status_code != 200:
            raise KISAPIError(
                f"접근토큰 발급 실패 (HTTP {resp.status_code}). "
                "KIS 실전/모의 키, 발급 제한, API 권한을 확인하세요."
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise KISAPIError(f"접근토큰 발급 실패: 코드 {body.get('error_code', '미상')}")
        self._token = token
        # KIS may return an explicit expiration datetime; otherwise use a conservative limit.
        raw_expires = body.get("access_token_token_expired", "")
        try:
            expiry = dt.datetime.strptime(raw_expires, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except (TypeError, ValueError):
            expiry = now_kst() + dt.timedelta(hours=12)
        self._token_expiry = expiry
        self._cache_token()
        print("[KIS] 접근토큰 발급 성공 (토큰값 비공개)")
        return token

    def approval_key(self) -> str:
        resp = self.session.post(
            self.cfg.base_url + "/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": self.cfg.app_key,
                "secretkey": self.cfg.app_secret,
            },
            timeout=12,
        )
        if resp.status_code != 200:
            raise KISAPIError(f"WebSocket 접속키 발급 실패 (HTTP {resp.status_code})")
        body = resp.json()
        if not body.get("approval_key"):
            raise KISAPIError("WebSocket 접속키 발급 실패 (응답에 approval_key 없음)")
        detail_print("[KIS] WebSocket 접속키 발급 성공 (접속키 비공개)")
        return body["approval_key"]

    def _get(self, path: str, tr_id: str, params: dict[str, str],
             tr_cont: str = "") -> dict:
        for retry in range(2):
            token = self.access_token()
            resp = self.session.get(
                self.cfg.base_url + path,
                headers={
                    "authorization": f"Bearer {token}",
                    "appkey": self.cfg.app_key,
                    "appsecret": self.cfg.app_secret,
                    "tr_id": tr_id,
                    "tr_cont": tr_cont,
                    "custtype": "P",
                },
                params=params,
                timeout=8,
            )
            if resp.status_code == 401 and retry == 0:
                self.access_token(force=True)
                continue
            if resp.status_code != 200:
                raise KISAPIError(f"{tr_id} HTTP {resp.status_code}")
            data = resp.json()
            if data.get("rt_cd") != "0":
                # Never log the complete response: server might reflect sensitive info.
                raise KISAPIError(f"{tr_id}: {data.get('msg_cd', '')} {data.get('msg1', '')}")
            data["_tr_cont"] = resp.headers.get("tr_cont", "")
            return data
        raise KISAPIError(f"{tr_id} API 호출 실패")

    def quote(self, code: str) -> dict[str, float] | None:
        try:
            data = self._get(
                API_PRICE, TR_PRICE,
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )["output"]
            if safe_int(data.get("stck_prpr")) <= 0:
                return None
            return {
                "last": safe_float(data.get("stck_prpr")),
                "cum_volume": safe_float(data.get("acml_vol")),
                "turnover": safe_float(data.get("acml_tr_pbmn")),
                "change_pct": safe_float(data.get("prdy_ctrt")),
            }
        except (KISAPIError, KeyError, TypeError) as exc:
            print(f"[REST] {code} 현재가 조회 실패: {exc}")
            return None

    def minute_bars(self, code: str, count: int = 60) -> list[dict]:
        """KIS completed *today* 1m bars, paged backwards (max ~30 per call).

        Request time is an upper bound, not 09:00 start. Excludes unfinished minute.
        """
        now = now_kst()
        if now.weekday() > 4 or now.hour < 9:
            return []
        cutoff = now.replace(second=0, microsecond=0)
        cursor = cutoff - dt.timedelta(minutes=1)
        result: dict[str, dict] = {}
        max_pages = max(1, math.ceil(count / 30) + 1)
        for _ in range(max_pages):
            if cursor.date() != now.date() or cursor.time() < dt.time(9, 0):
                break
            try:
                data = self._get(
                    API_MINUTE, TR_MINUTE,
                    {
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": code,
                        "FID_INPUT_HOUR_1": cursor.strftime("%H%M%S"),
                        "FID_PW_DATA_INCU_YN": "Y",
                        "FID_ETC_CLS_CODE": "",  # official parameter; NOT FID_FIT_PRC_CLS_CODE
                    },
                )
            except KISAPIError as exc:
                print(f"[BACKFILL] {code} KIS 분봉조회 실패: {exc}")
                break
            rows = data.get("output2") or []
            if not isinstance(rows, list) or not rows:
                break
            earliest = None
            for item in rows:
                hhmmss = str(item.get("stck_cntg_hour", ""))
                try:
                    ts = dt.datetime.combine(
                        now.date(), dt.time(int(hhmmss[:2]), int(hhmmss[2:4])), KST
                    )
                except (ValueError, TypeError):
                    continue
                if not (dt.time(9, 0) <= ts.time() < dt.time(15, 30)) or ts >= cutoff:
                    continue
                op = safe_float(item.get("stck_oprc"))
                hi = safe_float(item.get("stck_hgpr"))
                lo = safe_float(item.get("stck_lwpr"))
                cl = safe_float(item.get("stck_prpr"))
                vol = safe_int(item.get("cntg_vol"))
                if min(op, hi, lo, cl) <= 0 or vol < 0:
                    continue
                result[ts.strftime("%H%M")] = {
                    "time": ts.strftime("%H%M"),
                    "date": ts.strftime("%Y%m%d"),
                    "open": op, "high": hi, "low": lo, "close": cl, "volume": vol,
                }
                if earliest is None or ts < earliest:
                    earliest = ts
            if earliest is None or len(result) >= count:
                break
            next_cursor = earliest - dt.timedelta(minutes=1)
            if next_cursor >= cursor:
                break
            cursor = next_cursor
            time.sleep(0.3)
        return [result[k] for k in sorted(result)][-count:]


# KIS official fixed-width masters: tail lengths exclude CR/LF.
# Source: open-trading-api/stocks_info/kis_{kospi,kosdaq}_code_mst.py
MASTER_LAYOUT = {'kospi': (227, 212, 9), 'kosdaq': (221, 206, 9)}
STOCK_GROUPS = {"ST", "RT", "PF", "IF", "MF", "DR", "FS"}


def parse_stock_master(payload: bytes, market: str) -> dict[str, tuple[int, str]]:
    """Read KIS master cap (KRW 100 million), excluding ETFs/ETNs/bonds/warrants."""
    tail_size, cap_offset, cap_width = MASTER_LAYOUT[market]
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            info = archive.getinfo(f"{market}_code.mst")
            if info.file_size > 20_000_000:
                raise ValueError("oversized master")
            content = archive.read(info).decode("cp949")
    except (zipfile.BadZipFile, KeyError, UnicodeError, ValueError) as exc:
        raise KISAPIError(f"한투 {market} 종목정보 파일 형식 오류") from exc
    stocks = {}
    for line in content.splitlines():
        if not line.strip():
            continue
        if len(line) < 21 + tail_size:
            raise KISAPIError(f"한투 {market} 종목정보 필드 길이 변경/손상")
        tail = line[-tail_size:]
        group = tail[:2]
        if group not in STOCK_GROUPS:
            continue
        code = line[:9].strip()
        if len(code) != 6 or not code.isdigit():
            continue
        name = line[21:-tail_size].strip()
        cap_text = tail[cap_offset:cap_offset + cap_width].strip()
        if not name or not cap_text.isdigit():
            raise KISAPIError(f"한투 {market} 시총 필드 오류: {code}")
        cap = int(cap_text)
        if code in stocks:
            raise KISAPIError(f"한투 {market} 종목정보 중복: {code}")
        if cap > 0:
            stocks[code] = (cap, name)
    if len(stocks) < 150:
        raise KISAPIError(f"한투 {market} 유효 주식 종목 부족: {len(stocks)}")
    return stocks


def download_stock_master(market: str) -> bytes:
    """Public KIS download only. No API credentials or token cache involved."""
    url = f"https://new.real.download.dws.co.kr/common/master/{market}_code.mst.zip"
    try:
        with requests.get(url, timeout=(10, 30), stream=True) as response:
            response.raise_for_status()
            chunks = []
            total = 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > 5_000_000:
                    raise KISAPIError(f"한투 {market} 다운로드 크기 초과")
                chunks.append(chunk)
            detail_print(f"[MASTER] {market} 공개 종목정보 수신, "
                  f"서버 수정시각 {response.headers.get('Last-Modified', '미제공')}")
            return b"".join(chunks)
    except requests.RequestException as exc:
        raise KISAPIError(f"한투 {market} 공개 종목정보 다운로드 실패: {type(exc).__name__}") from exc


def market_universe(cfg: Config, api: KISClient) -> tuple[list[str], dict[str, str]]:
    """Rank the full KIS stock masters, never the top-30 ranking endpoint."""
    if cfg.kis_env != "real":
        raise KISAPIError("이 봇은 KIS_ENV=real 시세 환경을 사용합니다.")
    merged = {}
    for market in ("kospi", "kosdaq"):
        stocks = parse_stock_master(download_stock_master(market), market)
        if set(merged).intersection(stocks):
            raise KISAPIError("한투 시장 간 종목코드 중복: 종목정보 확인 필요")
        merged.update(stocks)
        detail_print(f"[MASTER] {market}: 시총 유효 주식 {len(stocks)}개 (ETF/ETN/채권 등 제외)")
    ordered = sorted(merged, key=lambda code: (-merged[code][0], code))
    if not (1 <= cfg.universe_from <= cfg.universe_to <= len(ordered)):
        raise KISAPIError("한투 전체 종목정보로 요청한 시총 순위를 확보할 수 없습니다.")
    codes = ordered[cfg.universe_from - 1:cfg.universe_to]
    print(f"[UNIVERSE] 한투 종목정보 파일 기준 시총 {cfg.universe_from}~{cfg.universe_to}위: "
          f"{len(codes)}종목 (장중 실시간 순위 아님)")
    return codes, {code: merged[code][1] for code in codes}


def completed_daily_average(rows: list[dict], through: str, count: int) -> float | None:
    """Deduplicate completed daily dates; malformed volume is never treated as zero."""
    values = {}
    for row in rows:
        day = str(row.get("stck_bsop_date", ""))
        try:
            dt.datetime.strptime(day, "%Y%m%d")
            if len(day) != 8 or day > through:
                continue
            volume = float(str(row["acml_vol"]).replace(",", ""))
            if not math.isfinite(volume) or volume < 0:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        if day in values and values[day] != volume:
            return None
        values[day] = volume
    if len(values) < count:
        return None
    return sum(values[d] for d in sorted(values)[-count:]) / count


def daily_average_volumes(codes: list[str], cfg: Config, api: KISClient) -> dict[str, float]:
    """KIS completed daily volume; generated only in memory, with no disk cache."""
    today = now_kst().date()
    through = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
    start = (today - dt.timedelta(days=100)).strftime("%Y%m%d")
    volumes = {}
    for i, code in enumerate(codes, 1):
        try:
            data = api._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                 "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": through,
                 "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"},
            )
            avg = completed_daily_average(data.get("output2") or [], through, cfg.daily_average_days)
            if avg is not None:
                volumes[code] = avg
            else:
                print(f"[BASELINE] {code}: 확정 일봉 부족/오류, 후보 제외")
        except (KISAPIError, requests.RequestException, ValueError, TypeError) as exc:
            print(f"[BASELINE] {code} 한투 일봉 조회 실패: {type(exc).__name__}")
        if i % 20 == 0:
            print(f"[BASELINE] {i}/{len(codes)} 처리, 유효 {len(volumes)}")
        time.sleep(cfg.quote_pause_seconds)
    print(f"[BASELINE] 한투 {cfg.daily_average_days}일 평균 거래량 확보: {len(volumes)}/{len(codes)}종목")
    return volumes


def select_hot(
    quotes: dict[str, dict[str, float]],
    baseline: dict[str, float],
    cfg: Config,
) -> tuple[list[str], float]:
    """Rank morning/day-to-date volume vs previous 20 daily volumes.

    This is an experimental screening score, NOT a verified trader's formula.
    """
    ranked: list[tuple[str, float]] = []
    changes = []
    for code, quote in quotes.items():
        avg = baseline.get(code, 0)
        if avg < cfg.min_daily_avg_volume or quote["cum_volume"] <= 0:
            continue
        rvol = quote["cum_volume"] / avg
        # Relative volume dominates; small bonus for positive price change and liquidity.
        bonus = max(-5.0, min(5.0, quote["change_pct"])) / 100
        turnover_bonus = min(0.1, quote["turnover"] / 20_000_000_000) * 0.1
        score = rvol + bonus + turnover_bonus
        ranked.append((code, score))
        changes.append(quote["change_pct"])
    ranked.sort(key=lambda x: (-x[1], x[0]))
    # A breadth proxy from the UNIVERSE, not the KOSPI/KOSDAQ indices.
    breadth = (sum(x > 0 for x in changes) / len(changes)) if changes else 0.5
    return [c for c, _ in ranked[:cfg.hot_count]], breadth


def pin_positions(hot_rank: list[str], positions: dict, max_count: int) -> list[str]:
    pinned = list(dict.fromkeys(positions))
    if len(pinned) > max_count:
        raise RuntimeError("보유 종목 수가 HOT 구독 상한보다 큽니다. 수동 점검 필요")
    result = list(pinned)
    for code in hot_rank:
        if code not in result and len(result) < max_count:
            result.append(code)
    return result


class Store:
    """Atomic trades/state, persistent minute bars; exports human-readable CSV."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id=1), body TEXT NOT NULL)")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, code TEXT,
                name TEXT, side TEXT, price REAL, qty INTEGER, pnl REAL,
                pnl_pct REAL, reason TEXT, cash_after REAL, realized_pnl REAL
            )""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS bars (
                day TEXT, code TEXT, hhmm TEXT, open REAL, high REAL,
                low REAL, close REAL, volume INTEGER,
                PRIMARY KEY(day, code, hhmm)
            )""")

    def load_state(self, initial_cash: int) -> dict:
        row = self.conn.execute("SELECT body FROM state WHERE id=1").fetchone()
        if row:
            return json.loads(row["body"])
        return {"cash": float(initial_cash), "realized_pnl": 0.0, "positions": {}, "last_exits": {}}

    def transaction(self, trade: dict, state: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO trades (time, code, name, side, price, qty, pnl, pnl_pct, reason, cash_after, realized_pnl) "
                "VALUES (:time,:code,:name,:side,:price,:qty,:pnl,:pnl_pct,:reason,:cash_after,:realized_pnl)",
                trade,
            )
            self.conn.execute(
                "INSERT INTO state(id,body) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (json.dumps(state, ensure_ascii=False),),
            )
        self.export_csv()

    def save_bar(self, code: str, bar: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)",
                (bar["date"], code, bar["time"], bar["open"], bar["high"],
                 bar["low"], bar["close"], bar["volume"]),
            )

    def load_bars(self, code: str, date: str, limit: int = 180) -> list[dict]:
        rows = self.conn.execute(
            "SELECT day,hhmm,open,high,low,close,volume FROM bars "
            "WHERE code=? AND day=? ORDER BY hhmm DESC LIMIT ?",
            (code, date, limit),
        ).fetchall()
        return [
            {"date": r["day"], "time": r["hhmm"], "open": r["open"],
             "high": r["high"], "low": r["low"], "close": r["close"],
             "volume": r["volume"]}
            for r in reversed(rows)
        ]

    def last_known_names(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT code, name FROM trades WHERE id IN "
            "(SELECT MAX(id) FROM trades GROUP BY code)"
        ).fetchall()
        return {r["code"]: r["name"] for r in rows if r["name"]}

    def buys_today(self, code: str, yyyy_mm_dd: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE code=? AND side='BUY' AND substr(time,1,10)=?",
            (code, yyyy_mm_dd),
        ).fetchone()[0])

    def export_csv(self) -> None:
        try:
            for table, target in (
                ("trades", LOCAL / "paper_trades.csv"),
                ("performance", LOCAL / "paper_performance.csv"),
            ):
                if table == "trades":
                    rows = self.conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
                    fields = [x[1] for x in self.conn.execute("PRAGMA table_info(trades)").fetchall()]
                else:
                    rows = self.conn.execute(
                        "SELECT time, cash_after, realized_pnl, "
                        "ROUND(realized_pnl * 100.0 / ?, 4) AS realized_return_pct, "
                        "COUNT(*) OVER (ORDER BY id) AS closed_trades, "
                        "ROUND(100.0 * SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) "
                        "OVER (ORDER BY id) / COUNT(*) OVER (ORDER BY id), 2) AS win_rate_pct "
                        "FROM trades WHERE side='SELL' ORDER BY id",
                        (self.initial_cash,),
                    ).fetchall()
                    fields = ["time", "cash_after", "realized_pnl", "realized_return_pct", "closed_trades", "win_rate_pct"]
                with target.open("w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows([dict(r) for r in rows])
        except OSError as exc:
            print(f"[CSV WARN] 출력 실패: {type(exc).__name__} (SQLite에는 거래 저장됨)")

    initial_cash: int = 10_000_000


class Candle1Min:
    """Completed candles only; WS ticks build current candle, never forecast it."""

    def __init__(self):
        self.day = ""
        self.current: dict | None = None
        self.bars: dict[str, dict] = {}

    def load_bars(self, rows: list[dict], day: str) -> None:
        if self.day != day:
            self.day = day
            self.bars.clear()
            self.current = None
        for r in rows:
            if r.get("date") == day and len(str(r.get("time", ""))) == 4:
                self.bars[r["time"]] = r
        self._trim()

    def _trim(self) -> None:
        if len(self.bars) > 200:
            for key in sorted(self.bars)[:-200]:
                del self.bars[key]

    def add_tick(self, day: str, hhmmss: str, price: int, vol: int) -> dict | None:
        hhmm = hhmmss[:4]
        if self.day != day:
            self.day = day
            self.bars.clear()
            self.current = None
        if self.current is not None:
            if hhmm < self.current["time"]:
                return None  # Out-of-order/late tick; do not corrupt candles.
            if hhmm != self.current["time"]:
                closed = self.current
                self.bars[closed["time"]] = closed
                self._trim()
                self.current = None
            else:
                closed = None
        else:
            closed = None
        if self.current is None:
            self.current = {"date": day, "time": hhmm, "open": price,
                            "high": price, "low": price, "close": price, "volume": vol}
        else:
            c = self.current
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            c["volume"] += vol
        return closed

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.bars[k] for k in sorted(self.bars)])


def build_three_minute_bars(df: pd.DataFrame, day: str, at_hhmm: str) -> tuple[pd.DataFrame, dict]:
    """Require three distinct valid minutes per closed, clock-aligned 3m bucket."""
    columns = ["open", "high", "low", "close", "volume"]
    meta = {"one_minute": 0, "incomplete": 0, "invalid": 0, "duplicates": 0}
    empty = pd.DataFrame(columns=columns)
    if df.empty:
        return empty, meta
    if not set(["time"] + columns).issubset(df.columns):
        meta["invalid"] = len(df)
        return empty, meta
    current = pd.to_datetime(day + at_hhmm, format="%Y%m%d%H%M")
    times = pd.to_datetime(day + df["time"].astype(str), format="%Y%m%d%H%M", errors="coerce")
    tmp = df[columns].apply(pd.to_numeric, errors="coerce")
    tmp.index = pd.DatetimeIndex(times)
    good = tmp.notna().all(axis=1) & ~tmp.index.isna()
    good &= tmp.apply(lambda c: c.map(lambda x: math.isfinite(x))).all(axis=1)
    good &= (tmp[["open", "high", "low", "close"]] > 0).all(axis=1) & (tmp["volume"] >= 0)
    if "date" in df.columns:
        good &= (df["date"].astype(str).to_numpy() == day)
    meta["invalid"] = int((~good).sum())
    tmp = tmp.loc[good]
    # Current/future 1m candles are not closed data.
    tmp = tmp[(tmp.index < current) & (tmp.index >= current.normalize())]
    duplicates = tmp.index.duplicated(keep=False)
    meta["duplicates"] = int(duplicates.sum())
    tmp = tmp[~duplicates].sort_index()
    meta["one_minute"] = len(tmp)
    if tmp.empty:
        return empty, meta
    grouped = tmp.resample("3min", label="left", closed="left", origin="start_day")
    bars = grouped.agg({"open": "first", "high": "max", "low": "min",
                        "close": "last", "volume": "sum"})
    counts = grouped["close"].count()
    # Subtract from a timestamp instead of adding to an index (NumPy compatibility).
    closed = bars.index <= dt.datetime.strptime(day + at_hhmm, "%Y%m%d%H%M") - dt.timedelta(minutes=3)
    meta["incomplete"] = int(((counts != 3) & closed).sum())
    bars = bars.loc[(counts == 3) & closed].dropna()
    return bars, meta


def three_minute_check(df: pd.DataFrame, day: str, at_hhmm: str) -> tuple[bool, str]:
    bars, meta = build_three_minute_bars(df, day, at_hhmm)
    info = (f"1분봉 {meta['one_minute']}개, 확정3분봉 {len(bars)}개, "
            f"불완전구간 {meta['incomplete']}개")
    if meta["invalid"] or meta["duplicates"]:
        info += f", 무효행 {meta['invalid']}개, 중복행 {meta['duplicates']}개"
    if len(bars) < 3:
        return False, f"3분봉 부족({info}; 최소 3개 필요)"
    last = bars.iloc[-1]
    mean3 = bars["close"].iloc[-3:].mean()
    reasons = []
    if last["close"] <= last["open"]:
        reasons.append("음봉/도지")
    if last["close"] < mean3:
        reasons.append("종가가 최근3봉 평균 미만")
    details = (f"{info}; 최근봉 {bars.index[-1]:%H:%M}, "
               f"시가 {last['open']:g}, 종가 {last['close']:g}, 3봉평균 {mean3:.2f}")
    if reasons:
        return False, f"3분봉 추세 탈락({' / '.join(reasons)}; {details})"
    return True, f"3분봉 통과({details})"


def three_minute_ok(df: pd.DataFrame, day: str, at_hhmm: str) -> bool:
    return three_minute_check(df, day, at_hhmm)[0]


def indicator_checks(df: pd.DataFrame, cfg: Config, profile: dict, score: float) -> dict[str, bool]:
    """Computes signal from only closed 1-minute bars; previous 20 for volume."""
    if len(df) < max(cfg.min_bars_required, cfg.breakout_minutes + 1):
        return {"history": False}
    d = df.copy()
    baseline = d["volume"].iloc[-21:-1].mean()   # exclude signal bar
    high_prev = d["high"].iloc[-cfg.breakout_minutes-1:-1].max()
    last = d.iloc[-1]
    vr = last["volume"] / baseline if baseline > 0 else 0.0
    body = (last["close"] - last["open"]) / last["open"] if last["open"] else 0.0
    # score remains a diagnostic input; no extra market-breadth threshold adjustment.
    vol_th, body_th = profile["vol"], profile["body"]
    return {
        "history": True,
        "liquidity": baseline >= cfg.min_volume_ma20,
        "volume": vr >= vol_th,
        "breakout": last["close"] > high_prev,
        "body": body >= body_th,
    }


def time_profile(now: dt.datetime) -> dict | None:
    hhmm = int(now.strftime("%H%M"))
    if 910 <= hhmm < 1100:
        return {"period": "오전", "vol": 1.4, "body": 0.0015}
    if 1100 <= hhmm < 1300:
        return {"period": "점심", "vol": 1.2, "body": 0.001}
    if 1300 <= hhmm < 1510:
        return {"period": "오후", "vol": 1.3, "body": 0.001}
    return None


class PaperBot:
    def __init__(self, cfg: Config, store: Store, names: dict[str, str]):
        self.cfg, self.store, self.names = cfg, store, names
        self.names = {**store.last_known_names(), **names}
        state = store.load_state(cfg.initial_cash)
        self.cash = float(state["cash"])
        self.realized_pnl = float(state["realized_pnl"])
        self.positions: dict[str, dict] = state.get("positions", {})
        self.last_exits: dict[str, dict] = state.get("last_exits", {})
        self.candles: dict[str, Candle1Min] = {}
        self.hot_rank: list[str] = []
        self.desired_codes: list[str] = pin_positions([], self.positions, cfg.hot_count)
        self.market_score = 50.0
        self.last_score_time = now_kst() - dt.timedelta(days=1)
        self.last_debug: dict[str, str] = {}
        self.check_status: dict[str, tuple[dt.datetime, str]] = {}
        self.ticks_received: dict[str, dt.datetime] = {}
        self.discord_queue: asyncio.Queue[str] = asyncio.Queue()
        print(f"[STATE] 복원: 가상현금 {self.cash:,.0f}원 / 미청산 {len(self.positions)}종목")
        if self.positions:
            print("[STATE] 복원된 보유종목을 HOT 30개 안에서 최우선 감시합니다.")

    def name(self, code: str) -> str:
        return self.names.get(code, code)

    def persist(self, log: dict) -> None:
        self.store.transaction(log, {
            "cash": self.cash, "realized_pnl": self.realized_pnl,
            "positions": self.positions, "last_exits": self.last_exits,
        })

    def send_trade_message(self, text: str) -> None:
        """Log synchronously; send via background event-loop task, never block tick parsing."""
        print("[PAPER]", text.replace("\n", " | "))
        self.discord_queue.put_nowait(text)

    def _trade_log(self, code: str, price: float, qty: int, side: str, reason: str,
                   now: dt.datetime, pnl=0.0, pnl_pct=0.0) -> dict:
        return {
            "time": now.isoformat(timespec="seconds"), "code": code,
            "name": self.name(code), "side": side, "price": price, "qty": qty,
            "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
            "cash_after": self.cash, "realized_pnl": self.realized_pnl,
        }

    def buy(self, code: str, price: float, now: dt.datetime) -> bool:
        if code in self.positions or len(self.positions) >= self.cfg.max_open_positions:
            return False
        qty = min(int(self.cfg.trade_budget // price), int(self.cash // price)) if price > 0 else 0
        if qty <= 0:
            return False
        self.cash -= qty * price
        self.positions[code] = {
            "entry": price, "qty": qty, "entry_time": now.isoformat(timespec="seconds"),
        }
        self.persist(self._trade_log(code, price, qty, "BUY", "1m_3m_breakout", now))
        self.send_trade_message(
            f"📈 [가상 매수] {self.name(code)}({code})\n"
            f"시간 {now:%Y-%m-%d %H:%M:%S} | 가격 {price:,.0f}원 | 수량 {qty}주\n"
            f"근거: 1분봉 거래량·돌파 + 3분봉 추세 (실제 주문 아님)"
        )
        return True

    def sell(self, code: str, price: float, now: dt.datetime, reason: str) -> bool:
        pos = self.positions.get(code)
        if not pos or price <= 0:
            return False
        entry, qty = float(pos["entry"]), int(pos["qty"])
        pnl = (price - entry) * qty
        pnl_pct = (price / entry - 1) * 100
        self.cash += qty * price
        self.realized_pnl += pnl
        del self.positions[code]
        self.last_exits[code] = {"time": now.isoformat(timespec="seconds"), "reason": reason}
        self.persist(self._trade_log(code, price, qty, "SELL", reason, now, pnl, pnl_pct))
        # Release the hot-list slot only when the ranking refreshes; no ghost positions.
        labels = {"stop_loss": "손절", "take_profit": "익절", "flow_exit": "수급 약화"}
        self.send_trade_message(
            f"💰 [가상 매도 · {labels.get(reason, reason)}] {self.name(code)}({code})\n"
            f"시간 {now:%Y-%m-%d %H:%M:%S} | 매수가 {entry:,.0f}원 | 매도가 {price:,.0f}원\n"
            f"수량 {qty}주 | 거래 손익 {pnl:+,.0f}원 ({pnl_pct:+.2f}%)\n"
            f"누적 실현손익 {self.realized_pnl:+,.0f}원"
        )
        return True

    def set_hot_rank(self, rank: list[str], breadth: float) -> list[str]:
        self.hot_rank = rank
        self.market_score = max(0.0, min(100.0, breadth * 100.0))
        self.last_score_time = now_kst()
        wanted = pin_positions(rank, self.positions, self.cfg.hot_count)
        new = [c for c in wanted if c not in self.desired_codes]
        self.desired_codes = wanted
        print(f"[HOT] 시총 유니버스에서 선정 {len(wanted)}개 (보유종목 우선); "
              f"진입 {len(new)}개; 시장 대용 점수 {self.market_score:.0f}")
        return new

    def candle(self, code: str, day: str) -> Candle1Min:
        c = self.candles.setdefault(code, Candle1Min())
        if c.day != day:
            rows = self.store.load_bars(code, day)
            c.load_bars(rows, day)
        return c

    def merge_backfill(self, code: str, bars: list[dict]) -> None:
        if not bars:
            print(f"[BACKFILL] {self.name(code)}({code}) 데이터 없음. 봉 축적까지 신규매수 금지")
            return
        day = now_kst().strftime("%Y%m%d")
        c = self.candle(code, day)
        c.current = None  # Discard stale partial minute after leaving/re-entering HOT.
        c.load_bars(bars, day)  # Merge, NOT replace earlier live bars.
        for bar in bars:
            if bar["date"] == day:
                self.store.save_bar(code, bar)
        detail_print(f"[BACKFILL] {self.name(code)}({code}) 확정 1분봉 {len(bars)}개 확인")

    def _debug_skip(self, code: str, now: dt.datetime, reasons: list[str]) -> None:
        reason = "기타 대기"
        if any("분봉 부족" in r for r in reasons):
            reason = "봉 부족"
        elif any("3분봉 추세 탈락" in r for r in reasons):
            reason = "추세 탈락"
        elif any("3분봉 통과" in r for r in reasons):
            reason = "3분봉 통과"
        self.check_status[code] = (now, reason)
        minute = now.strftime("%Y%m%d%H%M")
        if self.last_debug.get(code) == minute:
            return
        self.last_debug[code] = minute
        detail_print(f"[SKIP] {self.name(code)}({code}) " + ", ".join(reasons))


    def on_tick(self, code: str, day: str, hhmmss: str, price: int, vol: int) -> None:
        if code not in self.desired_codes or price <= 0 or vol < 0:
            return
        try:
            at = dt.datetime.strptime(day + hhmmss, "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            return
        if not (dt.time(9) <= at.time() < dt.time(15, 30)):
            return
        if at.date() != now_kst().date() or abs((now_kst() - at).total_seconds()) > 120:
            return  # Never paper-trade using old/replayed ticks.
        # No reliance on stale prior-day candles; this tick is the executable quote proxy.
        self.ticks_received[code] = now_kst()
        c = self.candle(code, day)
        if c.current is not None and hhmmss[:4] < c.current["time"]:
            return  # Ignore out-of-order tick before risk and candle updates.
        finished = c.add_tick(day, hhmmss, price, abs(vol))
        # Stop-loss / take-profit use current tick, independent of 20-bar history.
        pos = self.positions.get(code)
        if pos:
            change = price / pos["entry"] - 1
            if change <= -self.cfg.stop_loss_pct:
                self.sell(code, price, at, "stop_loss")
                return
            if change >= self.cfg.take_profit_pct:
                self.sell(code, price, at, "take_profit")
                return
        if finished is not None:
            self.store.save_bar(code, finished)
            self._on_closed_bar(code, c, at, price)

    def _on_closed_bar(self, code: str, candle: Candle1Min,
                       tick_time: dt.datetime, price: float) -> None:
        df = candle.frame()
        if df.empty or len(df) < max(self.cfg.min_bars_required, self.cfg.breakout_minutes + 1):
            self._debug_skip(code, tick_time, [f"1분봉 부족 {len(df)}/{self.cfg.min_bars_required}"])
            return
        # A flow exit based on *closed* negative candle; evaluate even when entries are disabled.
        last = df.iloc[-1]
        mean20 = df["volume"].iloc[-21:-1].mean()
        body_pct = (last["close"] / last["open"] - 1) if last["open"] else 0
        if code in self.positions:
            if mean20 > 0 and last["volume"] < mean20 * 0.5 and body_pct <= -0.004:
                self.sell(code, price, tick_time, "flow_exit")
            return
        profile = time_profile(tick_time)
        if profile is None:
            return
        if len(self.positions) >= self.cfg.max_open_positions:
            self._debug_skip(code, tick_time, ["포지션 수 상한"])
            return
        today = tick_time.strftime("%Y-%m-%d")
        if self.store.buys_today(code, today) >= self.cfg.max_buys_per_stock_per_day:
            self._debug_skip(code, tick_time, ["종목별 당일 진입 횟수 초과"])
            return
        last_exit = self.last_exits.get(code)
        if last_exit and last_exit.get("reason") == "stop_loss":
            try:
                exit_time = dt.datetime.fromisoformat(last_exit["time"])
                if tick_time - exit_time < dt.timedelta(minutes=self.cfg.stop_reentry_minutes):
                    self._debug_skip(code, tick_time, ["손절 후 재진입 대기"])
                    return
            except ValueError:
                pass
        score = self.market_score if now_kst() - self.last_score_time < dt.timedelta(minutes=20) else 50.0
        tests = indicator_checks(df, self.cfg, profile, score)
        if not tests.get("history"):
            return
        pass3, detail3 = three_minute_check(df, tick_time.strftime("%Y%m%d"), tick_time.strftime("%H%M"))
        failed = [k for k, good in tests.items() if not good]
        if failed or not pass3:
            self._debug_skip(code, tick_time, failed + [detail3])
            return
        detail_print(f"[SIGNAL] {self.name(code)}({code}) {detail3}")
        self.check_status[code] = (tick_time, "3분봉 통과")
        self.buy(code, price, tick_time)


def parse_ws_ticks(raw: str, default_day: str) -> list[tuple[str, str, str, int, int]]:
    """Parse KIS '0|H0STCNT0|N|code^...^cntg_vol^...' messages."""
    if not raw.startswith("0|"):
        return []
    chunks = raw.split("|", 3)
    if len(chunks) != 4 or chunks[1] != TR_TICK:
        return []
    n = max(1, safe_int(chunks[2]))
    fields = chunks[3].split("^")
    stride = len(fields) // n
    if stride < 14 or len(fields) % n:
        return []
    ticks = []
    for i in range(n):
        f = fields[i * stride:(i + 1) * stride]
        code, hms = f[0], f[1]
        price, vol = safe_int(f[2]), safe_int(f[12])
        day = f[33] if len(f) > 33 and len(f[33]) == 8 and f[33].isdigit() else default_day
        if len(code) == 6 and code.isdigit() and len(hms) == 6 and hms.isdigit() and price > 0:
            ticks.append((code, day, hms, price, vol))
    return ticks


async def scan_hot_loop(bot: PaperBot, api: KISClient,
                        universe: list[str], baseline: dict[str, float]) -> None:
    """REST scans the WHOLE universe; WS subscribes ONLY top 30 incl. positions."""
    universe_day = now_kst().date()
    while True:
        now = now_kst()
        if now.date() != universe_day:
            try:
                # Refresh market-cap rankings and completed daily volumes each local day.
                fresh_universe, fresh_names = await asyncio.to_thread(market_universe, bot.cfg, api)
                fresh_baseline = await asyncio.to_thread(daily_average_volumes, fresh_universe, bot.cfg, api)
                if fresh_baseline:
                    universe, baseline = fresh_universe, fresh_baseline
                    bot.names.update(fresh_names)
                    bot.hot_rank = []
                    bot.desired_codes = pin_positions([], bot.positions, bot.cfg.hot_count)
                    print("[UNIVERSE] 날짜 변경: 전일 HOT 해제, 포지션 우선 감시")
                    universe_day = now.date()
            except Exception as exc:
                print(f"[UNIVERSE WARN] 일간 갱신 실패: {type(exc).__name__}")
                await asyncio.sleep(60)
                continue
        if now.weekday() > 4 or not (dt.time(9, 10) <= now.time() < dt.time(15, 25)):
            await asyncio.sleep(15)
            continue
        try:
            snapshots = {}
            for code in universe:
                quote = await asyncio.to_thread(api.quote, code)
                if quote is not None:
                    snapshots[code] = quote
                await asyncio.sleep(bot.cfg.quote_pause_seconds)
            rank, breadth = select_hot(snapshots, baseline, bot.cfg)
            if rank:
                # Hot newcomers get authentic completed KIS 1m data before WS admission.
                wanted = pin_positions(rank, bot.positions, bot.cfg.hot_count)
                for code in wanted:
                    if code not in bot.desired_codes:
                        bars = await asyncio.to_thread(api.minute_bars, code, 60)
                        bot.merge_backfill(code, bars)
                        await asyncio.sleep(0.25)
                bot.set_hot_rank(rank, breadth)
            else:
                print("[HOT] 유효한 현재가/기준 거래량이 부족합니다. 기존 구독/보유종목 유지")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[HOT WARN] 재선정 실패: {type(exc).__name__}, 보유 종목 감시 지속")
        await asyncio.sleep(bot.cfg.hot_refresh_seconds)


def subscription_message(approval_key: str, code: str, subscribe: bool) -> str:
    return json.dumps({
        "header": {
            "approval_key": approval_key, "custtype": "P",
            "tr_type": "1" if subscribe else "2",  # KIS auth wrapper unsubscribe
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": TR_TICK, "tr_key": code}},
    })


async def ws_loop(bot: PaperBot, api: KISClient) -> None:
    """One websocket; remove old subscriptions before adding new subscriptions."""
    while True:
        n = now_kst()
        if n.weekday() > 4 or not (dt.time(8, 55) <= n.time() <= dt.time(15, 35)):
            await asyncio.sleep(30)  # Avoid repeated reconnects overnight/weekends.
            continue
        if not bot.desired_codes:
            await asyncio.sleep(2)
            continue
        try:
            approval_key = await asyncio.to_thread(api.approval_key)
            subscribed: set[str] = set()
            async with websockets.connect(bot.cfg.ws_url, ping_interval=20, ping_timeout=20) as ws:
                print("[WS] 연결됨. HOT 30개 이내 구독 조정 중")
                while True:
                    target = set(bot.desired_codes)
                    # Evict first, then subscribe. Never exceed HOT_SIZE on client.
                    for code in sorted(subscribed - target):
                        await ws.send(subscription_message(approval_key, code, False))
                        subscribed.discard(code)
                        await asyncio.sleep(0.25)
                    for code in bot.desired_codes:
                        if code not in subscribed:
                            if len(subscribed) >= bot.cfg.hot_count:
                                break
                            await ws.send(subscription_message(approval_key, code, True))
                            subscribed.add(code)
                            await asyncio.sleep(0.15)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    if raw.startswith("{"):
                        try:
                            msg = json.loads(raw)
                            if msg.get("header", {}).get("tr_id") == "PINGPONG":
                                await ws.pong(raw.encode("utf-8"))
                                continue
                            body = msg.get("body") or {}
                            if str(body.get("rt_cd", "0")) != "0":
                                key = msg.get("header", {}).get("tr_key", "미상")
                                print(f"[WS WARN] {key} 구독 거절: {body.get('msg1', '')}")
                                if "MAX SUBSCRIBE" in str(body.get("msg1", "")):
                                    print("[WS WARN] 구독한도에 걸렸습니다. 해당 종목 실제 수신 여부를 확인하세요.")
                        except ValueError:
                            pass
                    elif raw.startswith("0|"):
                        date = now_kst().strftime("%Y%m%d")
                        for code, day, hms, price, volume in parse_ws_ticks(raw, date):
                            bot.on_tick(code, day, hms, price, volume)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WS] 연결 종료: {type(exc).__name__}. 5초 뒤 재연결")
            await asyncio.sleep(5)


async def discord_loop(bot: PaperBot) -> None:
    """Discord transport is isolated from the market-data event loop."""
    while True:
        message = await bot.discord_queue.get()
        try:
            res = await asyncio.to_thread(
                requests.post,
                bot.cfg.discord_webhook,
                json={"content": message},
                timeout=5,
            )
            if res.status_code not in (200, 204):
                print(f"[DISCORD WARN] HTTP {res.status_code} (SQLite 거래기록은 유지됨)")
        except requests.RequestException:
            print("[DISCORD WARN] 전송 실패 (SQLite 거래기록은 유지됨)")
        finally:
            bot.discord_queue.task_done()


def status_line(bot: PaperBot, now: dt.datetime) -> str:
    target = set(bot.desired_codes)
    live = sum(code in target and 0 <= (now - t).total_seconds() < 180
               for code, t in bot.ticks_received.items())
    active = now.weekday() < 5 and dt.time(9, 0) <= now.time() < dt.time(15, 30)
    if not active:
        phase = "장외 대기"
    elif not target:
        phase = "후보 준비/장 시작 대기"
    elif live == len(target):
        phase = "감시 중"
    else:
        phase = "수신 확인 필요"
    counts = {label: 0 for label in ("봉 부족", "추세 탈락", "3분봉 통과")}
    for code, (at, reason) in bot.check_status.items():
        if code in target and 0 <= (now - at).total_seconds() < 180 and reason in counts:
            counts[reason] += 1
    checks = (f"최근3분 판단: 봉부족 {counts['봉 부족']} / "
              f"추세탈락 {counts['추세 탈락']} / 3분봉통과 {counts['3분봉 통과']}")
    return (f"[{now:%H:%M:%S}] {phase} | 최근3분 수신 {live}/{len(target)} | "
            f"보유 {len(bot.positions)} | 실현손익 {bot.realized_pnl:+,.0f}원 | {checks}")


async def heartbeat_loop(bot: PaperBot) -> None:
    while True:
        now = now_kst()
        print(status_line(bot, now))
        if now.weekday() < 5 and dt.time(9, 0) <= now.time() < dt.time(15, 30):
            for code in bot.positions:
                t = bot.ticks_received.get(code)
                if not t or (now - t).total_seconds() > 180:
                    print(f"[주의] 보유종목 {bot.name(code)}({code}) 3분 이상 수신 없음. 연결 확인 필요")
        await asyncio.sleep(60)


async def start_hot_scanner(bot: PaperBot, api: KISClient) -> None:
    """Slow baseline prep runs alongside websocket monitoring of saved positions."""
    while True:
        try:
            # Do not let slow KIS calls delay WS monitoring of yesterday's positions.
            await asyncio.to_thread(api.access_token)
            universe, names = await asyncio.to_thread(market_universe, bot.cfg, api)
            baseline = await asyncio.to_thread(daily_average_volumes, universe, bot.cfg, api)
            bot.names.update(names)
            if not baseline:
                print("[HOT WARN] 과거 일봉 거래량 조회 실패: 신규 종목 스캔 일시 중단")
                await asyncio.sleep(120)
                continue
            await scan_hot_loop(bot, api, universe, baseline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[HOT WARN] 데이터 준비 실패: {type(exc).__name__}. 120초 뒤 재시도")
            if isinstance(exc, KISAPIError):
                print(f"[HOT WARN] {exc}")
            await asyncio.sleep(120)


async def main() -> int:
    cfg = load_config()
    if cfg is None:
        return 2
    print("[시작] 한투 가상매매 — 준비 후 1분마다 상태 표시 (실제 주문 없음)")
    print("[조건] 완화형: 10분 돌파 · 거래량 증가 · 양봉 · 3분봉 추세")
    print(f"[환경] 시세 서버: {cfg.kis_env}, 시총 {cfg.universe_from}~{cfg.universe_to}위, "
          f"WS 최대 {cfg.hot_count}종목 (보유 포함)")
    store = Store(LOCAL / "paper_trades.sqlite3")
    store.initial_cash = cfg.initial_cash
    api = KISClient(cfg)
    # IMPORTANT: restore positions BEFORE the slow KIS daily-average scans.
    bot = PaperBot(cfg, store, {})
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(ws_loop(bot, api))
            tasks.create_task(start_hot_scanner(bot, api))
            tasks.create_task(heartbeat_loop(bot))
            tasks.create_task(discord_loop(bot))
    except ExceptionGroup as eg:
        print(f"[중단] 실행 작업 오류: {type(eg.exceptions[0]).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    warnings.showwarning = compact_warning
    if "--self-test" in sys.argv:
        test_cfg = Config("test", "test", "https://discord.com/api/webhooks/example/example")
        tiny = pd.DataFrame([
            {"time": f"09{i:02d}", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}
            for i in range(0, 30)
        ])
        print("[SELF-TEST] 1분봉/3분봉 정적 확인:",
              three_minute_ok(tiny, "20261201", "0930"),
              "\n[SELF-TEST] 민감정보 불필요 / 실계좌 주문 없음")
        sys.exit(0)
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[종료] Ctrl+C 감지. SQLite 기록은 보존됩니다.")
        sys.exit(0)

    except Exception as exc:
        if VERBOSE:
            raise
        print(f"[중단] {type(exc).__name__}: 실행 오류. 상세 모드로 원인 확인이 필요합니다.")
        sys.exit(1)


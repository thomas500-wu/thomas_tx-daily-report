"""抓取台指期報告所需的原始資料。

全部來自免 API key 的公開來源:
  - 期交所 TAIFEX OpenAPI  https://openapi.taifex.com.tw/v1/...
  - 證交所 TWSE  OpenAPI   https://openapi.twse.com.tw/v1/...
  - Yahoo Finance(加權指數收盤 / ATR 代理歷史)

每個 fetch_* 函式都盡量「壞了就回 None / 空 dict」,讓 render 端能降級處理,
而不是整份報告掛掉。
"""

from __future__ import annotations

import csv
import io
import sys

import requests

TAIFEX = "https://openapi.taifex.com.tw/v1"
TWSE = "https://openapi.twse.com.tw/v1"
UA = {"User-Agent": "Mozilla/5.0 (thomas_tx-daily-report; GitHub Actions)"}
TIMEOUT = 40


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _taifex_json(path: str) -> list[dict]:
    return _get(f"{TAIFEX}/{path}").json()


def _taifex_csv(path: str) -> list[dict]:
    text = _get(f"{TAIFEX}/{path}").content.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def to_float(value) -> float | None:
    try:
        s = str(value).replace(",", "").strip()
        if s in ("", "-", "NULL", "None", "N/A"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _front_month(rows: list[dict], key: str) -> str | None:
    """近月 = 最小的 6 碼純數字月份(排除週契約與價差)。"""
    months = {str(r.get(key, "")).strip() for r in rows}
    months = {m for m in months if m.isdigit() and len(m) == 6}
    return min(months) if months else None


# --------------------------------------------------------------------------- #
# 期貨行情:TX / MTX 近月,日盤 + 夜盤
# --------------------------------------------------------------------------- #
def fetch_futures() -> dict:
    try:
        rows = _taifex_json("DailyMarketReportFut")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] DailyMarketReportFut 失敗:{exc}", file=sys.stderr)
        return {}

    out: dict = {}
    for code in ("TX", "MTX"):
        recs = [r for r in rows if str(r.get("Contract", "")).strip() == code]
        front = _front_month(recs, "ContractMonth(Week)")
        fm = [r for r in recs if str(r.get("ContractMonth(Week)", "")).strip() == front]
        if not fm:
            continue
        day = next((r for r in fm if str(r.get("TradingSession", "")).strip() == "一般"), None)
        aft = next((r for r in fm if str(r.get("TradingSession", "")).strip() == "盤後"), None)
        base = day or fm[0]
        out[code] = {
            "date": str(base.get("Date", "")).strip(),
            "month": front,
            "open": to_float(base.get("Open")),
            "high": to_float(base.get("High")),
            "low": to_float(base.get("Low")),
            "close": to_float(base.get("SettlementPrice")) or to_float(base.get("Last")),
            "last": to_float(base.get("Last")),
            "change": to_float(base.get("Change")),
            "pct": to_float(str(base.get("%", "")).rstrip("%")),
            "aft_last": to_float(aft.get("Last")) if aft else None,
            "aft_change": to_float(aft.get("Change")) if aft else None,
            "aft_open": to_float(aft.get("Open")) if aft else None,
            "aft_high": to_float(aft.get("High")) if aft else None,
            "aft_low": to_float(aft.get("Low")) if aft else None,
        }
    return out


# --------------------------------------------------------------------------- #
# 三大法人 — 臺股期貨契約別(自營 / 投信 / 外資)
# --------------------------------------------------------------------------- #
_INST_KEYS = {"自營商": "dealer", "投信": "invtrust", "外資及陸資": "foreign"}


def fetch_institution_futures() -> dict:
    try:
        rows = _taifex_json("MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 三大法人(期貨契約別)失敗:{exc}", file=sys.stderr)
        return {}

    out: dict = {"date": None}
    for r in rows:
        if str(r.get("ContractCode", "")).strip() != "臺股期貨":
            continue
        item = str(r.get("Item", "")).strip()
        key = _INST_KEYS.get(item)
        if not key:
            continue
        out["date"] = str(r.get("Date", "")).strip()
        out[key] = {
            "name": item,
            "day_net": to_float(r.get("TradingVolume(Net)")),          # 單日淨口數(+買超 / -賣超)
            "oi_net": to_float(r.get("OpenInterest(Net)")),            # 未平倉淨口數
            "oi_long": to_float(r.get("OpenInterest(Long)")),
            "oi_short": to_float(r.get("OpenInterest(Short)")),
        }
    return out


# --------------------------------------------------------------------------- #
# 三大法人 — 期貨 / 選擇權 二分總表(取數字口數淨額)
# --------------------------------------------------------------------------- #
def fetch_institution_divided() -> dict:
    try:
        rows = _taifex_json("MarketDataOfMajorInstitutionalTradersDividedByFuturesAndOptionsBytheDate")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 三大法人(期/選二分)失敗:{exc}", file=sys.stderr)
        return {}
    out: dict = {}
    for r in rows:
        key = _INST_KEYS.get(str(r.get("Item", "")).strip())
        if not key:
            continue
        out[key] = {
            "fut_oi_net": to_float(r.get("FuturesOpenInterest(Net)")),
            "opt_oi_net": to_float(r.get("OptionsOpenInterest(Net)")),
        }
    return out


# --------------------------------------------------------------------------- #
# 台指選擇權 Put/Call ratio
# --------------------------------------------------------------------------- #
def fetch_put_call_ratio() -> dict:
    try:
        rows = _taifex_json("PutCallRatio")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] PutCallRatio 失敗:{exc}", file=sys.stderr)
        return {}
    if not rows:
        return {}
    r = rows[0]  # 端點回傳最新在前
    return {
        "date": str(r.get("Date", "")).strip(),
        "volume_ratio": to_float(r.get("PutCallVolumeRatio%")),
        "oi_ratio": to_float(r.get("PutCallOIRatio%")),
        "put_oi": to_float(r.get("PutOI")),
        "call_oi": to_float(r.get("CallOI")),
    }


# --------------------------------------------------------------------------- #
# 選擇權 OI 牆:台指選(TXO)近月一般時段,買權 / 賣權未平倉最大的履約價
# --------------------------------------------------------------------------- #
def fetch_oi_walls() -> dict:
    try:
        rows = _taifex_csv("DailyMarketReportOpt")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] DailyMarketReportOpt 失敗:{exc}", file=sys.stderr)
        return {}

    txo = [r for r in rows if str(r.get("契約", "")).strip() == "TXO"
           and str(r.get("交易時段", "")).strip() == "一般"]
    # 取「今日成交量最大」的到期別(通常是最近的週選),那才是當下有意義的 OI 牆
    vol_by_exp: dict[str, float] = {}
    for r in txo:
        exp = str(r.get("到期月份(週別)", "")).strip()
        vol_by_exp[exp] = vol_by_exp.get(exp, 0) + (to_float(r.get("成交量")) or 0)
    if not vol_by_exp:
        return {}
    front = max(vol_by_exp, key=vol_by_exp.get)
    fm = [r for r in txo if str(r.get("到期月份(週別)", "")).strip() == front]
    if not fm:
        return {}

    def wall(kind: str) -> dict | None:
        best = None
        for r in fm:
            if str(r.get("買賣權", "")).strip() != kind:
                continue
            oi = to_float(r.get("未沖銷契約量"))
            strike = to_float(r.get("履約價"))
            if oi is None or strike is None:
                continue
            if best is None or oi > best["oi"]:
                best = {"strike": strike, "oi": oi}
        return best

    return {"month": front, "call": wall("買權"), "put": wall("賣權")}


# --------------------------------------------------------------------------- #
# 加權指數收盤(TWSE OpenAPI MI_INDEX,官方每日收盤;取代原本的 Yahoo ^TWII)
#
# 2026-08 踩坑:Yahoo ^TWII 是即時報價,同一交易日多次抓會抓到不同快照(差 200~400
# 點),導致「加權指數收盤」「期現價差」不穩定。改用證交所官方 MI_INDEX,一天只
# 公布一次收盤值,穩定且免 key。回傳已含當日漲跌/漲跌%,不用再自己抓前一日算。
# --------------------------------------------------------------------------- #
def fetch_taiex() -> dict:
    try:
        rows = _taifex_like_twse_json("exchangeReport/MI_INDEX")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] TWSE MI_INDEX 失敗:{exc}", file=sys.stderr)
        return {}

    row = next((r for r in rows if str(r.get("指數", "")).strip() == "發行量加權股價指數"), None)
    if not row:
        return {}
    close = to_float(row.get("收盤指數"))
    pts = to_float(row.get("漲跌點數"))
    sign = -1 if str(row.get("漲跌", "")).strip() == "-" else 1
    pct = to_float(row.get("漲跌百分比"))
    return {
        "date": _roc_to_iso(row.get("日期")),
        "close": close,
        "change": (sign * pts) if pts is not None else None,
        "pct": pct,
    }


# 沿用舊名,避免其他地方忘了改;新程式碼請用 fetch_taiex()
fetch_twii = fetch_taiex


def _roc_to_iso(raw) -> str:
    """民國年 8 碼(如 1150831)轉西元 ISO(2026-08-31)。"""
    s = str(raw or "").strip()
    if len(s) == 7 and s.isdigit():  # 民國年通常 3 碼(115)
        y, m, d = int(s[:3]) + 1911, s[3:5], s[5:]
        return f"{y}-{m}-{d}"
    return s


def _taifex_like_twse_json(path: str) -> list[dict]:
    return _get(f"{TWSE}/{path}").json()


# --------------------------------------------------------------------------- #
# 現貨三大法人買賣超
#
# 2026-08 踩坑:TWSE OpenAPI 的 `v1/fund/BFI82U` 已經不存在(swagger 裡查無此
# path,呼叫只會拿到一個偽 404 頁面、HTTP 200,`.json()` 直接炸掉,被 try/except
# 吞掉後整區塊悄悄消失,長期以為是「休市才空」,其實是端點本身失效)。改走證交
# 所網站本身在用的舊版 AJAX JSON 端點,已驗證交易日有正常回傳。
# --------------------------------------------------------------------------- #
def fetch_spot_institution() -> dict:
    url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&date=&_=0"
    try:
        payload = _get(url).json()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] TWSE BFI82U(現貨三大法人)失敗(略過現貨法人):{exc}", file=sys.stderr)
        return {}

    if str(payload.get("stat", "")).strip() != "OK":
        return {}

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    try:
        name_i = fields.index("單位名稱")
        net_i = fields.index("買賣差額")
    except ValueError:
        return {}

    # 自營商拆「自行買賣」/「避險」兩列;其餘三大法人(投信/外資)各一列直接對應。
    # 外資自營商已計入自營商買賣金額,依證交所備註不重複納入三大法人合計,故略過。
    label = {
        "外資及陸資(不含外資自營商)": "foreign",
        "外資及陸資": "foreign",
        "投信": "invtrust",
        "自營商(自行買賣)": "dealer",
        "自營商(避險)": "dealer",
    }
    out: dict = {}
    for r in rows:
        if len(r) <= max(name_i, net_i):
            continue
        name = str(r[name_i]).strip()
        key = label.get(name)
        if not key:
            continue
        net = to_float(r[net_i])
        if net is None:
            continue
        prev = out.get(key)
        out[key] = {
            "name": "自營商" if key == "dealer" else name,
            "net_twd": (prev["net_twd"] + net) if prev else net,
        }
    return out

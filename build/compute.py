"""從原始資料算出報告要用的指標:樞紐點、支撐壓力、ATR 波動區間、規則式情緒分數。

Phase 1 的情緒分數是「規則式」初步判斷,只用來畫雷達圖;實際盤勢解讀與策略
在 Phase 3 由 Claude API 產生。
"""

from __future__ import annotations

import csv
import os
from datetime import datetime


# --------------------------------------------------------------------------- #
# 樞紐點 / 支撐壓力(古典 pivot)
# --------------------------------------------------------------------------- #
def pivots(high: float, low: float, close: float) -> dict:
    p = (high + low + close) / 3
    rng = high - low
    return {
        "P": p,
        "R1": 2 * p - low,
        "R2": p + rng,
        "R3": high + 2 * (p - low),
        "S1": 2 * p - high,
        "S2": p - rng,
        "S3": low - 2 * (high - p),
    }


# --------------------------------------------------------------------------- #
# 歷史日 K CSV(給 ATR)。欄位:date,open,high,low,close,source
# --------------------------------------------------------------------------- #
HIST_HEADER = ["date", "open", "high", "low", "close", "source"]


def load_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            out.append({
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "source": r.get("source", ""),
            })
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda x: x["date"])
    return out


def append_today(path: str, row: dict) -> list[dict]:
    """把今天的 OHLC 併進 CSV(同日期已存在則以新值覆蓋),回傳排序後的完整歷史。"""
    hist = {r["date"]: r for r in load_history(path)}
    if row.get("date") and all(row.get(k) is not None for k in ("high", "low", "close")):
        hist[row["date"]] = {
            "date": row["date"],
            "open": row.get("open") or row["close"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "source": row.get("source", "TX"),
        }
    merged = sorted(hist.values(), key=lambda x: x["date"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_HEADER)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in HIST_HEADER})
    return merged


def atr(history: list[dict], n: int = 14) -> float | None:
    if len(history) < n + 1:
        return None
    trs = []
    for i in range(1, len(history)):
        h, l = history[i]["high"], history[i]["low"]
        pc = history[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-n:]
    return sum(recent) / len(recent)


def vol_range(close: float, atr_val: float | None, mult: float = 1.0) -> dict | None:
    if atr_val is None:
        return None
    return {
        "low": close - atr_val * mult,
        "high": close + atr_val * mult,
        "atr": atr_val,
        "mult": mult,
    }


# --------------------------------------------------------------------------- #
# 規則式情緒分數(0~100,50 = 中性)
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def sentiment(ctx: dict) -> dict:
    """回傳 {axes: {軸: 分數}, overall: 分數, label: 文字}。純規則,僅供雷達圖參考。"""
    fut = ctx.get("futures", {}).get("TX", {}) or {}
    inst = ctx.get("institution_fut", {}) or {}
    pc = ctx.get("put_call", {}) or {}
    piv = ctx.get("pivots", {}) or {}
    hist = ctx.get("history", [])

    axes: dict[str, float] = {}

    # 1) 外資期貨淨部位方向(淨多 → 偏多;用 ±5 萬口當滿分刻度)
    foreign_oi = (inst.get("foreign") or {}).get("oi_net")
    if foreign_oi is not None:
        axes["外資期貨淨部位"] = _clamp(50 + foreign_oi / 50000 * 50)

    # 2) 外資單日期貨進出
    foreign_day = (inst.get("foreign") or {}).get("day_net")
    if foreign_day is not None:
        axes["外資單日進出"] = _clamp(50 + foreign_day / 8000 * 50)

    # 3) 價格位階(收盤相對樞紐點,用 ±1% 當刻度)
    if fut.get("close") and piv.get("P"):
        axes["價格位階"] = _clamp(50 + (fut["close"] / piv["P"] - 1) * 100 * 50)

    # 4) 期現價差(正價差偏多;用 ±60 點刻度)
    twii = ctx.get("twii", {}) or {}
    if fut.get("close") and twii.get("close"):
        basis = fut["close"] - twii["close"]
        axes["期現價差"] = _clamp(50 + basis / 60 * 50)
        ctx["basis"] = basis

    # 5) 選擇權 P/C OI ratio(>100 偏多;用 80~120 對應 0~100)
    if pc.get("oi_ratio"):
        axes["選擇權 P/C"] = _clamp((pc["oi_ratio"] - 80) / 40 * 100)

    # 6) 短期動能(收盤 vs 5 日前收盤,±3% 刻度)
    if fut.get("close") and len(hist) >= 6:
        base = hist[-6]["close"]
        if base:
            axes["短期動能"] = _clamp(50 + (fut["close"] / base - 1) / 0.03 * 50)

    overall = round(sum(axes.values()) / len(axes)) if axes else 50
    if overall >= 62:
        label = "偏多"
    elif overall >= 55:
        label = "偏多整理"
    elif overall > 45:
        label = "中性震盪"
    elif overall > 38:
        label = "偏空整理"
    else:
        label = "偏空"

    return {"axes": {k: round(v) for k, v in axes.items()}, "overall": overall, "label": label}


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

"""台指期每日報告產生器:抓資料 → 算指標 → 套模板 → 寫出 index.html 與存檔。

用法:
    python build/render.py --session day-close     # 台北 14:00,今日總結 + 夜盤預測
    python build/render.py --session next-day      # 台北 06:30,夜盤回顧 + 今日展望

Phase 1:數據層 + 圖表 + 規則式情緒雷達。盤勢解讀(AI)與收盤後驗證為佔位。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

import compute
import fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_CSV = os.path.join(ROOT, "data", "tx_daily.csv")
REPORTS_DIR = os.path.join(ROOT, "reports")
PRED_DIR = os.path.join(ROOT, "data", "predictions")
TPE = timezone(timedelta(hours=8))

SESSIONS = {
    "day-close": ("夜盤預測", "日盤收後 · 夜盤預測"),
    "next-day": ("今日展望", "夜盤回顧 · 今日展望"),
}


# --------------------------------------------------------------------------- #
# 格式化小工具（台股慣例:漲紅跌綠 → 正數 = up）
# --------------------------------------------------------------------------- #
def n0(x, dash="—"):
    return f"{x:,.0f}" if isinstance(x, (int, float)) else dash


def n1(x, dash="—"):
    return f"{x:,.1f}" if isinstance(x, (int, float)) else dash


def sgn(x, dec=0, dash="—"):
    if not isinstance(x, (int, float)):
        return dash
    return f"{x:+,.{dec}f}"


def pct(x, dash="—"):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else dash


def dircls(x):
    if not isinstance(x, (int, float)) or x == 0:
        return "flat"
    return "up" if x > 0 else "down"


def ymd(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def expected_data_date(now: datetime) -> str:
    """粗估『期交所此刻應該已經有的最新交易日』(ISO)。

    - 平日 14:20 之後 → 當天(日盤收盤/結算資料通常已公布)
    - 平日 14:20 之前、或週末 → 往前退到最近一個已收盤的平日

    無法判斷國定假日 / 颱風假,所以休市日會被算成「延遲」;寧可標出來讓人自己查,
    也不要靜靜顯示舊資料當成今天的。
    """
    d = now.date()
    after_close = now.hour > 14 or (now.hour == 14 and now.minute >= 20)
    if now.weekday() >= 5 or not after_close:
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # 週六=5、週日=6
        d -= timedelta(days=1)
    return d.isoformat()


# --------------------------------------------------------------------------- #
def build_context(session: str) -> dict:
    short, long = SESSIONS[session]
    now = datetime.now(TPE)

    futures = fetch.fetch_futures()
    tx = futures.get("TX") or {}
    if not tx.get("close"):
        print("[error] 取不到台指期近月收盤,略過本次產生(保留前一份 index.html)", file=sys.stderr)
        sys.exit(1)

    inst = fetch.fetch_institution_futures()
    pcr = fetch.fetch_put_call_ratio()
    walls = fetch.fetch_oi_walls()
    taiex = fetch.fetch_taiex()
    spot = fetch.fetch_spot_institution()

    data_date = ymd(tx.get("date")) or now.strftime("%Y-%m-%d")
    expected = expected_data_date(now)
    stale = {"have": data_date, "expected": expected} if data_date < expected else None

    # 樞紐點
    piv = compute.pivots(tx["high"], tx["low"], tx["close"]) if tx.get("high") and tx.get("low") else {}

    # 歷史 + ATR
    history = compute.append_today(HIST_CSV, {
        "date": data_date, "open": tx.get("open"), "high": tx.get("high"),
        "low": tx.get("low"), "close": tx.get("close"), "source": "TX",
    })
    atr_val = compute.atr(history, 14)
    vol_main = compute.vol_range(tx["close"], atr_val, 1.0)
    vol_tail = compute.vol_range(tx["close"], atr_val, 1.5)

    # 情緒(規則式)
    senti = compute.sentiment({
        "futures": futures, "institution_fut": inst, "put_call": pcr,
        "pivots": piv, "history": history, "twii": taiex,
    })
    basis = tx["close"] - taiex["close"] if taiex.get("close") else None

    # 走勢圖(近 30)
    tail = history[-30:]
    chart = {
        "n": len(tail),
        "labels": [r["date"][5:] for r in tail],
        "close": [round(r["close"], 1) for r in tail],
    }
    radar = {"labels": list(senti["axes"].keys()), "values": list(senti["axes"].values())}

    inst_rows = []
    for key in ("dealer", "invtrust", "foreign"):
        row = inst.get(key)
        if not row:
            continue
        inst_rows.append({
            "name": row["name"],
            "day_net": sgn(row["day_net"]), "day_dir": dircls(row["day_net"]),
            "oi_net": sgn(row["oi_net"]), "oi_dir": dircls(row["oi_net"]),
        })

    spot_ctx = None
    if spot:
        def yi(k):
            v = (spot.get(k) or {}).get("net_twd")
            return sgn(v / 1e8, 1) if isinstance(v, (int, float)) else "—"
        spot_ctx = {"foreign": yi("foreign"), "invtrust": yi("invtrust"), "dealer": yi("dealer")}

    d = {
        "title": f"台指期交易決策與分析儀表板 － {data_date} {short}",
        "data_date": data_date,
        "stale": stale,
        "session_label": long,
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "summary": {
            "contract": "TX 台指期", "month": tx.get("month", ""),
            "close": n0(tx["close"]), "dir": dircls(tx.get("change")),
            "change": sgn(tx.get("change")), "pct": pct(tx.get("pct")),
            "high": n0(tx.get("high")), "low": n0(tx.get("low")),
            "range": n0((tx["high"] - tx["low"]) if tx.get("high") and tx.get("low") else None),
            "aft_last": n0(tx.get("aft_last")), "aft_change": sgn(tx.get("aft_change")),
            "aft_dir": dircls(tx.get("aft_change")),
            "basis": sgn(basis) if basis is not None else "—",
        },
        "twii": {
            "close": n0(taiex.get("close")),
            "pct": pct(taiex.get("pct")),
            "dir": dircls(taiex.get("change")),
        },
        "sentiment": senti,
        "institution": inst_rows,
        "spot": spot_ctx,
        "pc": {
            "volume_ratio": n1(pcr.get("volume_ratio")),
            "oi_ratio": n1(pcr.get("oi_ratio")),
        },
        "walls": {
            "call_strike": n0((walls.get("call") or {}).get("strike")),
            "call_oi": n0((walls.get("call") or {}).get("oi")),
            "put_strike": n0((walls.get("put") or {}).get("strike")),
            "put_oi": n0((walls.get("put") or {}).get("oi")),
        },
        "pivots": {k: n0(v) for k, v in piv.items()} or {k: "—" for k in ("P", "R1", "R2", "R3", "S1", "S2", "S3")},
        "vol": None,
        "vol_need": 15, "hist_len": len(history),
        "chart": chart,
        "radar_json": json.dumps(radar, ensure_ascii=False),
        "chart_json": json.dumps(chart, ensure_ascii=False),
        "ai": None,
        "verify": None,
    }
    if vol_main and vol_tail:
        d["vol"] = {
            "main_low": n0(vol_main["low"]), "main_high": n0(vol_main["high"]),
            "tail_low": n0(vol_tail["low"]), "tail_high": n0(vol_tail["high"]),
            "atr": n0(atr_val), "n": 14,
        }

    # Phase 2:day-close 收盤時存一份「預測快照」,給隔天 06:30 next-day 回頭核對用。
    # next-day 抓到的仍是同一個交易日 date_date 的資料(夜盤掛在日盤那一天,直到
    # 05:00 收盤才算完整),所以用同一組 data_date 存/取即可,不用往前找一天。
    if session == "day-close":
        save_prediction(data_date, {
            "close": tx["close"],
            "sentiment_overall": senti["overall"],
            "sentiment_label": senti["label"],
            "vol_main": vol_main,
            "vol_tail": vol_tail,
        })
    if session == "next-day":
        d["verify"] = build_verify(tx, load_prediction(data_date))

    return d


# --------------------------------------------------------------------------- #
# Phase 2:收盤後驗證 —— day-close 存預測快照,next-day 回頭比對夜盤實際結果
# --------------------------------------------------------------------------- #
def save_prediction(data_date: str, pred: dict) -> None:
    os.makedirs(PRED_DIR, exist_ok=True)
    with open(os.path.join(PRED_DIR, f"{data_date}.json"), "w", encoding="utf-8") as f:
        json.dump(pred, f, ensure_ascii=False)


def load_prediction(data_date: str) -> dict | None:
    path = os.path.join(PRED_DIR, f"{data_date}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001
        print(f"[warn] 讀取預測快照 {path} 失敗:{exc}", file=sys.stderr)
        return None


def build_verify(tx: dict, pred: dict | None) -> dict:
    """比對 day-close 當時存的預測快照 vs 夜盤(盤後)實際結果。"""
    aft_last, aft_high, aft_low = tx.get("aft_last"), tx.get("aft_high"), tx.get("aft_low")
    aft_change = tx.get("aft_change") or 0

    if aft_last is None or aft_high is None or aft_low is None:
        return {
            "path": "夜盤尚無完整資料(可能休市或 TAIFEX 尚未更新)",
            "range": "—",
            "hit": "—",
        }

    path = f"開 {n0(tx.get('aft_open'))} → 高 {n0(aft_high)} → 低 {n0(aft_low)} → 收 {n0(aft_last)}"
    rng = f"{n0(aft_low)} – {n0(aft_high)}（寬 {n0(aft_high - aft_low)} 點）"

    if not pred:
        return {"path": path, "range": rng, "hit": "找不到前一份日盤收後的預測快照,無法核對方向命中"}

    overall = pred.get("sentiment_overall", 50)
    pred_dir = "多" if overall >= 55 else ("空" if overall <= 45 else "中性")
    actual_dir = "漲" if aft_change > 0 else ("跌" if aft_change < 0 else "平")

    if pred_dir == "中性":
        hit = f"預測中性(規則式分數 {overall}%),不判定方向命中；夜盤實際{actual_dir}"
    else:
        matched = (pred_dir == "多" and aft_change > 0) or (pred_dir == "空" and aft_change < 0)
        hit = f"{'✅ 命中' if matched else '❌ 未命中'}(預測偏{pred_dir}，夜盤實際{actual_dir})"

    vol_main = pred.get("vol_main")
    if vol_main:
        in_band = vol_main["low"] <= aft_last <= vol_main["high"]
        hit += f"；{'落在' if in_band else '突破'} ATR 主區間（{n0(vol_main['low'])}–{n0(vol_main['high'])}）"

    return {"path": path, "range": rng, "hit": hit}


def write_reports_index() -> None:
    files = sorted(
        (os.path.basename(p) for p in glob.glob(os.path.join(REPORTS_DIR, "*.html"))
         if os.path.basename(p) != "index.html"),
        reverse=True,
    )
    items = "\n".join(f'  <li><a href="{f}">{f[:-5]}</a></li>' for f in files)
    html = (
        '<!DOCTYPE html><html lang="zh-Hant-TW"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>台指期報告存檔</title>"
        "<style>body{background:#0f1419;color:#e6edf3;font-family:-apple-system,'Segoe UI',"
        "'Noto Sans TC',sans-serif;max-width:640px;margin:0 auto;padding:32px 18px}"
        "a{color:#4a9eff}h1{font-size:18px}li{margin:6px 0}</style></head><body>"
        "<h1>台指期報告存檔</h1><p><a href=\"../index.html\">← 回最新一份</a></p><ul>\n"
        f"{items}\n</ul></body></html>\n"
    )
    with open(os.path.join(REPORTS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=list(SESSIONS), default="day-close")
    args = ap.parse_args()

    env = Environment(
        loader=FileSystemLoader(os.path.join(ROOT, "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    d = build_context(args.session)
    page = env.get_template("report.html.j2").render(d=d)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(REPORTS_DIR, f"{d['data_date']}.html"), "w", encoding="utf-8") as f:
        f.write(page)
    write_reports_index()

    if d["stale"]:
        print(f"[warn] 期交所 OpenAPI 最新僅到 {d['stale']['have']},預期 {d['stale']['expected']}"
              f"(非休市則為上游更新延遲;稍後補跑的排程會自動接住)", file=sys.stderr)
    print(f"[ok] 產生報告 {d['data_date']} ({args.session}),歷史 {d['hist_len']} 天")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

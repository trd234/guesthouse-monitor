#!/usr/bin/env python3
"""
共用施設 キャンセル空き監視・自動予約（シンプル版）

  ・翌日〜45日後（先着予約の期間）の空きを確認
  ・土日祝に空き → 自動予約（夜枠優先、両方空きなら両方）＋ LINE通知
  ・平日に空き   → LINE通知のみ（同じ枠は24時間に1回まで）
  ・cron-job.org から5分おきに起動。1回の実行でチェック1回
    （土日祝は毎回、平日は1時間に1回だけ確認）

ブラウザ操作は miwa_auto_lottery.py の関数を共用する（同じフォルダに置くこと）。
環境変数 MIWA_DRY_RUN=1 のときは予約を確定しない。
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jpholiday
from playwright.sync_api import sync_playwright

import miwa_auto_lottery as L

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
DAYS_AHEAD = int(os.environ.get("MIWA_MONITOR_DAYS", "45"))
# cron-job.org から5分おきに起動される前提で、1回の実行ではチェック1回だけ
LOOP_COUNT = int(os.environ.get("MIWA_MONITOR_LOOPS", "1"))
LOOP_INTERVAL_SEC = int(os.environ.get("MIWA_MONITOR_INTERVAL", "60"))
# 平日（通知のみ）のチェック間隔（分）。土日祝は毎回チェック
WEEKDAY_EVERY = timedelta(minutes=int(os.environ.get("MIWA_WEEKDAY_EVERY_MIN", "60")))
COOLDOWN = timedelta(hours=int(os.environ.get("MIWA_NOTIFY_COOLDOWN_HOURS", "24")))
DRY_RUN = L.DRY_RUN

STATE_FILE = "miwa_state.json"
JST = timezone(timedelta(hours=9))
FACILITY_URL = f"{L.BASE_URL}/facilities/{L.FACILITY_ID}"

SLOTS = [L.SLOTS[1], L.SLOTS[0]]  # 夜枠 → 昼枠 の優先順（人数などは抽選と共通）

DONE_TEXTS = ("受付けました", "予約が完了", "予約しました", "予約を確定", "抽選待ち")

log = L.log


def is_holiday(d) -> bool:
    return d.weekday() >= 5 or jpholiday.is_holiday(d)


def slot_key(d, slot) -> str:
    return f"{d.isoformat()}_{slot['start'].replace(':', '')}"  # 例 2026-12-28_1700


# ------------------------------------------------------------
# 状態ファイル（予約済み・通知済みの記録）
# ------------------------------------------------------------
def load_state() -> dict:
    try:
        st = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("booked", [])
    st.setdefault("notified", {})
    if isinstance(st["notified"], list):  # 旧形式からの移行
        now = datetime.now(JST).isoformat()
        st["notified"] = {k: now for k in st["notified"]}
    return st


def save_state(st: dict):
    today = datetime.now(JST).date().isoformat()
    st["notified"] = {k: v for k, v in st["notified"].items() if k[:10] >= today}
    st["booked"] = [k for k in st["booked"] if k[:10] >= today]
    Path(STATE_FILE).write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------
# LINE通知
# ------------------------------------------------------------
def send_line(text: str):
    if DRY_RUN:
        log(f"  （DRY_RUN のためLINE通知はしません）\n{text}")
        return
    if not LINE_TOKEN:
        log("  ⚠️ LINE_CHANNEL_ACCESS_TOKEN が未設定のため通知できません")
        return
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=json.dumps({"messages": [{"type": "text", "text": text}]}).encode("utf-8"),
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        log("  ✅ LINE通知を送信")
    except Exception as e:
        log(f"  ❌ LINE通知失敗: {e}")


def notify_once(st: dict, key: str, text: str):
    """同じ key の通知は COOLDOWN の間は1回だけ送る。"""
    now = datetime.now(JST)
    last = st["notified"].get(key)
    try:
        if last and now - datetime.fromisoformat(last) < COOLDOWN:
            return
    except ValueError:
        pass
    st["notified"][key] = now.isoformat()
    send_line(text)


# ------------------------------------------------------------
# 空き確認
# ------------------------------------------------------------
def free_starts(page, d) -> set:
    """対象日に予約できる開始時刻（'11:00' / '17:00'）を返す。"""
    try:
        if not L.select_date(page, d, wait_tries=4):
            return set()
    except Exception as e:  # 選べない日（予約不可日など）は空きなし扱い
        log(f"    {d} 確認スキップ: {e}")
        return set()
    iso = d.isoformat()
    values = page.locator(f"{L.SEL_START} option").evaluate_all("os => os.map(o => o.value)")
    starts = {v[11:16] for v in values if v.startswith(iso)}

    # 精度向上: タイムラインの枠表示（title 例 '2026/12/28 17:00 ～ … 予約可'）で
    # 「予約不可」「利用不可」と出ている枠は除外する
    titles = page.locator('[wire\\:key^="slot-"]').evaluate_all(
        "els => els.map(e => (e.getAttribute('title') || '').trim())")
    ds = d.strftime("%Y/%m/%d")
    for t in titles:
        m = re.match(rf"{ds}\s+(\d\d:\d\d)", t)
        if m and t.split()[-1] in ("予約不可", "利用不可"):
            starts.discard(m.group(1))
    return starts & {s["start"] for s in SLOTS}


# ------------------------------------------------------------
# 予約（先着）
# ------------------------------------------------------------
def finalize(page, tag: str) -> str:
    """確認ページで規約に同意し、予約を1回だけ送信する。"""
    agree = page.locator("#isAgree")
    L.lw(page, lambda: agree.check(force=True))
    if DRY_RUN:
        L.save(page, f"{tag}_dry")
        return "dry"
    L.lw(page, page.locator('form[wire\\:submit="reserve"] button[type="submit"]').click,
         timeout=20000)
    result = "failed"
    for _ in range(30):
        text = L.body_text(page)
        if any(k in text for k in DONE_TEXTS) or re.search(r"/reserves/\d+", page.url):
            result = "booked"
            break
        if any(k in text for k in L.DUP_TEXTS):
            result = "already"
            break
        page.wait_for_timeout(1000)
    L.save(page, f"{tag}_{result}")
    return result


def book(page, d, slot) -> str:
    """1枠を予約。戻り値 booked / already / dry / gone（直前に埋まった）/ failed。"""
    tag = f"mon_{slot_key(d, slot)}"
    for attempt in (1, 2):
        try:
            L.select_date(page, d)
            if L.select_start(page, d, slot["start"]) != "ok":
                return "gone"
            L.set_options(page, slot["options"])
            if L.go_confirm(page):
                return finalize(page, tag)   # 送信後は二重予約を防ぐため再試行しない
            if any(k in L.body_text(page) for k in L.DUP_TEXTS):
                return "already"
            log(f"    ⚠️ 確認ページに進めません（{attempt}/2）: {L.error_texts(page)}")
            L.save(page, f"{tag}_confirm_failed")
        except Exception as e:
            log(f"    ⚠️ エラー（{attempt}/2）: {e}")
            L.save(page, f"{tag}_error")
    return "failed"


# ------------------------------------------------------------
# 1回分のチェック
# ------------------------------------------------------------
def weekday_check_due(st: dict) -> bool:
    """前回の平日チェックから WEEKDAY_EVERY 以上たっていれば True。"""
    last = st.get("weekday_checked")
    try:
        return not last or datetime.now(JST) - datetime.fromisoformat(last) >= WEEKDAY_EVERY
    except ValueError:
        return True


def run_once(page, st: dict, first: bool):
    now = datetime.now(JST)
    today = now.date()
    days = [today + timedelta(days=i) for i in range(1, DAYS_AHEAD + 1)]
    with_weekdays = first and weekday_check_due(st)
    if with_weekdays:
        st["weekday_checked"] = now.isoformat()
        log("  （今回は平日も確認します）")
    targets = [d for d in days if with_weekdays or is_holiday(d)]

    found = 0
    t_loop = time.time()
    for n, d in enumerate(targets, 1):
        wd = "月火水木金土日"[d.weekday()]
        date_label = f"{d.isoformat()}（{wd}）"
        t0 = time.time()
        starts = free_starts(page, d)
        took = f"{time.time() - t0:.1f}秒"
        if not starts:
            log(f"  [{n}/{len(targets)}] {date_label} 空きなし（{took}）")
            continue
        found += 1
        slots = [s for s in SLOTS if s["start"] in starts]
        log(f"  [{n}/{len(targets)}] 🟢 {date_label} 空き: {'・'.join(s['name'] for s in slots)}（{took}）")

        if not is_holiday(d):
            for s in slots:
                notify_once(st, slot_key(d, s),
                            f"🟢【共用施設 空き検知】\n{date_label} {s['name']} に空きが出ました"
                            f"（平日のため自動予約なし）\n\n手動で予約する\n{FACILITY_URL}")
            continue

        for s in slots:
            key = slot_key(d, s)
            if key in st["booked"]:
                log(f"    [{s['name']}] 予約済みとして記録済み → スキップ")
                continue
            r = book(page, d, s)
            log(f"    [{s['name']}] 結果: {r}")
            if r in ("booked", "already"):
                st["booked"].append(key)
                if r == "booked":
                    send_line(f"🏨【共用施設 空き自動予約 完了】\n{date_label} {s['name']} を予約しました！"
                              f"\n\n予約状況の確認\n{L.BASE_URL}/reserves")
            elif r == "failed":
                notify_once(st, key + "_manual",
                            f"🟠【共用施設 空き検知・要手動確認】\n{date_label} {s['name']} に空きがありますが、"
                            f"自動予約できませんでした。お早めに手動でご確認ください。\n\n{FACILITY_URL}")
    log(f"  チェック {len(targets)}日 ／ 空き {found}日 ／ 所要 {time.time() - t_loop:.0f}秒")


def main() -> int:
    if not all([L.BASE_URL, L.FACILITY_ID, L.USER_ID, L.PASSWORD]):
        log("⚠️ GitHub Secrets（MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE / MIWA_USER_ID / MIWA_PASSWORD）を確認してください")
        return 1

    log("=" * 50)
    log("共用施設 空き監視・自動予約" + ("（DRY_RUN: 予約しない安全モード）" if DRY_RUN else "（本番）"))
    log(f"監視期間: 翌日〜{DAYS_AHEAD}日後（土日祝は毎回・平日は{WEEKDAY_EVERY.seconds // 60}分おき）")
    log("=" * 50)

    st = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo")
        page.set_default_timeout(20000)
        try:
            L.login(page)
        except Exception as e:
            log(f"⚠️ {e}")
            browser.close()
            return 1

        for i in range(LOOP_COUNT):
            log(f"\n--- チェック {i + 1}/{LOOP_COUNT}（{datetime.now(JST):%H:%M:%S} JST）---")
            try:
                run_once(page, st, first=(i == 0))
            except Exception as e:
                log(f"  ⚠️ チェック中エラー: {e}")
                L.save(page, f"loop{i + 1}_error")
            save_state(st)
            if i < LOOP_COUNT - 1:
                time.sleep(LOOP_INTERVAL_SEC)
        browser.close()

    log("\n全チェック完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())

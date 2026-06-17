#!/usr/bin/env python3
"""
共用施設予約サイト キャンセル空き自動予約スクリプト（Playwright版）

サイトが「タイムラインをクリックして枠を選ぶ」JS中心UI（Livewire v3）に刷新され、
旧来のHTTP直叩きでは空き検知も予約もできなくなった。本スクリプトは実ブラウザ
（Chromium）を操作する。ログイン〜予約のフローは抽選スクリプト
（miwa_auto_lottery.py）の実績ある関数を再利用する。

  ・各日を選択して「予約可能な開始時刻（11:00/17:00）」が出るかで空きを判定
  ・土日祝に空きを検知 → 自動予約（夜枠優先、両方空きなら両方予約）
  ・平日に空きを検知   → LINE通知のみ（自動予約なし）
  ・GitHub Actions の cron（最短5分）で起動し、1回の実行内で数回チェック

安全装置: MIWA_DRY_RUN=1（手動実行の既定）のときは予約を確定しない。
"""

import os
import json
import time
import re
from pathlib import Path
from datetime import datetime, date, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    import jpholiday
except ImportError:
    jpholiday = None

# 抽選スクリプトの実績あるブラウザ操作ヘルパーを再利用
import miwa_auto_lottery as L

# ============================================================
# 設定
# ============================================================
BASE_URL = os.environ.get("MIWA_BASE_URL", "").rstrip("/")
FACILITY_ID = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")
USER_ID = os.environ.get("MIWA_USER_ID", "")
PASSWORD = os.environ.get("MIWA_PASSWORD", "")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
FACILITY_URL = f"{BASE_URL}/facilities/{FACILITY_ID}"

# 先着（キャンセル拾い）の監視範囲：翌日〜N日後（先着受付＝抽選後〜利用日前日）
DAYS_AHEAD = int(os.environ.get("MIWA_MONITOR_DAYS", "45"))
# 1回の実行内でのチェック回数と間隔
LOOP_COUNT = int(os.environ.get("MIWA_MONITOR_LOOPS", "3"))
LOOP_INTERVAL_SEC = int(os.environ.get("MIWA_MONITOR_INTERVAL", "60"))

# 安全モード（1=予約しない）。cronは0、手動実行は既定1。
DRY_RUN = os.environ.get("MIWA_DRY_RUN", "1") != "0"

STATE_FILE = "miwa_state.json"
JST = timezone(timedelta(hours=9))

# 予約する枠（夜枠を優先するため夜→昼の順）。option値は診断で確認済み。
#   46 = 21時までの利用人数 / 49 = 21時以降の利用人数 / 61 = 夜枠用チェックイン時間
SLOTS_PRIORITY = [
    {"start": "17:00", "time": "1700", "name": "夜枠（17:00〜翌9:00）",
     "options": {46: 8, 49: 4, 61: 1}},
    {"start": "11:00", "time": "1100", "name": "昼枠（11:00〜15:00）",
     "options": {46: 8, 49: 0, 61: 0}},
]

# 申込/予約の完了を示す文言（先着=予約完了 / 抽選=抽選待ち）
DONE_KEYWORDS = ["受付けました", "抽選待ち", "予約を受け付け", "予約しました",
                 "予約が完了", "申込が完了", "予約のキャンセル", "抽選予約のキャンセル"]


def log(msg=""):
    print(msg, flush=True)


# ============================================================
# 土日祝判定 / 状態の読み書き
# ============================================================
def is_weekend_or_holiday(d: date) -> bool:
    if d.weekday() >= 5:
        return True
    if jpholiday and jpholiday.is_holiday(d):
        return True
    return False


def weekday_ja(d: date) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
        except (json.JSONDecodeError, OSError):
            st = {}
    else:
        st = {}
    st.setdefault("booked", [])      # 予約に成功した "YYYY-MM-DD_HHMM"
    st.setdefault("notified", [])    # 空き通知済みの "YYYY-MM-DD_HHMM"
    return st


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# LINE通知
# ============================================================
def send_line(message: str):
    if not LINE_TOKEN:
        log("  ⚠️ LINE_CHANNEL_ACCESS_TOKEN が未設定です")
        return
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if resp.status_code == 200:
            log("  ✅ LINE通知を送信しました")
        else:
            log(f"  ❌ LINE通知失敗: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"  ❌ LINE通知エラー: {e}")


def notify_booked(date_str: str, names: list):
    body = "\n".join(f"  ・{n}" for n in names)
    send_line(
        f"🏨【共用施設 空き自動予約 完了】\n{date_str} の予約を確保しました！\n\n"
        f"{body}\n\n予約状況の確認\n{BASE_URL}/reserves"
    )


def notify_partial(date_str: str, names: list):
    body = "\n".join(f"  ・{n}" for n in names)
    send_line(
        f"🟠【共用施設 空き検知・要手動確認】\n{date_str} に空きを検知し予約を進めましたが、"
        f"自動で確定できませんでした。お早めに手動でご確認ください。\n\n"
        f"{body}\n\n{FACILITY_URL}"
    )


def notify_vacancy(date_str: str, names: list):
    body = "\n".join(f"  ・{n}" for n in names)
    send_line(
        f"🟢【共用施設 空き検知】\n{date_str} に空きが出ました！（平日のため自動予約なし）\n\n"
        f"{body}\n\n手動で予約する\n{FACILITY_URL}"
    )


# ============================================================
# 空き検知
# ============================================================
def get_candidate_dates() -> list:
    """翌日から DAYS_AHEAD 日後までの date を列挙する。"""
    today = datetime.now(JST).date()
    return [today + timedelta(days=i) for i in range(1, DAYS_AHEAD + 1)]


def available_times(page, target: date) -> set:
    """対象日を選択し、予約可能な開始時刻の集合（'11:00'/'17:00'）を返す。"""
    try:
        L.open_facility(page)
        if not L.open_calendar(page):
            return set()
        if not L.goto_month(page, target.strftime("%Y-%m")):
            return set()
        L.click_date(page, target)  # タイムラインが切り替わらなくても options を見る
    except PWTimeout:
        return set()
    except Exception as e:
        log(f"    ⚠️ {target} 空き確認エラー: {e}")
        return set()

    times = set()
    for o in L.get_select_options(page, "selectStartDateTime"):
        t = (o.get("text") or "").strip()
        if t in ("11:00", "17:00"):
            times.add(t)
    return times


# ============================================================
# 予約（先着）
# ============================================================
def _finalize(page, tag: str) -> bool:
    """確認ページで規約同意→予約確定し、完了を確認する。DRY_RUNなら確定しない。"""
    if DRY_RUN:
        log("    🟡 DRY_RUN のため確定しません（確認ページのスクショのみ）")
        L.shot(page, f"{tag}_confirm")
        return False

    # 利用規約に同意（チェックボックスをON）
    boxes = page.locator('input[type="checkbox"]')
    for i in range(boxes.count()):
        try:
            boxes.nth(i).check(force=True, timeout=3000)
        except Exception:
            pass
    page.evaluate(
        """() => document.querySelectorAll('input[type=checkbox]').forEach(b => {
            if (!b.checked) {
                b.checked = true;
                b.dispatchEvent(new Event('input', {bubbles: true}));
                b.dispatchEvent(new Event('change', {bubbles: true}));
            }
        })""")
    page.wait_for_timeout(600)

    # 確定フォーム（wire:submit=reserve）を1回だけ送信
    res = page.evaluate(
        """() => {
            const f = Array.from(document.querySelectorAll('form')).find(e =>
                e.getAttributeNames().some(n => n.startsWith('wire:submit')));
            if (!f) return 'no-form';
            if (f.requestSubmit) f.requestSubmit(); else
                f.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
            return 'submitted';
        }""")
    log(f"    予約確定 送信: {res}")

    done = duplicate = False
    body = ""
    for _ in range(12):
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except PWTimeout:
            pass
        page.wait_for_timeout(700)
        body = " ".join((page.evaluate("() => document.body.innerText") or "").split())
        url = page.url
        if any(k in body for k in DONE_KEYWORDS) or re.search(r"/reserves/\d+", url) \
                or ("reserve-confirm" not in url and "/facilities/" in url):
            done = True
            break
        if any(w in body for w in ("同じ時間に予約", "既に予約", "すでに予約")):
            duplicate = True
            break

    L.shot(page, f"{tag}_after_reserve")
    L.dump_html(page, f"{tag}_after_reserve")
    log(f"    確定後 URL={page.url}")
    if done:
        log("    ✅ 予約完了を確認")
        return True
    if duplicate:
        log("    ℹ️ 既に予約あり（予約済みとして扱う）")
        return True
    log(f"    ⚠️ 完了を確認できませんでした。画面テキスト（先頭600字）: {body[:600]}")
    return False


def book_slot(page, target: date, slot: dict) -> str:
    """1枠を予約する。戻り値: 'booked' / 'already' / 'partial' / 'failed'。"""
    tag = f"mon_{slot['time']}_{target.isoformat()}"
    log(f"    [{slot['name']}] 予約開始 (date={target})")

    outcome = "failed"
    for attempt in range(1, 4):
        try:
            L.open_facility(page)
            if not L.open_calendar(page):
                continue
            if not L.goto_month(page, target.strftime("%Y-%m")):
                continue
            if not L.click_date(page, target):
                continue
            L.select_slot(page, target, slot["start"])
            L.set_options(page, slot["options"])
            if L.click_confirm_button(page):
                outcome = "confirmed"
                break
            body = " ".join((page.evaluate("() => document.body.innerText") or "").split())
            if any(w in body for w in ("同じ時間に予約", "既に予約", "すでに予約",
                                       "予約済み", "申込済み")):
                outcome = "already"
                break
        except PWTimeout:
            log(f"    ⚠️ タイムアウト（試行 {attempt}/3）")
        log(f"    確認ページに進めず（試行 {attempt}/3）。やり直します")
        page.wait_for_timeout(1500)

    if outcome == "already":
        log("    ℹ️ その枠は既に予約済みです")
        return "already"
    if outcome != "confirmed":
        L.shot(page, f"{tag}_confirm_fail")
        return "failed"

    L.shot(page, f"{tag}_confirm_page")
    return "booked" if _finalize(page, tag) else "partial"


# ============================================================
# 1回分のチェック
# ============================================================
def run_once(page, state: dict, loop_index: int):
    booked = state["booked"]
    notified = state["notified"]

    all_days = get_candidate_dates()
    holidays = [d for d in all_days if is_weekend_or_holiday(d)]
    weekdays = [d for d in all_days if not is_weekend_or_holiday(d)]

    # 土日祝（自動予約対象）は毎回。平日（通知のみ）は負荷軽減のため初回ループだけ。
    targets = list(holidays)
    if loop_index == 0:
        targets += weekdays
    targets.sort()

    current_keys = set()
    found_days = 0
    for d in targets:
        date_str = d.isoformat()
        times = available_times(page, d)
        if not times:
            continue
        found_days += 1
        avail_slots = [s for s in SLOTS_PRIORITY if s["start"] in times]
        label = "・".join(s["name"] for s in avail_slots)
        log(f"  🟢 {date_str}（{weekday_ja(d)}）空き: {label}")
        for s in avail_slots:
            current_keys.add(f"{date_str}_{s['time']}")

        if is_weekend_or_holiday(d):
            # 土日祝 → 自動予約（夜枠優先で順に。両方空きなら両方）
            booked_names, partial_names = [], []
            for s in avail_slots:
                key = f"{date_str}_{s['time']}"
                if key in booked:
                    log(f"    [{s['name']}] 予約済みのためスキップ")
                    continue
                result = book_slot(page, d, s)
                if result in ("booked", "already"):
                    booked.append(key)
                    booked_names.append(s["name"])
                elif result == "partial":
                    partial_names.append(s["name"])
            if booked_names and not DRY_RUN:
                notify_booked(date_str, booked_names)
            if partial_names and not DRY_RUN:
                notify_partial(date_str, partial_names)
        else:
            # 平日 → 新規の空きだけ通知
            new_names = []
            for s in avail_slots:
                key = f"{date_str}_{s['time']}"
                if key not in notified:
                    notified.append(key)
                    new_names.append(s["name"])
            if new_names:
                notify_vacancy(date_str, new_names)

    log(f"  チェック {len(targets)}日（うち土日祝 {len(holidays)}日）／空き {found_days}日")

    # 空きが消えた通知済みキーは解除（次の空きで再通知できるように）
    state["notified"] = [k for k in notified if k in current_keys]
    state["booked"] = booked


# ============================================================
# メイン
# ============================================================
def main():
    if not BASE_URL or not FACILITY_ID:
        log("⚠️ MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE が未設定です")
        return
    if not USER_ID or not PASSWORD:
        log("⚠️ MIWA_USER_ID / MIWA_PASSWORD が未設定です")
        return

    days = get_candidate_dates()
    log("=" * 50)
    log("共用施設 空き自動予約" + ("（DRY_RUN: 予約しない安全モード）" if DRY_RUN else ""))
    log(f"監視期間: {days[0].isoformat()} 〜 {days[-1].isoformat()}（{LOOP_COUNT}回チェック）")
    log("=" * 50)

    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
        )
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            L.login(page)
        except Exception as e:
            log(f"⚠️ ログイン失敗: {e}")
            browser.close()
            return

        for i in range(LOOP_COUNT):
            now = datetime.now(JST).strftime("%H:%M:%S")
            log(f"\n--- チェック {i + 1}/{LOOP_COUNT}  ({now} JST) ---")
            try:
                run_once(page, state, i)
            except Exception as e:
                log(f"  ⚠️ チェック中エラー: {e}")
            save_state(state)
            if i < LOOP_COUNT - 1:
                log(f"  {LOOP_INTERVAL_SEC}秒後に再チェック...")
                time.sleep(LOOP_INTERVAL_SEC)

        browser.close()

    log("\n" + "=" * 50)
    log("全チェック完了")
    log("=" * 50)


if __name__ == "__main__":
    main()

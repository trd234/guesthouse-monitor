#!/usr/bin/env python3
"""
共用施設予約サイト 抽選自動申込スクリプト（Playwright/ブラウザ自動操作版）

サイトが「タイムラインをクリックして枠を選ぶ」JS中心のUI（Livewire v3）に作り替え
られたため、requests でのHTTP直叩きでは枠が読み込めなくなった。本スクリプトは実際の
ブラウザ（Chromium）を人間と同じように操作して申込む。

申込フロー（実画面の操作順）:
  1. ログイン
  2. 施設ページを開く（BASE_URL/facilities/{id}）
  3. カレンダーを開いて対象月へ移動 → 対象日をクリック（＝枠が読み込まれる）
  4. タイムラインから該当枠（昼=11:00 / 夜=17:00）をクリックして選択（selectSlot）
  5. 人数オプション（option 46/49/61）を設定
  6. 「予約内容を確認する」→ 確認ページ
  7. 確認ページで確定（抽選申込）→ /reserves/{id} 到達で完了

安全装置:
  - 環境変数 MIWA_DRY_RUN=1（既定）のときは、確認ページの手前で止めて
    「予約は確定しない」。各ステップのスクリーンショットと画面HTMLを artifacts/ に保存する。
    初回はこの安全モードで動作確認し、画面が想定どおりだと確認できたら
    MIWA_DRY_RUN=0 にして本番稼働させる。
"""

import os
import re
import sys
import json
import traceback
from datetime import datetime, date, timedelta

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    import jpholiday
except ImportError:
    jpholiday = None
    print("⚠️ jpholiday 未インストール（祝日判定なし、土日のみ対象）", flush=True)

# ============================================================
# 設定（マンション固有の値は環境変数から取得）
# ============================================================
BASE_URL = os.environ.get("MIWA_BASE_URL", "").rstrip("/")
FACILITY_ID = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")
USER_ID = os.environ.get("MIWA_USER_ID", "")
PASSWORD = os.environ.get("MIWA_PASSWORD", "")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

LOGIN_URL = f"{BASE_URL}/login"
FACILITY_URL = f"{BASE_URL}/facilities/{FACILITY_ID}"

# DRY_RUN: 既定は安全モード（確定しない）。本番稼働時のみ MIWA_DRY_RUN=0 を設定。
DRY_RUN = os.environ.get("MIWA_DRY_RUN", "1") != "0"

# 60日後を対象（抽選受付開始日）
DAYS_AHEAD = 60

ART_DIR = "artifacts"

# 申込する枠（option_id は診断で確認済み）
#   46 = 21時までの利用人数（0〜8）
#   49 = 21時以降の利用人数（0〜4）
#   61 = 夜枠用チェックイン予定時間（0=未選択 / 1=17:00〜19:00 ...）
SLOTS = [
    {"name": "昼枠（11:00〜15:00）", "start": "11:00", "options": {46: 8, 49: 0, 61: 0}},
    {"name": "夜枠（17:00〜翌9:00）", "start": "17:00", "options": {46: 8, 49: 4, 61: 1}},
]


def log(msg=""):
    print(msg, flush=True)


def shot(page, name):
    """スクリーンショットを artifacts/ に保存（失敗しても処理は止めない）。"""
    try:
        os.makedirs(ART_DIR, exist_ok=True)
        path = os.path.join(ART_DIR, f"{name}.png")
        page.screenshot(path=path, full_page=True)
        log(f"    📷 スクショ保存: {path}")
    except Exception as e:
        log(f"    （スクショ失敗: {e}）")


def dump_html(page, name):
    """画面HTMLを artifacts/ に保存（デバッグ用）。"""
    try:
        os.makedirs(ART_DIR, exist_ok=True)
        path = os.path.join(ART_DIR, f"{name}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page.content())
        log(f"    📝 HTML保存: {path}")
    except Exception as e:
        log(f"    （HTML保存失敗: {e}）")


# ============================================================
# 土日祝判定
# ============================================================
def is_weekend_or_holiday(d: date) -> bool:
    if d.weekday() >= 5:
        return True
    if jpholiday and jpholiday.is_holiday(d):
        return True
    return False


# ============================================================
# Livewire スナップショット読み取り（枠の状態確認用）
# ============================================================
def read_snapshots(page) -> list:
    """画面内の全 wire:snapshot を取り出して [(name, data), ...] を返す。"""
    raw_list = page.evaluate(
        "() => Array.from(document.querySelectorAll('*'))"
        ".filter(e => e.hasAttribute('wire:snapshot'))"
        ".map(e => e.getAttribute('wire:snapshot'))"
    )
    out = []
    for raw in raw_list or []:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        out.append((d.get("memo", {}).get("name", ""), d.get("data", {})))
    return out


def _unwrap_arr(v):
    if isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict) and "s" in v[1]:
        return v[0]
    return v


def _status_str(rs):
    rs2 = _unwrap_arr(rs)
    if isinstance(rs2, str):
        return rs2
    if isinstance(rs, list) and rs and isinstance(rs[0], str):
        return rs[0]
    return str(rs)


def get_timeline_slots(page) -> list:
    """reserve-timeline の computed から枠一覧を取り出す。
    各要素 = {index, start, end, status, reserveKbn}。"""
    slots = []
    for name, data in read_snapshots(page):
        if "reserve-timeline" not in name:
            continue
        computed = _unwrap_arr(data.get("computed", []))
        for raw_slot in (computed if isinstance(computed, list) else []):
            slot = _unwrap_arr(raw_slot)
            if not isinstance(slot, dict):
                continue
            slots.append({
                "index": slot.get("index"),
                "start": slot.get("start"),
                "end": slot.get("end"),
                "status": _status_str(slot.get("reservableStatus")),
                "reserveKbn": slot.get("reserveKbn"),
            })
    return slots


# ============================================================
# ブラウザ操作の各ステップ
# ============================================================
def login(page):
    log("ログイン中…")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    # メール/パスワード欄（name優先、無ければ type で）
    for sel in ['input[name="email"]', 'input[type="email"]', 'input[type="text"]']:
        if page.locator(sel).count():
            page.fill(sel, USER_ID)
            break
    for sel in ['input[name="password"]', 'input[type="password"]']:
        if page.locator(sel).count():
            page.fill(sel, PASSWORD)
            break
    # 送信
    for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("ログイン")']:
        if page.locator(sel).count():
            page.locator(sel).first.click()
            break
    page.wait_for_load_state("networkidle", timeout=30000)
    if "/login" in page.url:
        shot(page, "login_failed")
        raise RuntimeError("ログイン失敗（ID/パスワードをご確認ください）")
    log("ログイン成功")


def open_facility(page):
    page.goto(FACILITY_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_load_state("networkidle", timeout=30000)


def open_calendar(page):
    """日付表示ボタン（wire:click='toggle'）を押してカレンダーを開く。"""
    btn = page.locator("button", has_text=re.compile(r"\d{4}年\d{2}月\d{2}日"))
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(800)
        return True
    # フォールバック: toggle を持つボタン
    alt = page.locator('button[wire\\:click\\.prevent\\.stop="toggle"]')
    if alt.count():
        alt.first.click()
        page.wait_for_timeout(800)
        return True
    return False


def debug_calendar(page):
    """開いたカレンダーの構造をログに出力（月送りボタン・日付セルの形を確認するため）。"""
    snaps = read_snapshots(page)
    log(f"    [debug] snapshot数={len(snaps)} names={[n for n, _ in snaps]}")
    # reserve-calendar コンポーネントの outerHTML を取り出す
    outer = page.evaluate(
        """() => {
            const els = Array.from(document.querySelectorAll('*'))
                .filter(e => e.hasAttribute('wire:snapshot'));
            for (const e of els) {
                try {
                    const d = JSON.parse(e.getAttribute('wire:snapshot'));
                    if (((d.memo && d.memo.name) || '').includes('reserve-calendar'))
                        return e.outerHTML;
                } catch (x) {}
            }
            return '';
        }"""
    )
    if outer:
        snippet = " ".join(outer.split())[:6000]
        log("    [debug] reserve-calendar outerHTML（先頭6000字）↓")
        log(snippet)
    else:
        log("    [debug] reserve-calendar 要素が見つかりません")


def current_visible_month(page) -> str:
    """カレンダー(reserve-calendar)の visibleMonth を読む（例 '2026-06'）。"""
    for name, data in read_snapshots(page):
        if "reserve-calendar" in name:
            return data.get("visibleMonth", "")
    return ""


def goto_month(page, target_month: str, max_clicks: int = 14):
    """カレンダーを target_month（'YYYY-MM'）まで「次の月」ボタンで進める。"""
    for _ in range(max_clicks):
        cur = current_visible_month(page)
        log(f"    カレンダー表示月: {cur} / 目標: {target_month}")
        if cur == target_month:
            return True
        # 「次の月」ボタンの候補をいくつか試す
        moved = False
        for sel in [
            'button[aria-label*="次"]', 'button[aria-label*="next"]',
            '[wire\\:click*="next"]', '[wire\\:click*="Next"]',
            '[wire\\:click*="changeMonth"]',
        ]:
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.last.click()
                    page.wait_for_timeout(700)
                    moved = True
                    break
                except Exception:
                    continue
        if not moved:
            log("    ⚠️ 「次の月」ボタンが見つかりませんでした（カレンダーHTMLを保存）")
            dump_html(page, "calendar_no_nav")
            return False
    return current_visible_month(page) == target_month


def click_date(page, target: date) -> bool:
    """カレンダー上で対象日のセルをクリックする。"""
    day = target.day
    iso = target.strftime("%Y-%m-%d")
    # 候補: wire:click に日付が入っているセル / 日付テキストのセル
    candidates = [
        f'[wire\\:click*="{iso}"]',
        f'[wire\\:key*="{iso}"]',
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count():
            loc.first.click()
            page.wait_for_timeout(1200)
            return True
    # テキスト（日にち数字）でクリック。カレンダー内の該当セルを探す。
    cells = page.locator('[wire\\:key^="reserve-calendar-"] >> text=' + str(day))
    if cells.count():
        cells.first.click()
        page.wait_for_timeout(1200)
        return True
    log("    ⚠️ 対象日のセルが特定できませんでした（カレンダーHTMLを保存）")
    dump_html(page, "calendar_date_notfound")
    return False


def select_slot(page, start_label: str) -> bool:
    """タイムラインから start_label（'11:00'/'17:00'）の枠を選んでクリックする。"""
    slots = get_timeline_slots(page)
    log(f"    タイムライン枠: " + ", ".join(
        f"[{s['index']}]{s['start']}-{s['end']}({s['status']})" for s in slots) or "（枠なし）")
    # まず予約可かつ開始時刻一致の枠
    target = None
    for s in slots:
        if s["start"] == start_label and s["status"] == "reservable":
            target = s
            break
    if target is None:
        # 予約可が無い場合は時刻一致のみ（DRY_RUN時の挙動確認用）
        for s in slots:
            if s["start"] == start_label:
                target = s
                break
    if target is None or target["index"] is None:
        log(f"    ⚠️ {start_label} の枠が見つかりません")
        return False
    idx = target["index"]
    log(f"    枠 index={idx}（{target['start']}-{target['end']} / {target['status']}）を選択")
    loc = page.locator(f'[wire\\:key="slot-{idx}"]')
    if not loc.count():
        log(f"    ⚠️ slot-{idx} の要素が見つかりません")
        return False
    loc.first.click()
    page.wait_for_timeout(1000)
    return target["status"] == "reservable"


def set_options(page, options: dict):
    """人数オプションの select を設定する。"""
    for opt_id, value in options.items():
        sel = f'select[wire\\:model\\.change="selectOptionQuantities.{opt_id}"]'
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.select_option(str(value))
                page.wait_for_timeout(500)
                log(f"    option {opt_id} = {value} 設定")
            except Exception as e:
                log(f"    ⚠️ option {opt_id} 設定失敗: {e}")
        else:
            log(f"    ⚠️ option {opt_id} の select が見つかりません")


def click_confirm_button(page) -> bool:
    """「予約内容を確認する」を押して確認ページへ。"""
    for sel in ['button:has-text("予約内容を確認する")', 'button[type="submit"]']:
        loc = page.locator(sel)
        if loc.count():
            loc.first.click()
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(800)
            return True
    return False


def finalize_reservation(page) -> bool:
    """確認ページで確定操作を行い、/reserves/{id} 到達で True。
    DRY_RUN のときは押さずに False（未確定）。"""
    if DRY_RUN:
        log("    🟡 DRY_RUN のため確定はしません（確認ページのスクショ/HTMLのみ保存）")
        return False
    # 同意チェックがあれば入れる
    for sel in ['input[type="checkbox"]']:
        boxes = page.locator(sel)
        for i in range(boxes.count()):
            try:
                if not boxes.nth(i).is_checked():
                    boxes.nth(i).check()
            except Exception:
                pass
    # 確定ボタンの候補
    for sel in [
        'button:has-text("抽選を申し込む")', 'button:has-text("抽選申込")',
        'button:has-text("申し込む")', 'button:has-text("申込む")',
        'button:has-text("予約する")', 'button:has-text("確定")',
        'button[type="submit"]',
    ]:
        loc = page.locator(sel)
        if loc.count():
            log(f"    確定ボタン「{loc.first.inner_text().strip()}」を押下")
            loc.first.click()
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            if re.search(r"/reserves/\d+", page.url) or re.search(r"/reserves/\d+", page.content()):
                return True
    return bool(re.search(r"/reserves/\d+", page.url))


# ============================================================
# 1枠分の申込
# ============================================================
def apply_slot(page, target: date, slot: dict) -> str:
    """戻り値: 'applied' / 'partial'(未確定) / 'failed'。"""
    name = slot["name"]
    tag = "day" if slot["start"] == "11:00" else "night"
    log(f"\n  [{name}] 申込開始 (date={target})")

    open_facility(page)
    if not open_calendar(page):
        log("    ⚠️ カレンダーを開けませんでした")
        shot(page, f"{tag}_no_calendar")
        dump_html(page, f"{tag}_no_calendar")
        return "failed"

    shot(page, f"{tag}_1_calendar_open")
    dump_html(page, f"{tag}_1_calendar_open")
    debug_calendar(page)

    if not goto_month(page, target.strftime("%Y-%m")):
        log("    ⚠️ 対象月へ移動できませんでした")
        shot(page, f"{tag}_2_month_fail")
        return "failed"

    if not click_date(page, target):
        log("    ⚠️ 対象日を選択できませんでした")
        shot(page, f"{tag}_3_date_fail")
        return "failed"

    shot(page, f"{tag}_4_date_selected")

    reservable = select_slot(page, slot["start"])
    shot(page, f"{tag}_5_slot_selected")
    if not reservable:
        log("    ⚠️ 予約可能な枠ではありませんでした（受付期間外・満枠・既申込の可能性）")
        # DRY_RUN中は挙動確認のため続行、本番は中断
        if not DRY_RUN:
            return "failed"

    set_options(page, slot["options"])
    shot(page, f"{tag}_6_options_set")

    if not click_confirm_button(page):
        log("    ⚠️ 「予約内容を確認する」を押せませんでした")
        shot(page, f"{tag}_7_confirm_fail")
        return "failed"

    shot(page, f"{tag}_8_confirm_page")
    dump_html(page, f"{tag}_8_confirm_page")
    log(f"    確認ページURL: {page.url}")

    ok = finalize_reservation(page)
    if ok:
        shot(page, f"{tag}_9_done")
        log("    ✅ 申込確定完了！")
        return "applied"
    return "partial"


# ============================================================
# LINE通知
# ============================================================
def send_line(message: str):
    if not LINE_TOKEN:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
    except Exception as e:
        log(f"  ❌ LINE通知エラー: {e}")


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

    target = date.today() + timedelta(days=DAYS_AHEAD)
    weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][target.weekday()]

    log("=" * 50)
    log("共用施設 抽選自動申込" + ("（DRY_RUN: 予約しない安全モード）" if DRY_RUN else ""))
    log(f"対象日: {target}（{weekday_ja}）")
    log("=" * 50)

    if not is_weekend_or_holiday(target):
        log("→ 平日のためスキップ")
        return

    holiday_name = ""
    if jpholiday:
        h = jpholiday.is_holiday_name(target)
        if h:
            holiday_name = f"（{h}）"
    log(f"→ 土日祝{holiday_name}のため抽選申込を実行")

    results = []
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
            login(page)
        except Exception as e:
            log(f"⚠️ {e}")
            traceback.print_exc()
            browser.close()
            return

        for slot in SLOTS:
            try:
                outcome = apply_slot(page, target, slot)
            except PWTimeout as e:
                log(f"    ⚠️ タイムアウト: {e}")
                shot(page, f"timeout_{slot['start']}")
                outcome = "failed"
            except Exception as e:
                log(f"    ⚠️ エラー: {e}")
                traceback.print_exc()
                outcome = "failed"
            results.append((slot["name"], outcome))

        browser.close()

    log("\n" + "=" * 50)
    log("結果:" + ("（DRY_RUN: 確定はしていません）" if DRY_RUN else ""))
    label = {"applied": "✅ 申込完了", "partial": "🟠 要手動確認", "failed": "❌ 失敗"}
    lines = []
    for name, outcome in results:
        log(f"  {name}: {label.get(outcome, outcome)}")
        lines.append(f"  ・{name}: {label.get(outcome, outcome)}")
    log("=" * 50)

    # 本番モードで完了/要確認があれば通知（DRY_RUNでは通知しない）
    if not DRY_RUN and any(o in ("applied", "partial") for _, o in results):
        body = "\n".join(lines)
        send_line(
            f"🎫【共用施設 抽選自動申込】\n対象日: {target}（{weekday_ja}）\n\n{body}\n\n"
            f"申込状況の確認👇\n{BASE_URL}/reserves"
        )


if __name__ == "__main__":
    main()

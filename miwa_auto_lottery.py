#!/usr/bin/env python3
"""
共用施設 抽選自動申込（シンプル版）

流れ: ログイン → 施設ページ → 日付 → 開始時刻 → オプション「使用する」＋人数
      → 「予約内容を確認する」 → 利用規約に同意 → 「抽選予約する」 → 完了確認

環境変数:
  MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE / MIWA_USER_ID / MIWA_PASSWORD
  MIWA_DRY_RUN     : 1（既定）= 確認ページで止める / 0 = 本番で申込む
  MIWA_TARGET_DATE : YYYY-MM-DD を指定するとその日（曜日不問）。空なら60日後の土日祝のみ
"""

import os
import sys
from datetime import date, datetime, timedelta

import jpholiday
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = os.environ.get("MIWA_BASE_URL", "").rstrip("/")
FACILITY_ID = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")
USER_ID = os.environ.get("MIWA_USER_ID", "")
PASSWORD = os.environ.get("MIWA_PASSWORD", "")
DRY_RUN = os.environ.get("MIWA_DRY_RUN", "1") != "0"
TARGET_DATE = os.environ.get("MIWA_TARGET_DATE", "").strip()

DAYS_AHEAD = 60
ART_DIR = "artifacts"

# options: {オプションID: 数量}
#   46 = 21時までの利用人数 / 49 = 21時以降の利用人数 / 61 = 夜枠チェックイン予定時間(1=17:00~19:00)
SLOTS = [
    {"name": "昼枠（11:00〜15:00）", "tag": "day", "start": "11:00", "options": {46: 12}},
    {"name": "夜枠（17:00〜翌9:00）", "tag": "night", "start": "17:00", "options": {46: 12, 49: 4, 61: 1}},
]

DONE_TEXTS = ("抽選予約を受付けました", "抽選待ち")
DUP_TEXTS = ("同じ時間に予約", "既に予約", "すでに予約")

SEL_START = 'select[wire\\:model\\.change="selectStartDateTime"]'


def log(msg=""):
    print(msg, flush=True)


def save(page, name):
    """スクショとHTMLを artifacts/ に保存（失敗しても止めない）。"""
    try:
        os.makedirs(ART_DIR, exist_ok=True)
        page.screenshot(path=f"{ART_DIR}/{name}.png", full_page=True)
        with open(f"{ART_DIR}/{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log(f"    📷 保存: {ART_DIR}/{name}.png / .html")
    except Exception as e:
        log(f"    （保存失敗: {e}）")


def body_text(page):
    return " ".join((page.inner_text("body") or "").split())


def lw(page, action, timeout=10000):
    """Livewire の通信が発生する操作を実行し、その応答を待つ。"""
    try:
        with page.expect_response(lambda r: "/livewire/update" in r.url, timeout=timeout):
            action()
    except PWTimeout:
        pass
    page.wait_for_timeout(500)


def error_texts(page):
    """画面上の赤字エラーメッセージを集める。"""
    texts = page.locator(".text-error").all_inner_texts()
    return [t.strip() for t in texts if t.strip() and "必須" not in t]


# ------------------------------------------------------------
# 操作
# ------------------------------------------------------------
def login(page):
    log("ログイン中…")
    page.goto(f"{BASE_URL}/login")
    page.locator('input[name="email"], input[type="email"], input[type="text"]').first.fill(USER_ID)
    pw = page.locator('input[name="password"], input[type="password"]').first
    pw.fill(PASSWORD)
    # ログインボタン（type=submit が無い画面のため「ログイン」の文字でも探す）。無ければ Enter で送信
    btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("ログイン")')
    if btn.count():
        btn.first.click()
    else:
        pw.press("Enter")
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
    except PWTimeout:
        save(page, "login_failed")
        raise RuntimeError("ログイン失敗（ID/パスワードを確認してください）")
    log("ログイン成功")


def select_date(page, target: date) -> bool:
    """施設ページを開き、カレンダーで対象日を選ぶ。"""
    page.goto(f"{BASE_URL}/facilities/{FACILITY_ID}")
    toggle = page.locator('button[wire\\:click\\.prevent\\.stop="toggle"]').first
    toggle.wait_for()
    lw(page, toggle.click)

    month = target.strftime("%Y-%m")
    for _ in range(14):
        if page.locator(f'[wire\\:key="reserve-calendar-{month}"]').count():
            break
        lw(page, page.locator('[wire\\:click\\.prevent\\.stop="nextMonth"]').first.click)
    else:
        log(f"    ⚠️ カレンダーを {month} まで進められませんでした")
        return False

    # 日付セルは他の要素と重なっていることがあるため、クリックイベントを直接送る
    iso = target.isoformat()
    def click_day():
        res = page.evaluate(
            """(val) => {
                const el = Array.from(document.querySelectorAll('*')).find(e =>
                    e.getAttributeNames().some(n => n.startsWith('wire:click') && e.getAttribute(n) === val));
                if (!el) return false;
                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            }""", f"selectDate('{iso}')")
        if not res:
            raise RuntimeError(f"カレンダーに {iso} が見つかりません")
    lw(page, click_day)

    # 開始時刻の選択肢が対象日のものに切り替わるまで待つ
    for _ in range(20):
        values = page.locator(f"{SEL_START} option").evaluate_all("os => os.map(o => o.value)")
        if any(v.startswith(iso) for v in values):
            return True
        page.wait_for_timeout(500)
    return True  # 予約可の枠が無い日は選択肢が空のまま（呼び出し側で判定）


def select_start(page, target: date, start: str) -> str:
    """開始時刻を選ぶ。戻り値 'ok' / 'unavailable'（その枠が無い）。"""
    value = f"{target.isoformat()} {start}:00"
    sel = page.locator(SEL_START)
    values = sel.locator("option").evaluate_all("os => os.map(o => o.value)")
    log(f"    開始時刻の選択肢: {values}")
    if value not in values:
        return "unavailable"
    # 既に選択済みだと change が発生せずサーバーに反映されないため、一度別の値を経由する
    if sel.input_value() == value:
        other = next((v for v in values if v != value), None)
        if other:
            lw(page, lambda: sel.select_option(other))
    lw(page, lambda: sel.select_option(value))
    log(f"    開始時刻 = {sel.input_value()}")
    return "ok" if sel.input_value() == value else "failed"


def set_options(page, options: dict):
    """オプション『使用する』を選び、各数量を設定する。"""
    radio = page.locator('input[name="isUseOptions"][value="1"]')
    lw(page, lambda: radio.check(force=True))
    log(f"    オプション「使用する」= {radio.is_checked()}")
    for opt_id, qty in options.items():
        sel = page.locator(f'select[wire\\:model\\.change="selectOptionQuantities.{opt_id}"]')
        lw(page, lambda: sel.select_option(str(qty)))
        log(f"    option {opt_id} = {sel.input_value()}")


def go_confirm(page) -> bool:
    """「予約内容を確認する」を押して確認ページ（利用規約の同意欄）へ進む。"""
    btn = page.locator('form[wire\\:submit="next"] button[type="submit"]')
    lw(page, btn.click, timeout=20000)
    try:
        page.locator("#isAgree").wait_for(state="attached", timeout=20000)
        return True
    except PWTimeout:
        return False


def reserve(page) -> str:
    """利用規約に同意して「抽選予約する」を1回だけ押す。"""
    agree = page.locator("#isAgree")
    lw(page, lambda: agree.check(force=True))
    log(f"    利用規約に同意 = {agree.is_checked()}")
    if DRY_RUN:
        return "dry"

    lw(page, page.locator('form[wire\\:submit="reserve"] button[type="submit"]').click,
       timeout=20000)
    for _ in range(30):
        text = body_text(page)
        if any(t in text for t in DONE_TEXTS):
            return "applied"
        if any(t in text for t in DUP_TEXTS):
            return "already"
        page.wait_for_timeout(1000)
    return "failed"


def apply_slot(page, target: date, slot: dict) -> str:
    """1枠分を申込む。戻り値 applied / already / dry / unavailable / failed。"""
    tag = slot["tag"]
    if not select_date(page, target):
        save(page, f"{tag}_1_date_failed")
        return "failed"

    r = select_start(page, target, slot["start"])
    if r != "ok":
        save(page, f"{tag}_2_no_slot")
        return r

    set_options(page, slot["options"])
    save(page, f"{tag}_3_form")

    if not go_confirm(page):
        text = body_text(page)
        if any(t in text for t in DUP_TEXTS):
            return "already"
        log(f"    ⚠️ 確認ページに進めません。エラー表示: {error_texts(page)}")
        save(page, f"{tag}_4_confirm_failed")
        return "failed"
    log("    確認ページに到達")

    result = reserve(page)
    save(page, f"{tag}_5_{result}")
    return result


# ------------------------------------------------------------
# メイン
# ------------------------------------------------------------
def main() -> int:
    if not all([BASE_URL, FACILITY_ID, USER_ID, PASSWORD]):
        log("⚠️ GitHub Secrets（MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE / MIWA_USER_ID / MIWA_PASSWORD）を確認してください")
        return 1

    if TARGET_DATE:
        try:
            target = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()
        except ValueError:
            log(f"⚠️ 対象日 '{TARGET_DATE}' が不正です（YYYY-MM-DD で入力）")
            return 1
    else:
        target = date.today() + timedelta(days=DAYS_AHEAD)

    wd = "月火水木金土日"[target.weekday()]
    log("=" * 50)
    log("共用施設 抽選自動申込" + ("（DRY_RUN: 確定しない安全モード）" if DRY_RUN else "（本番）"))
    log(f"対象日: {target}（{wd}）" + ("　※手動指定" if TARGET_DATE else ""))
    log("=" * 50)

    if not TARGET_DATE and target.weekday() < 5 and not jpholiday.is_holiday(target):
        log("→ 平日のためスキップ")
        return 0

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo")
        page.set_default_timeout(20000)
        try:
            login(page)
        except Exception as e:
            log(f"⚠️ {e}")
            browser.close()
            return 1

        for slot in SLOTS:
            log(f"\n  [{slot['name']}]")
            outcome = "failed"
            for attempt in (1, 2):
                try:
                    outcome = apply_slot(page, target, slot)
                except Exception as e:
                    log(f"    ⚠️ エラー: {e}")
                    save(page, f"{slot['tag']}_error")
                    outcome = "failed"
                if outcome != "failed":
                    break
                log(f"    → 失敗（{attempt}/2）")
            results.append((slot["name"], outcome))
        browser.close()

    label = {
        "applied": "✅ 抽選申込完了",
        "already": "✅ 申込済み（重複のためスキップ）",
        "dry": "🟡 確認ページまで到達（DRY_RUNのため未確定）",
        "unavailable": "➖ 予約可の枠なし（対象外）",
        "failed": "❌ 失敗",
    }
    log("\n" + "=" * 50)
    log("結果:")
    for name, outcome in results:
        log(f"  {name}: {label[outcome]}")
    log("=" * 50)
    return 1 if any(o == "failed" for _, o in results) else 0


if __name__ == "__main__":
    sys.exit(main())

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
    """タイムラインの各枠（slot-N）を DOM の title 属性から読み取る。
    title 例: '2026/08/15 11:00 ～ 2026/08/15 15:00 予約可'
    各要素 = {index, date(YYYY/MM/DD), start(HH:MM), status, title}。"""
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll('*'))
            .filter(e => (e.getAttribute('wire:key') || '').startsWith('slot-'))
            .map(e => ({ key: e.getAttribute('wire:key'),
                         title: e.getAttribute('title') || '' }))"""
    )
    slots = []
    for item in raw or []:
        m = re.search(r"slot-(\d+)", item.get("key", ""))
        title = item.get("title", "")
        date_m = re.search(r"(\d{4}/\d{2}/\d{2})", title)
        time_m = re.search(r"\d{4}/\d{2}/\d{2}\s+(\d{2}:\d{2})", title)
        status = title.strip().split()[-1] if title.strip() else ""
        slots.append({
            "index": int(m.group(1)) if m else None,
            "date": date_m.group(1) if date_m else "",
            "start": time_m.group(1) if time_m else "",
            "status": status,
            "title": title,
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


def dump_region(page, anchor: str, before: int = 400, after: int = 14000, label=""):
    """実DOMの中から anchor 文字列の周辺HTMLをログに出す（セレクタ確認用）。"""
    body = page.content()
    idx = body.find(anchor)
    if idx < 0:
        log(f"    [debug] '{anchor}' は body 内に見つかりません")
        return
    window = " ".join(body[max(0, idx - before):idx + after].split())
    log(f"    [debug] '{anchor}' 周辺HTML{('（'+label+'）') if label else ''}↓")
    log(window)


def current_visible_month(page) -> str:
    """カレンダーの表示月を wire:key='reserve-calendar-YYYY-MM' から読む。"""
    keys = page.evaluate(
        """() => Array.from(document.querySelectorAll('*'))
            .map(e => e.getAttribute('wire:key'))
            .filter(k => k && k.startsWith('reserve-calendar-'))"""
    )
    for k in keys or []:
        m = re.search(r"(\d{4}-\d{2})", k)
        if m:
            return m.group(1)
    return ""


def goto_month(page, target_month: str, max_clicks: int = 14):
    """カレンダーを target_month（'YYYY-MM'）まで nextMonth で進める。"""
    for _ in range(max_clicks):
        cur = current_visible_month(page)
        log(f"    カレンダー表示月: {cur} / 目標: {target_month}")
        if cur == target_month:
            return True
        nxt = page.locator('[wire\\:click\\.prevent\\.stop="nextMonth"]')
        if not nxt.count():
            log("    ⚠️ nextMonth ボタンが見つかりません（カレンダーHTML↓）")
            dump_region(page, "reserve-calendar")
            return False
        try:
            nxt.first.click()
        except Exception as e:
            log(f"    ⚠️ nextMonth クリック失敗: {e}")
            return False
        page.wait_for_timeout(900)
    return current_visible_month(page) == target_month


def debug_calendar_clickables(page):
    """カレンダー内の『押せる要素（wire:click系）』と、その値・表示文字を一覧表示。
    日付セルが何のメソッドを呼ぶか（selectDate など）を特定するため。"""
    items = page.evaluate(
        """() => {
            const cal = Array.from(document.querySelectorAll('*'))
                .find(e => (e.getAttribute('wire:key') || '').startsWith('reserve-calendar-'));
            if (!cal) return [];
            return Array.from(cal.querySelectorAll('*'))
                .filter(e => e.getAttributeNames().some(n => n.startsWith('wire:click')))
                .map(e => {
                    const n = e.getAttributeNames().find(x => x.startsWith('wire:click'));
                    return { attr: n, val: e.getAttribute(n),
                             text: (e.textContent || '').trim().slice(0, 12) };
                }).slice(0, 80);
        }"""
    )
    log(f"    [debug] カレンダー内クリック要素（{len(items or [])}件）↓")
    for it in items or []:
        log(f"      {it['attr']}={it['val']!r}  text={it['text']!r}")


def _js_dispatch_click_by_wire(page, attr_val: str) -> str:
    """wire:click 系属性の値が attr_val の要素へ MouseEvent を直接 dispatch する。"""
    return page.evaluate(
        """(val) => {
            const el = Array.from(document.querySelectorAll('*')).find(e =>
                e.getAttributeNames().some(n => n.startsWith('wire:click')
                    && e.getAttribute(n) === val));
            if (!el) return 'no-el';
            el.scrollIntoView({block: 'center'});
            el.dispatchEvent(new MouseEvent('click',
                {bubbles: true, cancelable: true, view: window}));
            return 'clicked';
        }""", attr_val)


def click_date(page, target: date) -> bool:
    """カレンダー上で対象日を選択する（JSで selectDate を直接発火し、
    タイムラインが対象日に切り替わるまで確認する）。"""
    iso = target.strftime("%Y-%m-%d")
    date_slash = target.strftime("%Y/%m/%d")
    res = _js_dispatch_click_by_wire(page, f"selectDate('{iso}')")
    log(f"    selectDate('{iso}') 発火結果: {res}")
    # タイムラインが対象日に切り替わるまで待つ（最大約8秒）
    for _ in range(8):
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except PWTimeout:
            pass
        page.wait_for_timeout(600)
        if any(s["date"] == date_slash for s in get_timeline_slots(page)):
            return True
    log("    ⚠️ タイムラインが対象日に切り替わりませんでした")
    return False


def _js_click_slot(page, idx: int) -> str:
    """slot-{idx} の要素へ MouseEvent を直接 dispatch する（重なり要素の妨害を回避）。"""
    return page.evaluate(
        """(idx) => {
            const el = Array.from(document.querySelectorAll('*'))
                .find(e => e.getAttribute('wire:key') === 'slot-' + idx);
            if (!el) return 'no-el';
            el.scrollIntoView({block: 'center'});
            el.dispatchEvent(new MouseEvent('click',
                {bubbles: true, cancelable: true, view: window}));
            return 'clicked';
        }""", idx)


def get_selected_start(page) -> str:
    """親フォームの selectStartDateTime（選択中の開始日時）を読む。例 '2026-08-15 11:00:00'。"""
    return page.evaluate(
        """() => {
            const el = Array.from(document.querySelectorAll('select'))
                .find(e => e.getAttribute('wire:model.change') === 'selectStartDateTime');
            return el ? el.value : '';
        }""") or ""


def get_select_value(page, model: str) -> str:
    return page.evaluate(
        """(model) => {
            const el = Array.from(document.querySelectorAll('select'))
                .find(e => e.getAttribute('wire:model.change') === model);
            return el ? el.value : '';
        }""", model) or ""


def get_select_options(page, model: str) -> list:
    return page.evaluate(
        """(model) => {
            const el = Array.from(document.querySelectorAll('select'))
                .find(e => e.getAttribute('wire:model.change') === model);
            return el ? Array.from(el.options).map(o => (
                { value: o.value, text: (o.textContent || '').trim() })) : [];
        }""", model) or []


def set_livewire_select(page, model: str, value) -> str:
    """wire:model.change の select に値をセットする。
    まず実操作(select_option)を試し、不可ならJSで value+change を発火。"""
    loc = page.locator(f'select[wire\\:model\\.change="{model}"]')
    if loc.count():
        try:
            loc.first.select_option(str(value), timeout=4000)
            return "pw-ok"
        except Exception:
            pass
    return "js:" + _js_set_select(page, model, value)


def change_select_and_wait(page, model: str, value) -> str:
    """select を変更し、Livewire 更新通信（/livewire/update）の完了を待つ。
    通信が起きなければ JS で再発火して再度待つ。"""
    def is_lw(resp):
        return "/livewire/update" in resp.url
    r = "?"
    try:
        with page.expect_response(is_lw, timeout=10000):
            r = set_livewire_select(page, model, value)
    except PWTimeout:
        try:
            with page.expect_response(is_lw, timeout=8000):
                _js_set_select(page, model, value)
            r = f"{r}+js"
        except PWTimeout:
            r = f"{r}+no-resp"
    page.wait_for_timeout(400)
    return r


def _settle_after_select(page, want: str) -> bool:
    """selectStartDateTime が want になり、画面が安定するまで待つ（最大約6秒）。"""
    ok = False
    for _ in range(8):
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except PWTimeout:
            pass
        page.wait_for_timeout(500)
        if get_selected_start(page).startswith(want):
            ok = True
            break
    # 反映後さらに少し待って再描画を確定させる
    page.wait_for_timeout(800)
    return ok


def select_slot(page, target: date, start_label: str) -> bool:
    """親フォームの selectStartDateTime プルダウンを直接設定して枠を選ぶ
    （タイムラインの子→親伝播を回避）。自動選択済みでも必ず明示設定して
    サーバー側に枠・予約番号を確定登録させ、再描画が安定するまで待つ。"""
    want = f"{target.strftime('%Y-%m-%d')} {start_label}"  # 例 '2026-08-15 17:00'

    opts = get_select_options(page, "selectStartDateTime")
    log(f"    selectStartDateTime の選択肢: {opts}")
    opt = next((o for o in opts
                if o["value"].startswith(want) or o["text"].strip() == start_label), None)
    if not opt:
        log(f"    ⚠️ {start_label} の選択肢が見つかりません")
        return False

    # すでに目的値の場合でも確実にサーバー登録させるため、一旦別の枠を選んでから戻す。
    if get_selected_start(page).startswith(want):
        other = next((o for o in opts if o["value"] != opt["value"]), None)
        if other:
            set_livewire_select(page, "selectStartDateTime", other["value"])
            _settle_after_select(page, other["value"][:16])

    r = set_livewire_select(page, "selectStartDateTime", opt["value"])
    log(f"    selectStartDateTime = {opt['value']} 設定（{r}）")

    if _settle_after_select(page, want):
        log(f"    選択OK selectReserveNumber={get_select_value(page, 'selectReserveNumber')}")
        return True
    log(f"    ⚠️ selectStartDateTime が {want} になりませんでした"
        f"（現在: {get_selected_start(page)}）")
    return False


def debug_options(page):
    """オプション欄の input/select（チェックボックス・人数select）の構造を一覧表示。
    『項目左側の✓』が何の入力か特定するため。"""
    items = page.evaluate(
        """() => Array.from(document.querySelectorAll('input, select')).map(e => {
            const w = {};
            e.getAttributeNames().filter(n => n.startsWith('wire:'))
                .forEach(n => w[n] = e.getAttribute(n));
            const row = e.closest('div');
            const near = row ? (row.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40) : '';
            return { tag: e.tagName, type: e.getAttribute('type'),
                     checked: e.checked, value: e.value, wire: w, near: near };
        }).slice(0, 30)"""
    )
    log(f"    [debug] オプション欄の入力一覧（{len(items or [])}件）↓")
    for it in items or []:
        log(f"      <{it['tag']} type={it['type']} checked={it['checked']} value={it['value']!r}> "
            f"wire={it['wire']} near={it['near']!r}")


def _js_set_select(page, model: str, value) -> str:
    """wire:model.change=model の select に値をセットし、input/change を発火させる。"""
    return page.evaluate(
        """([model, value]) => {
            const el = Array.from(document.querySelectorAll('select'))
                .find(e => e.getAttribute('wire:model.change') === model);
            if (!el) return 'no-el';
            el.value = String(value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return el.value;
        }""", [model, value])


def _js_check_required_boxes(page) -> int:
    """『項目左側の✓』に相当する type=checkbox（hidden/token以外）を全てONにする。"""
    return page.evaluate(
        """() => {
            const boxes = Array.from(document.querySelectorAll('input[type=checkbox]'));
            let n = 0;
            boxes.forEach(b => {
                if (!b.checked) {
                    b.checked = true;
                    b.dispatchEvent(new Event('input', {bubbles: true}));
                    b.dispatchEvent(new Event('change', {bubbles: true}));
                    n++;
                }
            });
            return n;
        }""")


def set_options(page, options: dict):
    """人数オプション（数量select）を設定する。0 の項目は変更しない。"""
    for opt_id, value in options.items():
        if value <= 0:
            continue
        model = f"selectOptionQuantities.{opt_id}"
        r = change_select_and_wait(page, model, value)
        log(f"    option {opt_id} = {value} 設定（{r}, 現在値={get_select_value(page, model)}）")
    page.wait_for_timeout(800)


def debug_buttons(page, label=""):
    """画面上のボタン/フォームの文字と wire: 属性を一覧表示（確定ボタン特定用）。"""
    items = page.evaluate(
        """() => Array.from(document.querySelectorAll('button, form'))
            .map(e => {
                const w = {};
                e.getAttributeNames().filter(n => n.startsWith('wire:'))
                    .forEach(n => w[n] = e.getAttribute(n));
                return { tag: e.tagName,
                         text: (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40),
                         wire: w };
            }).slice(0, 40)"""
    )
    log(f"    [debug] ボタン/フォーム一覧{('（'+label+'）') if label else ''}（{len(items or [])}件）↓")
    for it in items or []:
        log(f"      <{it['tag']}> text={it['text']!r} wire={it['wire']}")


def click_confirm_button(page) -> bool:
    """「予約内容を確認する」を押して確認ページへ進む。
    ボタンクリックに加え、フォームの submit(=next) をJSで直接発火させる。"""
    before = page.url
    # 1) ボタンを押す
    for sel in ['button:has-text("予約内容を確認する")', 'button[type="submit"]']:
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.click(force=True)
            except Exception as e:
                log(f"    （確認ボタンクリック失敗: {e}）")
            break
    # 2) フォーム submit(=next) をJSでも発火（保険）
    res = page.evaluate(
        """() => {
            const f = Array.from(document.querySelectorAll('form')).find(e =>
                e.getAttributeNames().some(n => n.startsWith('wire:submit')));
            if (!f) return 'no-form';
            if (f.requestSubmit) f.requestSubmit(); else
                f.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
            return 'submitted';
        }""")
    log(f"    （フォーム送信: {res}）")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)
    # 進めたか（フォームの『予約内容を確認する』が消えたか / URL変化）で判定
    still_form = page.locator('button:has-text("予約内容を確認する")').count() > 0
    advanced = (not still_form) or ("reserve-confirm" in page.url)
    log(f"    送信後: URL={page.url}（変化={page.url != before}） 確認ページ到達={advanced}")
    if not advanced:
        # 進めなかった理由（バリデーションエラー等）を画面から拾う
        try:
            body = " ".join((page.evaluate("() => document.body.innerText") or "").split())
            hits = [w for w in ("既に", "すでに", "済み", "重複", "できません",
                                "上限", "制限", "選択してください", "必須", "エラー")
                    if w in body]
            log(f"    [debug] 進めない理由の手掛かり: {hits}")
            log(f"    [debug] 画面テキスト（先頭700字）: {body[:700]}")
        except Exception as e:
            log(f"    （理由テキスト取得失敗: {e}）")
    return advanced


# 申込完了を示す文言（完了画面: 「抽選待ち」「抽選予約を受付けました。」等）
DONE_KEYWORDS = ["受付けました", "抽選待ち", "抽選予約のキャンセル", "予約を受け付け", "申込が完了"]


def finalize_reservation(page, tag: str) -> bool:
    """確認ページで『利用規約に同意』→『抽選予約する』を行い、申込完了を確認する。
    DRY_RUN のときは押さずに False（未確定）。"""
    if DRY_RUN:
        log("    🟡 DRY_RUN のため確定はしません（確認ページのスクショ/HTMLのみ保存）")
        return False

    # 利用規約に同意（チェックボックスをON）。実操作＋JSの二重で確実にする。
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

    # 「抽選予約する」を1回だけ送信する。
    # ※ボタンクリックとJS送信を両方やると、1回目で成立→2回目が「同じ時間に予約」
    #   という自己重複エラーになるため、必ず単一の送信にする。
    loc = page.locator('button:has-text("抽選予約する")')
    submitted = False
    if loc.count():
        try:
            loc.first.click(force=True)
            submitted = True
            log("    抽選予約 送信: ボタンクリック")
        except Exception as e:
            log(f"    （抽選予約するボタンのクリック失敗: {e}）")
    if not submitted:
        res = page.evaluate(
            """() => {
                const f = Array.from(document.querySelectorAll('form')).find(e =>
                    e.getAttributeNames().some(n => n.startsWith('wire:submit')));
                if (!f) return 'no-form';
                if (f.requestSubmit) f.requestSubmit(); else
                    f.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
                return 'submitted';
            }""")
        log(f"    抽選予約 送信: フォーム送信({res})")

    # 完了（抽選待ち画面）になるまで、または重複/エラーが出るまでポーリング
    done = False
    duplicate = False
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

    shot(page, f"{tag}_9_after_reserve")
    dump_html(page, f"{tag}_9_after_reserve")
    log(f"    確定後 URL={page.url}")

    if done:
        log("    ✅ 申込完了（抽選待ち）を確認")
        return True
    if duplicate:
        # その日時はすでに申込済み＝予約は存在する
        log("    ℹ️ 同じ時間に既に予約あり（申込済みとして扱う）")
        return True
    log(f"    ⚠️ 完了を確認できませんでした。画面テキスト（先頭800字）: {body[:800]}")
    return False


# ============================================================
# 1枠分の申込
# ============================================================
def _select_through_confirm(page, target: date, slot: dict, tag: str) -> str:
    """施設ページを開く→対象月→日付→枠→オプション→送信 を1回試行する。
    戻り値: 'confirmed'（確認ページ到達） / 'already'（既に申込/予約あり） / 'failed'。
    ※失敗時はサイトが利用日をリセットするため、毎回この関数まるごとをやり直す。"""
    open_facility(page)
    if not open_calendar(page):
        log("    ⚠️ カレンダーを開けませんでした")
        return "failed"
    if not goto_month(page, target.strftime("%Y-%m")):
        log("    ⚠️ 対象月へ移動できませんでした")
        return "failed"
    if not click_date(page, target):
        log("    ⚠️ 対象日を選択できませんでした")
        return "failed"
    shot(page, f"{tag}_4_date_selected")

    select_slot(page, target, slot["start"])
    shot(page, f"{tag}_5_slot_selected")

    set_options(page, slot["options"])
    shot(page, f"{tag}_6_options_set")

    if click_confirm_button(page):
        return "confirmed"

    # 進めなかった場合、既に予約済み等の判定（無駄な再試行を避ける）
    body = " ".join((page.evaluate("() => document.body.innerText") or "").split())
    if any(w in body for w in ("同じ時間に予約", "既に予約", "すでに予約",
                               "予約済み", "申込済み", "申込み済み")):
        return "already"
    return "failed"


def apply_slot(page, target: date, slot: dict) -> str:
    """戻り値: 'applied' / 'partial'(未確定) / 'failed'。"""
    name = slot["name"]
    tag = "day" if slot["start"] == "11:00" else "night"
    log(f"\n  [{name}] 申込開始 (date={target})")

    # 確認ページに到達するまで、最初の日付選択からやり直してリトライ（最大3回）。
    outcome = "failed"
    for attempt in range(1, 4):
        outcome = _select_through_confirm(page, target, slot, tag)
        if outcome in ("confirmed", "already"):
            break
        log(f"    確認ページに進めませんでした（試行 {attempt}/3）。最初からやり直します")
        page.wait_for_timeout(1500)

    if outcome == "already":
        log("    ℹ️ その枠は既に申込/予約済みです（スキップ）")
        return "applied"
    if outcome != "confirmed":
        log("    ⚠️ 確認ページに進めませんでした")
        shot(page, f"{tag}_7_confirm_fail")
        return "failed"

    shot(page, f"{tag}_8_confirm_page")
    dump_html(page, f"{tag}_8_confirm_page")
    log(f"    確認ページURL: {page.url}")

    ok = finalize_reservation(page, tag)
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

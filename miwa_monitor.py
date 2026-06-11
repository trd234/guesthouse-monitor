#!/usr/bin/env python3
"""
共用施設予約サイト キャンセル空き自動予約スクリプト（サイトリニューアル対応版 2）
GitHub Actions（外部cron）で起動 → 内部で1分ごとに複数回チェック = 実質1分間隔

【リニューアル後の仕組み】
旧サイトの /api/reserve/calendar は廃止され、空き情報は施設詳細ページの
Livewire コンポーネント（pages.reserve.facility-detail）の中に入っている。
このスクリプトは施設詳細を Livewire で開き、各日を selectStartDate で選択して
「予約可能な開始時刻（startDateTimes）」が返ってくるかどうかで空きを判定する。
ステータス文字列（"reserved"/"unreservable" 等）の推測には依存しない。

  ・土日祝に空きを検知 → 自動予約を実行（/reserves/{id} 到達で完了確定）
  ・平日に空きを検知   → 通知のみ（自動予約なし）
"""

import os
import json
import time
import re
import html
import urllib.parse
import requests
from pathlib import Path
from datetime import datetime, date, timezone, timedelta

try:
    import jpholiday
except ImportError:
    jpholiday = None

# ============================================================
# 設定（マンション固有の値は環境変数から取得）
# ============================================================
BASE_URL = os.environ.get("MIWA_BASE_URL", "").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login"
LIVEWIRE_UPDATE = f"{BASE_URL}/livewire/update"

# 予約・監視の対象施設（子施設ID）。例: VILLA／1階 GUEST HOUSE（片方ご利用）の詳細ページ
FACILITY_ID_RESERVE = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")
FACILITY_URL = f"{BASE_URL}/facilities/{FACILITY_ID_RESERVE}"

# 当月＋2ヶ月先までチェック（計3ヶ月分）
MONTHS_AHEAD = 2

# 前回の状態を保存するファイル（重複通知・二重予約を防ぐ）
STATE_FILE = "miwa_state.json"

# GitHub Actionsの最短cronは5分のため、1回の実行内で1分ごとに複数回チェックし実質1分間隔を実現
LOOP_COUNT = 5
LOOP_INTERVAL_SEC = 60

# 連続リクエストでサーバに負荷をかけないための待機（秒）
PROBE_PAUSE_SEC = 0.3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# 自動予約する枠の設定
# option_id: Livewire の selectOptionQuantities.{option_id} に対応（診断で確認済み）
#   46 = 21時までの利用人数（0〜8）
#   49 = 21時以降の利用人数（0〜4）
#   61 = 夜枠用チェックイン予定時間（0=未選択 / 1=17:00〜19:00 ...）
SLOT_OPTIONS = {
    "1100": {
        "name": "昼枠（11:00〜15:00）",
        "options": {46: 8, 49: 0, 61: 0},
    },
    "1700": {
        "name": "夜枠（17:00〜翌9:00）",
        "options": {46: 8, 49: 4, 61: 1},
    },
}

JST = timezone(timedelta(hours=9))


# ============================================================
# 土日祝判定
# ============================================================
def is_weekend_or_holiday(d: date) -> bool:
    if d.weekday() >= 5:
        return True
    if jpholiday and jpholiday.is_holiday(d):
        return True
    return False


def weekday_ja(d: date) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]


# ============================================================
# 状態の読み書き
# ============================================================
def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
        except (json.JSONDecodeError, OSError):
            st = {}
    else:
        st = {}
    st.setdefault("booked", [])      # 自動予約に成功した "YYYY-MM-DD_HHMM"
    st.setdefault("notified", [])    # 空き通知済みの "YYYY-MM-DD_HHMM"
    return st


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# Cookie ヘルパー（XSRF-TOKEN 重複対策）
# ============================================================
def _iter_xsrf_values(jar) -> list:
    return [c.value for c in jar if c.name == "XSRF-TOKEN"]


def _get_xsrf_token(session: requests.Session) -> str:
    values = _iter_xsrf_values(session.cookies)
    return urllib.parse.unquote(values[-1]) if values else ""


def _set_xsrf_token(session: requests.Session, raw_value: str) -> None:
    for cookie in list(session.cookies):
        if cookie.name == "XSRF-TOKEN":
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
    session.cookies.set("XSRF-TOKEN", raw_value)


# ============================================================
# ログイン
# ============================================================
def create_session() -> requests.Session:
    user_id = os.environ.get("MIWA_USER_ID", "")
    password = os.environ.get("MIWA_PASSWORD", "")
    if not user_id or not password:
        print("  ⚠️ MIWA_USER_ID / MIWA_PASSWORD が未設定です")
        return None

    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(LOGIN_URL, timeout=15)
    resp.raise_for_status()
    match = re.search(r'name="_token"\s+value="([^"]+)"', resp.text)
    if not match:
        print("  ⚠️ CSRFトークンが取得できません")
        return None

    resp = session.post(LOGIN_URL, data={
        "_token": match.group(1),
        "email": user_id,
        "password": password,
    }, timeout=15)
    resp.raise_for_status()

    if "/login" in resp.url:
        print("  ⚠️ ログイン失敗（ID/パスワードをご確認ください）")
        return None

    print("  ログイン成功")
    return session


# ============================================================
# Livewire API ヘルパー
# ============================================================
def extract_facility_snapshot(session: requests.Session) -> tuple:
    """施設詳細ページ（/facilities/{id}）から facility-detail の wire:snapshot を取得する。
    戻り値: (snapshot_str, xsrf_token) または (None, None)
    """
    try:
        resp = session.get(FACILITY_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠️ 施設ページ取得エラー: {e}")
        return None, None

    snaps = re.findall(r'wire:snapshot="((?:&[^;]+;|[^"])+)"', resp.text)
    for s in snaps:
        decoded = html.unescape(s)
        try:
            d = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        if "facility-detail" in d.get("memo", {}).get("name", ""):
            return decoded, _get_xsrf_token(session)

    print("  ⚠️ 施設詳細の Livewire スナップショットが見つかりません")
    return None, None


def livewire_call(session, snap_str, xsrf, updates=None, calls=None) -> tuple:
    """Livewire update エンドポイントを呼び出す。
    戻り値: (new_snap_str, new_data, effects) または (None, None, {})
    """
    if updates is None:
        updates = {}
    if calls is None:
        calls = []

    fresh_xsrf = _get_xsrf_token(session)
    if fresh_xsrf:
        xsrf = fresh_xsrf

    body = {"components": [{"snapshot": snap_str, "updates": updates, "calls": calls}]}

    try:
        resp = session.post(
            LIVEWIRE_UPDATE,
            json=body,
            headers={
                "X-XSRF-TOKEN": xsrf,
                "X-Livewire": "true",
                "Accept": "application/json",
                "Referer": FACILITY_URL,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ⚠️ Livewire リクエストエラー: {e}")
        return None, None, {}

    if resp.status_code != 200:
        print(f"  ⚠️ Livewire エラー: {resp.status_code} {resp.text[:200]}")
        return None, None, {}

    resp_xsrf_values = _iter_xsrf_values(resp.cookies)
    if resp_xsrf_values:
        _set_xsrf_token(session, resp_xsrf_values[-1])

    try:
        comp = resp.json()["components"][0]
        new_snap_str = comp["snapshot"]
        new_data = json.loads(new_snap_str)["data"]
        effects = comp.get("effects", {})
        return new_snap_str, new_data, effects
    except (KeyError, json.JSONDecodeError, IndexError) as e:
        print(f"  ⚠️ Livewire レスポンス解析エラー: {e}")
        return None, None, {}


# ============================================================
# Livewire データ構造のヘルパー
# ============================================================
def _unwrap_arr(v):
    """Livewire は配列を [payload, {"s":"arr"}] の形でラップする。payload を取り出す。"""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict) and v[1].get("s") == "arr":
        return v[0]
    return v


def _carbon_to_str(item):
    """Carbon 値 ["2026-06-27T11:00:00+09:00", {...}] や 文字列・辞書から ISO 文字列を取り出す。"""
    if isinstance(item, str):
        return item
    if isinstance(item, list) and item and isinstance(item[0], str):
        return item[0]
    if isinstance(item, dict):
        for k in ("value", "start", "datetime", "date"):
            v = item.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0]
    return None


def parse_start_times(data: dict) -> list:
    """選択中の日付に対する予約可能な開始時刻を、対象枠（昼枠/夜枠）に絞って返す。
    戻り値: [{"time": "1100", "name": "昼枠...", "start_dt": "...ISO..."}, ...]
    """
    raw = _unwrap_arr(data.get("startDateTimes", []))
    if isinstance(raw, dict):
        items = list(raw.keys())          # value→label 形式の select の場合
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    slots = []
    seen = set()
    for item in items:
        dt_str = _carbon_to_str(item)
        if not dt_str:
            continue
        hour = None
        try:
            hour = datetime.fromisoformat(dt_str).hour
        except ValueError:
            m = re.search(r'\b(\d{1,2}):(\d{2})\b', dt_str)
            if m:
                hour = int(m.group(1))
        if hour is None:
            continue
        if hour == 11:
            time_code = "1100"
        elif hour == 17:
            time_code = "1700"
        else:
            continue  # 対象外の時間帯（昼枠/夜枠以外）
        if time_code in seen:
            continue
        seen.add(time_code)
        slots.append({
            "time": time_code,
            "name": SLOT_OPTIONS[time_code]["name"],
            "start_dt": dt_str,
        })
    return slots


# ============================================================
# 空きスキャン
# ============================================================
def get_candidate_dates() -> list:
    """今日から当月＋MONTHS_AHEAD ヶ月の末日までの date を列挙する。"""
    today = date.today()
    y, m = today.year, today.month
    m += MONTHS_AHEAD
    while m > 12:
        m -= 12
        y += 1
    next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
    end = date(next_y, next_m, 1) - timedelta(days=1)
    days, d = [], today
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def scan_availability(session, target_days: list) -> dict:
    """指定した日付リストを Livewire で順に選択し、各日の空き枠を調べる。
    1回の GET で取得したスナップショットを使い回し、selectStartDate を連続で更新する。
    戻り値: {"YYYY-MM-DD": [slot, ...], ...}（空きのある日だけ）
    """
    results = {}
    if not target_days:
        return results

    snap_str, xsrf = extract_facility_snapshot(session)
    if not snap_str:
        return results

    for d in target_days:
        date_str = d.isoformat()
        new_snap, data, _ = livewire_call(session, snap_str, xsrf, updates={"selectStartDate": date_str})
        if not new_snap or data is None:
            # スナップショットが壊れた可能性 → 取り直して1回だけ再試行
            snap_str, xsrf = extract_facility_snapshot(session)
            if not snap_str:
                break
            new_snap, data, _ = livewire_call(session, snap_str, xsrf, updates={"selectStartDate": date_str})
            if not new_snap or data is None:
                continue
        snap_str = new_snap

        slots = parse_start_times(data)
        if slots:
            results[date_str] = slots
        time.sleep(PROBE_PAUSE_SEC)

    return results


# ============================================================
# 自動予約（Livewire ベース）
# ============================================================
def book_slot(session, date_str: str, slot: dict) -> str:
    """1枠の予約を試みる。戻り値: "booked"（完了） / "partial"（未確定） / "failed"。"""
    time_code = slot["time"]
    name = slot["name"]
    start_dt = slot["start_dt"]
    print(f"    [{name}] 予約開始 (date={date_str}, start={start_dt})")

    snap_str, xsrf = extract_facility_snapshot(session)
    if not snap_str:
        return "failed"

    # 日付を選択
    snap_str, data, _ = livewire_call(session, snap_str, xsrf, updates={"selectStartDate": date_str})
    if not snap_str:
        print("    ⚠️ 日付選択失敗")
        return "failed"

    # 日時・人数・オプションをまとめて設定
    updates = {
        "selectStartDate": date_str,
        "selectStartDateTime": start_dt,
        "selectReserveNumber": 1,
    }
    for opt_id, value in SLOT_OPTIONS[time_code]["options"].items():
        updates[f"selectOptionQuantities.{opt_id}"] = value
    snap_str, data, _ = livewire_call(session, snap_str, xsrf, updates=updates)
    if not snap_str:
        print("    ⚠️ オプション設定失敗")
        return "failed"

    # next を呼び出して確認ステップへ進める
    snap_str, data, effects = livewire_call(session, snap_str, xsrf, calls=[{"method": "next", "params": []}])
    if not snap_str:
        print("    ⚠️ 予約送信失敗（スロットが既に埋まった可能性）")
        return "failed"

    errors = effects.get("errors")
    if errors:
        print(f"    ⚠️ バリデーションエラー: {errors}")
        return "failed"

    redirect_url = effects.get("redirect")
    if redirect_url:
        print(f"    確認ページへ移動: {redirect_url}")
        if _confirm_and_verify(session, redirect_url):
            print(f"    ✅ {name} 予約完了！")
            return "booked"
        print(f"    ⚠️ {name} は確認ページまで進みましたが自動確定できませんでした")
        return "partial"

    # redirect が無い場合は確定に至っていない（偽の成功を出さない）
    print(f"    ⚠️ {name} は確認ページへ進めませんでした（未確定）")
    return "partial"


def _confirm_and_verify(session, url: str) -> bool:
    """確認ページで確定操作を行い、予約一覧（/reserves/{id}）に到達できたら True。"""
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    ⚠️ 確認ページ取得エラー: {e}")
        return False

    # 既に予約明細ページに到達していれば成功
    if re.search(r"/reserves/\d+", resp.url) or re.search(r"/reserves/\d+", resp.text):
        return True

    snaps = re.findall(r'wire:snapshot="((?:&[^;]+;|[^"])+)"', resp.text)
    for s in snaps:
        decoded = html.unescape(s)
        try:
            d = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        comp_data = d.get("data", {})
        xsrf = _get_xsrf_token(session)

        # 利用規約同意などのチェックボックスがあれば true にする
        agree_props = ["agreeTerms", "agree", "isAgreed", "acceptTerms",
                       "agreedToTerms", "acceptedTerms", "isAgree"]
        agree_updates = {k: True for k in agree_props if k in comp_data}
        use_snap = decoded
        if agree_updates:
            snap_a, _, _ = livewire_call(session, decoded, xsrf, updates=agree_updates)
            if snap_a:
                use_snap = snap_a

        # 確定系メソッドを順に試し、/reserves/{id} に到達したら成功確定
        for method in ["complete", "confirm", "reserve", "submit", "save",
                       "next", "apply", "store"]:
            snap2, _, effects2 = livewire_call(session, use_snap, xsrf,
                                               calls=[{"method": method, "params": []}])
            if snap2 is None:
                continue
            redirect2 = effects2.get("redirect")
            if redirect2:
                try:
                    resp3 = session.get(redirect2, timeout=15)
                    if re.search(r"/reserves/\d+", resp3.url) or re.search(r"/reserves/\d+", resp3.text):
                        return True
                except requests.RequestException:
                    pass

    # 最後に予約一覧を見て、確定済みか最終確認
    try:
        resp_list = session.get(f"{BASE_URL}/reserves", timeout=15)
        if re.search(r"/reserves/\d+", resp_list.text):
            # 一覧に明細リンクがあるだけでは当該予約とは限らないため、慎重に partial 扱い
            return False
    except requests.RequestException:
        pass
    return False


# ============================================================
# LINE通知
# ============================================================
def _send_line_message(message: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("  ⚠️ LINE_CHANNEL_ACCESS_TOKEN が未設定です")
        return
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if resp.status_code == 200:
            print("  ✅ LINE通知を送信しました")
        else:
            print(f"  ❌ LINE通知失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  ❌ LINE通知エラー: {e}")


def send_booked_notification(date_str: str, booked_names: list):
    slots_text = "\n".join(f"  ・{s}" for s in booked_names)
    _send_line_message(
        f"🏨【共用施設 自動予約完了】\n{date_str} の予約を確保しました！\n\n"
        f"{slots_text}\n\n予約状況の確認👇\n{BASE_URL}/reserves"
    )


def send_partial_notification(date_str: str, names: list):
    slots_text = "\n".join(f"  ・{s}" for s in names)
    _send_line_message(
        f"🟠【共用施設 空き検知・要手動確認】\n{date_str} に空きを検知し予約を進めましたが、"
        f"自動で確定できませんでした。\nお早めに手動でご確認ください👇\n\n"
        f"{slots_text}\n\n{FACILITY_URL}"
    )


def send_vacancy_notification(date_str: str, slot_names: list):
    slots_text = "\n".join(f"  ・{s}" for s in slot_names)
    _send_line_message(
        f"🟢【共用施設 空き検知】\n{date_str} に空きが出ました！（平日のため自動予約なし）\n\n"
        f"{slots_text}\n\n手動で予約する👇\n{FACILITY_URL}"
    )


# ============================================================
# メイン処理
# ============================================================
def run_once(session, state: dict, loop_index: int) -> dict:
    booked_list = state.get("booked", [])
    notified_list = state.get("notified", [])

    all_days = get_candidate_dates()
    holidays = [d for d in all_days if is_weekend_or_holiday(d)]
    weekdays = [d for d in all_days if not is_weekend_or_holiday(d)]

    # 土日祝（自動予約対象）は毎回スキャン。平日（通知のみ）は負荷軽減のため初回ループのみ。
    target_days = list(holidays)
    if loop_index == 0:
        target_days += weekdays
    target_days.sort()

    availability = scan_availability(session, target_days)

    avail_count = sum(len(v) for v in availability.values())
    print(f"  空き検知: {len(availability)}日 / {avail_count}枠")

    current_avail_keys = set()
    for date_str in sorted(availability.keys()):
        slots = availability[date_str]
        d = date.fromisoformat(date_str)
        wd = weekday_ja(d)
        slot_label = "・".join(s["name"] for s in slots)
        print(f"  🟢 {date_str}（{wd}）空き: {slot_label}")

        for slot in slots:
            current_avail_keys.add(f"{date_str}_{slot['time']}")

        if is_weekend_or_holiday(d):
            # 土日祝 → 自動予約
            booked_names, partial_names = [], []
            for slot in slots:
                slot_key = f"{date_str}_{slot['time']}"
                if slot_key in booked_list:
                    print(f"    [{slot['name']}] 予約済みのためスキップ")
                    continue
                outcome = book_slot(session, date_str, slot)
                if outcome == "booked":
                    booked_list.append(slot_key)
                    booked_names.append(slot["name"])
                elif outcome == "partial":
                    partial_names.append(slot["name"])
            if booked_names:
                send_booked_notification(date_str, booked_names)
            if partial_names:
                send_partial_notification(date_str, partial_names)
        else:
            # 平日 → 新規の空きだけ通知（重複通知を防ぐ）
            new_names = []
            for slot in slots:
                slot_key = f"{date_str}_{slot['time']}"
                if slot_key not in notified_list:
                    notified_list.append(slot_key)
                    new_names.append(slot["name"])
            if new_names:
                send_vacancy_notification(date_str, new_names)

    # 既に空きが無くなった通知済みキーは解除（次の空きで再通知できるように）
    notified_list = [k for k in notified_list if k in current_avail_keys]

    state["booked"] = booked_list
    state["notified"] = notified_list
    return state


def main():
    if not BASE_URL or not FACILITY_ID_RESERVE:
        print("⚠️ MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE が未設定です")
        return

    print(f"\n{'='*50}")
    print(f"共用施設 空き自動予約：{LOOP_COUNT}回 × {LOOP_INTERVAL_SEC}秒")
    days = get_candidate_dates()
    print(f"監視期間: {days[0].isoformat()} 〜 {days[-1].isoformat()}")
    print(f"{'='*50}")

    session = create_session()
    if not session:
        print("セッション作成に失敗しました。終了します。")
        return

    state = load_state()
    for i in range(LOOP_COUNT):
        now = datetime.now(JST).strftime("%H:%M:%S")
        print(f"\n--- チェック {i+1}/{LOOP_COUNT}  ({now} JST) ---")
        state = run_once(session, state, i)
        save_state(state)
        if i < LOOP_COUNT - 1:
            print(f"  {LOOP_INTERVAL_SEC}秒後に再チェック...")
            time.sleep(LOOP_INTERVAL_SEC)

    print(f"\n{'='*50}")
    print("全チェック完了")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

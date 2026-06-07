#!/usr/bin/env python3
"""
共用施設予約サイト キャンセル空き自動予約スクリプト（サイトリニューアル対応版）
GitHub Actions で5分ごとに起動 → 内部で1分ごとに5回チェック = 実質1分間隔

カレンダーで「available」（先着予約可能）を検知したら Livewire API で自動予約を行い、
予約完了時に LINE 通知を送る。
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
from bs4 import BeautifulSoup

try:
    import jpholiday
except ImportError:
    jpholiday = None

# ============================================================
# 設定（マンション固有の値は環境変数から取得）
# ============================================================
BASE_URL = os.environ.get("MIWA_BASE_URL", "")
LOGIN_URL = f"{BASE_URL}/login"
CALENDAR_API = f"{BASE_URL}/api/reserve/calendar"
LIVEWIRE_UPDATE = f"{BASE_URL}/livewire/update"

FACILITY_ID = os.environ.get("MIWA_FACILITY_ID", "")          # 監視用（親）: 100310
FACILITY_ID_RESERVE = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")  # 予約用（子）: 100371

# 当月＋2ヶ月先までチェック（計3ヶ月分）
MONTHS_AHEAD = 2

# 前回の状態を保存するファイル
STATE_FILE = "miwa_state.json"

# GitHub Actionsの最短cronは5分のため、1回の実行内で1分ごとに5回チェックし実質1分間隔を実現
LOOP_COUNT = 5
LOOP_INTERVAL_SEC = 60

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
# option_id: Livewire の selectOptionQuantities.{option_id} に対応
SLOT_OPTIONS = {
    "1100": {
        "name": "昼枠（11:00〜15:00）",
        "options": {
            46: 8,   # 21時までの利用人数: 8名
            49: 0,   # 21時以降: 不要
            61: 0,   # チェックイン時間: 不要
        },
    },
    "1700": {
        "name": "夜枠（17:00〜翌9:00）",
        "options": {
            46: 8,   # 21時までの利用人数: 8名
            49: 4,   # 21時以降の利用人数: 4名
            61: 1,   # チェックイン予定時間: 1 = 17:00〜19:00
        },
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


# ============================================================
# 状態の読み書き
# ============================================================
def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"calendar": {}, "booked": []}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
        print("  ⚠️ ログイン失敗")
        return None

    print("  ログイン成功")
    return session


# ============================================================
# カレンダーチェック（旧APIを継続使用）
# ============================================================
def get_months_to_check() -> list:
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(MONTHS_AHEAD + 1):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def check_calendar(session: requests.Session) -> dict:
    """カレンダーAPIから各日の予約状況を取得する（親施設IDで監視）"""
    results = {}
    for year, month in get_months_to_check():
        resp = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(5 * attempt)
                resp = session.get(
                    CALENDAR_API,
                    params={"year": year, "month": month, "id": FACILITY_ID},
                    timeout=20,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"  ⚠️ カレンダー取得エラー ({year}/{month}) 試行{attempt+1}/3: {e}")
                resp = None
        if resp is None:
            continue

        # クラス名に先頭スペースが付く場合があるため strip() で正規化
        for match in re.finditer(
            r'<td class="([^"]+)">\s*<a class="link_area">(\d+)</a>\s*</td>',
            resp.text,
        ):
            status = match.group(1).strip()
            day = int(match.group(2))
            date_str = f"{year}-{month:02d}-{day:02d}"
            results[date_str] = status

    return results


# ============================================================
# Livewire API ヘルパー
# ============================================================
def extract_facility_snapshot(session: requests.Session) -> tuple:
    """施設予約ページ（/facilities/{id}）から wire:snapshot を取得する。
    戻り値: (snapshot_str, xsrf_token)
    """
    facility_url = f"{BASE_URL}/facilities/{FACILITY_ID_RESERVE}"
    try:
        resp = session.get(facility_url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠️ 施設ページ取得エラー: {e}")
        return None, None

    # wire:snapshot 属性を抽出（HTML エスケープ済みの JSON 文字列として取得）
    snaps = re.findall(r'wire:snapshot="((?:&[^;]+;|[^"])+)"', resp.text)
    for s in snaps:
        decoded = html.unescape(s)
        try:
            d = json.loads(decoded)
            if "facility-detail" in d.get("memo", {}).get("name", ""):
                xsrf = urllib.parse.unquote(session.cookies.get("XSRF-TOKEN", ""))
                return decoded, xsrf
        except (json.JSONDecodeError, KeyError):
            continue

    print("  ⚠️ Livewire スナップショットが見つかりません")
    return None, None


def livewire_call(
    session: requests.Session,
    snap_str: str,
    xsrf: str,
    updates: dict = None,
    calls: list = None,
) -> tuple:
    """Livewire update エンドポイントを呼び出す。
    snap_str: wire:snapshot の JSON 文字列（HTML アンエスケープ済み）
    戻り値: (new_snap_str, new_data, effects) または (None, None, {})
    """
    if updates is None:
        updates = {}
    if calls is None:
        calls = []

    # 毎回セッションCookieから最新のXSRFトークンを取得（古いトークンで419エラーになるのを防ぐ）
    fresh_xsrf_raw = next(
        (c.value for c in reversed(list(session.cookies)) if c.name == "XSRF-TOKEN"),
        ""
    )
    if fresh_xsrf_raw:
        xsrf = urllib.parse.unquote(fresh_xsrf_raw)

    body = {
        "components": [{
            "snapshot": snap_str,
            "updates": updates,
            "calls": calls,
        }]
    }

    try:
        resp = session.post(
            LIVEWIRE_UPDATE,
            json=body,
            headers={
                "X-XSRF-TOKEN": xsrf,
                "X-Livewire": "true",
                "Accept": "application/json",
                "Referer": f"{BASE_URL}/facilities/{FACILITY_ID_RESERVE}",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ⚠️ Livewire リクエストエラー: {e}")
        return None, None, {}

    if resp.status_code != 200:
        print(f"  ⚠️ Livewire エラー: {resp.status_code} {resp.text[:200]}")
        return None, None, {}

    # レスポンスのXSRFトークンをセッションに反映（requestsが自動処理しない場合の補完）
    new_xsrf_enc = resp.cookies.get("XSRF-TOKEN")
    if new_xsrf_enc:
        session.cookies.set("XSRF-TOKEN", new_xsrf_enc)

    try:
        result = resp.json()
        comp = result["components"][0]
        new_snap_str = comp["snapshot"]  # 既に JSON 文字列
        new_data = json.loads(new_snap_str)["data"]
        effects = comp.get("effects", {})
        return new_snap_str, new_data, effects
    except (KeyError, json.JSONDecodeError, IndexError) as e:
        print(f"  ⚠️ Livewire レスポンス解析エラー: {e}")
        return None, None, {}


# ============================================================
# 利用可能スロットの取得（Livewire ベース）
# ============================================================
def get_available_slots_livewire(
    session: requests.Session, date_str: str
) -> list:
    """Livewire API で指定日の利用可能スロットを取得する。
    戻り値: [{"time": "1100", "name": "昼枠...", "start_dt": "2026-04-20T11:00:00+09:00"}, ...]
    """
    snap_str, xsrf = extract_facility_snapshot(session)
    if not snap_str:
        return []

    # 日付を選択して startDateTimes を取得
    snap_str, data, _ = livewire_call(
        session, snap_str, xsrf,
        updates={"selectStartDate": date_str},
    )
    if not snap_str or data is None:
        return []

    # startDateTimes の構造: [[dt1, dt2, ...], {"s": "arr"}]
    raw_dts = data.get("startDateTimes", [])
    if isinstance(raw_dts, list) and len(raw_dts) >= 1:
        dt_list = raw_dts[0]
        if isinstance(dt_list, list):
            slots = []
            for item in dt_list:
                # Carbon 形式 ["ISO_STRING", {...}] または 文字列
                if isinstance(item, list) and len(item) >= 1:
                    dt_str = item[0]
                elif isinstance(item, str):
                    dt_str = item
                else:
                    continue

                try:
                    dt = datetime.fromisoformat(dt_str)
                    hour = dt.hour
                    if hour == 11:
                        time_code = "1100"
                    elif hour == 17:
                        time_code = "1700"
                    else:
                        continue  # 対象外の時間帯

                    if time_code in SLOT_OPTIONS:
                        slots.append({
                            "time": time_code,
                            "name": SLOT_OPTIONS[time_code]["name"],
                            "start_dt": dt_str,
                        })
                except (ValueError, IndexError):
                    continue

            if slots:
                return slots

    # startDateTimes が空の場合、SLOT_OPTIONS の固定時間帯を試みる
    # （先着可能状態だが Livewire がまだ更新されていない場合のフォールバック）
    print(f"  （startDateTimes 空のため固定スロットで試みます）")
    fallback = []
    for time_code, opt in SLOT_OPTIONS.items():
        hour = int(time_code[:2])
        minute = int(time_code[2:])
        start_dt = f"{date_str}T{hour:02d}:{minute:02d}:00+09:00"
        fallback.append({
            "time": time_code,
            "name": opt["name"],
            "start_dt": start_dt,
        })
    return fallback


# ============================================================
# 自動予約（Livewire ベース）
# ============================================================
def book_slot_livewire(
    session: requests.Session, date_str: str, slot: dict
) -> bool:
    """Livewire API を使って1スロットの予約を実行する。"""
    time_code = slot["time"]
    name = slot["name"]
    start_dt = slot["start_dt"]

    print(f"    [{name}] 予約開始 (date={date_str}, start={start_dt})")

    # Step 1: 施設ページのスナップショット取得
    snap_str, xsrf = extract_facility_snapshot(session)
    if not snap_str:
        return False

    # Step 2: 日付を選択
    snap_str, data, _ = livewire_call(
        session, snap_str, xsrf,
        updates={"selectStartDate": date_str},
    )
    if not snap_str:
        print(f"    ⚠️ 日付選択失敗")
        return False

    # Step 3: 全オプションと日時をセット
    opt_config = SLOT_OPTIONS[time_code]["options"]
    updates = {
        "selectStartDate": date_str,
        "selectStartDateTime": start_dt,
        "selectReserveNumber": 1,
    }
    for opt_id, value in opt_config.items():
        updates[f"selectOptionQuantities.{opt_id}"] = value

    snap_str, data, _ = livewire_call(
        session, snap_str, xsrf,
        updates=updates,
    )
    if not snap_str:
        print(f"    ⚠️ オプション設定失敗")
        return False

    # Step 4: next を呼び出して予約を進める
    snap_str, data, effects = livewire_call(
        session, snap_str, xsrf,
        calls=[{"method": "next", "params": []}],
    )
    if not snap_str:
        print(f"    ⚠️ 予約送信失敗（スロットが既に埋まった可能性）")
        return False

    # リダイレクトがあれば確認ページへ
    redirect_url = effects.get("redirect")
    if redirect_url:
        print(f"    確認ページへ移動: {redirect_url}")
        return _handle_confirm_page(session, redirect_url)

    # エラーがあれば表示
    errors = effects.get("errors", [])
    if errors:
        print(f"    ⚠️ バリデーションエラー: {errors}")
        return False

    # リダイレクトなし・エラーなし → 予約完了と判断
    print(f"    ✅ {name} 予約完了！")
    return True


def _handle_confirm_page(session: requests.Session, url: str) -> bool:
    """確認ページを取得し、Livewire の confirm/save メソッドを呼び出す。"""
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    ⚠️ 確認ページ取得エラー: {e}")
        return False

    # Livewire スナップショットを探す
    snaps = re.findall(r'wire:snapshot="((?:&[^;]+;|[^"])+)"', resp.text)
    for s in snaps:
        decoded = html.unescape(s)
        try:
            d = json.loads(decoded)
            name = d.get("memo", {}).get("name", "")
            if any(k in name for k in ["confirm", "complete", "save"]):
                xsrf = urllib.parse.unquote(session.cookies.get("XSRF-TOKEN", ""))
                for method in ["confirm", "save", "complete", "submit"]:
                    snap_str2, data2, effects2 = livewire_call(
                        session, decoded, xsrf,
                        calls=[{"method": method, "params": []}],
                    )
                    if snap_str2 is None:
                        continue
                    # リダイレクトがあれば最終確認ページへ追跡する
                    redirect2 = effects2.get("redirect")
                    if redirect2:
                        try:
                            resp3 = session.get(redirect2, timeout=15)
                            if "/reserves/" in resp3.url or "/reserves/" in resp3.text:
                                print(f"    ✅ 予約確定完了！（{resp3.url}）")
                                return True
                        except requests.RequestException:
                            pass
                    if not effects2.get("errors"):
                        print(f"    ✅ 予約確定完了！")
                        return True
        except (json.JSONDecodeError, KeyError):
            continue

    # スナップショットが見つからない場合、ページに /reserves/{id} があれば成功
    if "/reserves/" in resp.url or "/reserves/" in resp.text:
        print(f"    ✅ 予約完了！（確認ページ: {resp.url}）")
        return True

    print(f"    ⚠️ 確認処理が不明: {url}")
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
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  ✅ LINE通知を送信しました")
        else:
            print(f"  ❌ LINE通知失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  ❌ LINE通知エラー: {e}")


def send_booked_notification(date_str: str, booked_slots: list):
    """自動予約完了時の通知（土日祝）"""
    slots_text = "\n".join(f"  ・{s}" for s in booked_slots)
    message = (
        f"🏨【共用施設 自動予約完了】\n"
        f"{date_str} の予約を確保しました！\n"
        f"\n"
        f"{slots_text}\n"
        f"\n"
        f"予約状況の確認👇\n"
        f"{BASE_URL}/reserves"
    )
    _send_line_message(message)


def send_vacancy_notification(date_str: str, slot_names: list):
    """空き検知通知（平日 — 自動予約なし）"""
    slots_text = "\n".join(f"  ・{s}" for s in slot_names)
    message = (
        f"🟢【共用施設 空き検知】\n"
        f"{date_str} に空きが出ました！（平日のため自動予約なし）\n"
        f"\n"
        f"{slots_text}\n"
        f"\n"
        f"手動で予約する👇\n"
        f"{BASE_URL}/facilities/{FACILITY_ID_RESERVE}"
    )
    _send_line_message(message)


# ============================================================
# メイン処理
# ============================================================
def run_once(session: requests.Session, prev_state: dict) -> dict:
    calendar = check_calendar(session)
    if not calendar:
        return prev_state

    prev_cal = prev_state.get("calendar", {})
    booked_list = prev_state.get("booked", [])

    # 状況サマリ
    available_dates = [d for d, s in calendar.items() if s == "available"]
    reserved_dates = [d for d, s in calendar.items() if s == "reserved"]
    print(f"  予約済み: {len(reserved_dates)}日 / 先着予約可能: {len(available_dates)}日")

    for d in sorted(available_dates):
        print(f"  🟢 {d} 先着予約可能")

    # 新たに available になった日を検知
    new_available = [d for d in available_dates if prev_cal.get(d) != "available"]

    for d in sorted(new_available):
        target_date = date.fromisoformat(d)
        is_holiday = is_weekend_or_holiday(target_date)
        weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][target_date.weekday()]

        # Livewire で利用可能スロットを取得
        slots = get_available_slots_livewire(session, d)
        if not slots:
            print(f"    予約可能なスロットが見つかりません")
            continue

        if is_holiday:
            # 土日祝 → 自動予約を実行
            print(f"\n  🎉 {d}（{weekday_ja}）の空きを検知！土日祝のため自動予約を試みます")

            booked_names = []
            for slot in slots:
                slot_key = f"{d}_{slot['time']}"
                if slot_key in booked_list:
                    print(f"    [{slot['name']}] 予約済みのためスキップ")
                    continue

                ok = book_slot_livewire(session, d, slot)
                if ok:
                    booked_list.append(slot_key)
                    booked_names.append(slot["name"])

            if booked_names:
                send_booked_notification(d, booked_names)
        else:
            # 平日 → 自動予約せず通知のみ
            print(f"\n  🟢 {d}（{weekday_ja}）の空きを検知！平日のため通知のみ")
            slot_names = [s["name"] for s in slots]
            send_vacancy_notification(d, slot_names)

    # available が埋まった日を検知
    lost = [d for d, s in prev_cal.items()
            if s == "available" and calendar.get(d) != "available"]
    for d in sorted(lost):
        print(f"  [{d}] 予約が埋まりました。次回空き時に再試行します")

    return {"calendar": calendar, "booked": booked_list}


def main():
    if not BASE_URL or not FACILITY_ID:
        print("⚠️ MIWA_BASE_URL / MIWA_FACILITY_ID が未設定です")
        return
    if not FACILITY_ID_RESERVE:
        print("⚠️ MIWA_FACILITY_ID_RESERVE が未設定です")
        return

    print(f"\n{'='*50}")
    print(f"共用施設 空き自動予約：{LOOP_COUNT}回 × {LOOP_INTERVAL_SEC}秒")
    months = get_months_to_check()
    print(f"監視期間: {months[0][0]}/{months[0][1]}月 〜 {months[-1][0]}/{months[-1][1]}月")
    print(f"{'='*50}")

    session = create_session()
    if not session:
        print("セッション作成に失敗しました。終了します。")
        return

    state = load_state()

    for i in range(LOOP_COUNT):
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n--- チェック {i+1}/{LOOP_COUNT}  ({now}) ---")
        state = run_once(session, state)
        save_state(state)

        if i < LOOP_COUNT - 1:
            print(f"  {LOOP_INTERVAL_SEC}秒後に再チェック...")
            time.sleep(LOOP_INTERVAL_SEC)

    print(f"\n{'='*50}")
    print("全チェック完了")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

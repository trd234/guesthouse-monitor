#!/usr/bin/env python3
"""
共用施設予約サイト キャンセル空き自動予約スクリプト
GitHub Actions で5分ごとに起動 → 内部で1分ごとに5回チェック = 実質1分間隔

カレンダーで「available」（先着予約可能）を検知したら自動予約を行い、
予約完了時に LINE 通知を送る。
"""

import os
import json
import time
import re
import requests
from pathlib import Path
from datetime import datetime, date
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
TIMESHIFT_API = f"{BASE_URL}/api/reserve/timeshift"
CALCFEE_API = f"{BASE_URL}/api/reserve/calcfee"
FACILITY_ID = os.environ.get("MIWA_FACILITY_ID", "")

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
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 自動予約する枠の設定（時刻コード → オプション設定）
SLOT_OPTIONS = {
    "1100": {
        "name": "昼枠（11:00〜15:00）",
        "options": {
            0: (True, 8),   # 21時までの利用人数: 8名
            1: (False, 0),
            2: (False, 0),
        },
    },
    "1700": {
        "name": "夜枠（17:00〜翌9:00）",
        "options": {
            0: (True, 8),   # 21時までの利用人数: 8名
            1: (True, 4),   # 21時以降の利用人数: 4名
            2: (True, 1),   # チェックイン予定時間: 1 = 17:00〜19:00
        },
    },
}


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
# カレンダーチェック
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
    """カレンダーAPIから各日の予約状況を取得する"""
    results = {}
    for year, month in get_months_to_check():
        try:
            resp = session.get(
                CALENDAR_API,
                params={"year": year, "month": month, "id": FACILITY_ID},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  ⚠️ カレンダー取得エラー ({year}/{month}): {e}")
            continue

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
# タイムシフトから予約可能スロットを抽出
# ============================================================
def get_available_slots(session: requests.Session, date_str: str) -> list:
    """タイムシフトAPIから先着予約可能なスロットのURLを取得する。
    戻り値: [{"facility_id": "100371", "datetime": "202604201100", "kbn": "0", "time": "1100"}, ...]
    """
    yyyymmdd = date_str.replace("-", "")
    try:
        resp = session.get(
            TIMESHIFT_API,
            params={"date": yyyymmdd, "id": FACILITY_ID},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠️ タイムシフト取得エラー ({date_str}): {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # available（先着予約可能）クラスのリンクを収集（lottery/unavailable は除外）
    seen = set()
    slots = []
    for li in soup.find_all("li"):
        classes = " ".join(li.get("class", []))
        if "available" not in classes:
            continue
        if "lottery" in classes or "unavailable" in classes:
            continue

        a = li.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        if href in seen:
            continue
        seen.add(href)

        m = re.search(r"/reserve/register/(\d+)/detail/\?datetime=(\d+)&(?:amp;)?kbn=(\d+)", href)
        if m:
            slot_info = {
                "facility_id": m.group(1),
                "datetime": m.group(2),
                "kbn": m.group(3),
                "time": m.group(2)[-4:],  # "1100" or "1700"
            }
            # 対象の枠のみ
            if slot_info["time"] in SLOT_OPTIONS:
                slots.append(slot_info)

    return slots


# ============================================================
# 自動予約
# ============================================================
def book_slot(session: requests.Session, slot: dict) -> bool:
    """1スロットの予約を実行する"""
    fac_id = slot["facility_id"]
    dt = slot["datetime"]
    kbn = slot["kbn"]
    time_code = slot["time"]
    opt_config = SLOT_OPTIONS[time_code]
    name = opt_config["name"]

    print(f"    [{name}] 予約開始 (facility={fac_id}, datetime={dt}, kbn={kbn})")

    # Step 1: 詳細ページ取得
    detail_url = f"{BASE_URL}/reserve/register/{fac_id}/detail/?datetime={dt}&kbn={kbn}"
    resp = session.get(detail_url, timeout=15)
    if resp.status_code != 200:
        print(f"    ⚠️ 詳細ページ取得失敗: {resp.status_code}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "_token"})
    if not token_input:
        print(f"    ⚠️ CSRFトークンが見つかりません")
        return False
    csrf = token_input["value"]

    # オプションIDを動的に取得
    option_values = {}
    for i in range(10):
        inp = soup.find("input", {"name": f"option_id_{i}"})
        if not inp:
            break
        option_values[i] = inp.get("value", "")

    # Step 1.5: 料金API
    fee_resp = session.get(CALCFEE_API, params={
        "id": fac_id, "kbn": kbn,
        "datetime": dt, "reserve_number": "1",
    }, timeout=15)
    fee_data = fee_resp.json()
    cost = fee_data.get("cost", 0)

    # Step 2: POST → 確認ページ
    form_data = [
        ("_token", csrf),
        ("reserve_kbn", kbn),
        ("datetime", dt),
        ("reserve_number", "1"),
        ("reserve_fee", str(cost)),
    ]
    for i in sorted(option_values.keys()):
        checked, num = opt_config["options"].get(i, (False, 0))
        if checked:
            form_data.append((f"option_id_{i}", option_values[i]))
        form_data.append(("num[]", str(num) if checked else ""))
        form_data.append((f"price_{i}", "0"))
    form_data.append(("sum_cost", str(cost)))
    form_data.append(("check_term", "1"))

    resp = session.post(
        f"{BASE_URL}/reserve/register/{fac_id}/confirm",
        data=form_data,
        timeout=15,
    )
    if resp.status_code != 200 or "save" not in resp.text:
        print(f"    ⚠️ 確認ページ失敗（既に埋まった可能性）")
        err_soup = BeautifulSoup(resp.text, "html.parser")
        for err in err_soup.select(".error, .alert, .warning"):
            print(f"      → {err.get_text(strip=True)}")
        return False

    # Step 3: GET → 予約確定
    resp = session.get(
        f"{BASE_URL}/reserve/register/{fac_id}/save",
        params={"action": ""},
        timeout=15,
    )
    if resp.status_code == 200:
        print(f"    ✅ {name} 予約完了！（¥{cost:,}）")
        return True
    else:
        print(f"    ❌ 予約失敗: {resp.status_code}")
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
        f"{BASE_URL}/reserve/list"
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
        f"{BASE_URL}/reserve/register/{FACILITY_ID}"
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

        # タイムシフトから予約可能スロットを取得
        slots = get_available_slots(session, d)
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
                    print(f"    [{SLOT_OPTIONS[slot['time']]['name']}] 予約済みのためスキップ")
                    continue

                ok = book_slot(session, slot)
                if ok:
                    booked_list.append(slot_key)
                    booked_names.append(SLOT_OPTIONS[slot["time"]]["name"])

            if booked_names:
                send_booked_notification(d, booked_names)
        else:
            # 平日 → 自動予約せず通知のみ
            print(f"\n  🟢 {d}（{weekday_ja}）の空きを検知！平日のため通知のみ")
            slot_names = [SLOT_OPTIONS[s["time"]]["name"] for s in slots]
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

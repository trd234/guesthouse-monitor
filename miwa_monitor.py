#!/usr/bin/env python3
"""
MIWA LOCK 共用施設（ゲストハウス）キャンセル空き監視スクリプト
GitHub Actions で5分ごとに起動 → 内部で1分ごとに5回チェック = 実質1分間隔

カレンダーAPIを叩いて「reserved → available」に変わった日を検知し、LINE通知する。
"""

import os
import json
import time
import re
import requests
from pathlib import Path
from datetime import datetime, date

# ============================================================
# 監視対象
# ============================================================
BASE_URL = "https://mitagh.miwalinks.jp"
LOGIN_URL = f"{BASE_URL}/login"
CALENDAR_API = f"{BASE_URL}/api/reserve/calendar"
FACILITY_ID = "100310"
FACILITY_NAME = "VILLA／1階 GUEST HOUSE"
RESERVE_PAGE_URL = f"{BASE_URL}/reserve/register/{FACILITY_ID}"

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


# ============================================================
# 状態の読み書き
# ============================================================
def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# ログイン
# ============================================================
def create_session() -> requests.Session:
    """ログインしてセッションを返す"""
    user_id = os.environ.get("MIWA_USER_ID", "")
    password = os.environ.get("MIWA_PASSWORD", "")
    if not user_id or not password:
        print("  ⚠️ MIWA_USER_ID / MIWA_PASSWORD が未設定です")
        return None

    session = requests.Session()
    session.headers.update(HEADERS)

    # ログインページからCSRFトークンを取得
    resp = session.get(LOGIN_URL, timeout=15)
    resp.raise_for_status()
    match = re.search(r'name="_token"\s+value="([^"]+)"', resp.text)
    if not match:
        print("  ⚠️ CSRFトークンが取得できません")
        return None
    csrf_token = match.group(1)

    # ログイン
    resp = session.post(LOGIN_URL, data={
        "_token": csrf_token,
        "email": user_id,
        "password": password,
    }, timeout=15)
    resp.raise_for_status()

    if "/login" in resp.url:
        print("  ⚠️ ログイン失敗（リダイレクトされませんでした）")
        return None

    print("  ログイン成功")
    return session


# ============================================================
# カレンダーチェック
# ============================================================
def get_months_to_check() -> list:
    """当月から MONTHS_AHEAD ヶ月先までの (year, month) リストを返す"""
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
    """カレンダーAPIから各日の予約状況を取得する。
    戻り値: {"2026-03-15": "reserved", "2026-05-03": "available", ...}
    """
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

        # 各日のステータスをパース: <td class="STATUS"><a class="link_area">DAY</a></td>
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
# LINE通知
# ============================================================
def send_line_notification(new_available_dates: list):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("  ⚠️ LINE_CHANNEL_ACCESS_TOKEN が未設定です")
        return

    dates_text = "\n".join(f"  ・{d}" for d in sorted(new_available_dates))
    message = (
        f"🏨【ゲストハウス 空き検知】\n"
        f"{FACILITY_NAME}\n"
        f"キャンセルにより予約可能な日が出ました！\n"
        f"\n"
        f"{dates_text}\n"
        f"\n"
        f"先着順です。お早めに👇\n"
        f"{RESERVE_PAGE_URL}"
    )

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


# ============================================================
# メイン処理
# ============================================================
def run_once(session: requests.Session, prev_state: dict) -> dict:
    current = check_calendar(session)
    if not current:
        return prev_state  # 取得失敗時は状態を変更しない

    # 状況サマリ
    available_dates = [d for d, s in current.items() if s == "available"]
    reserved_dates = [d for d, s in current.items() if s == "reserved"]
    print(f"  予約済み: {len(reserved_dates)}日 / 先着予約可能: {len(available_dates)}日")

    if available_dates:
        for d in sorted(available_dates):
            print(f"  🟢 {d} 先着予約可能")

    # 新たに available になった日を検知（前回は available でなかった日）
    new_available = []
    for d in available_dates:
        prev_status = prev_state.get(d)
        if prev_status != "available":
            new_available.append(d)

    if new_available:
        print(f"  🎉 新たに {len(new_available)} 日の空きを検知！LINE通知を送ります")
        send_line_notification(new_available)
    else:
        # available が埋まった日を検知
        lost = [d for d, s in prev_state.items() if s == "available" and current.get(d) != "available"]
        for d in sorted(lost):
            print(f"  [{d}] 予約が埋まりました。次回空き時に再通知します")

    return current


def main():
    print(f"\n{'='*50}")
    print(f"ゲストハウス空き監視開始：{LOOP_COUNT}回 × {LOOP_INTERVAL_SEC}秒")
    print(f"施設: {FACILITY_NAME}")
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

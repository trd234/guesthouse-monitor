#!/usr/bin/env python3
"""
MIWA LOCK ゲストハウス 抽選自動申込スクリプト
毎日1回実行 → 60日後が土日祝なら昼枠・夜枠の抽選申込を自動で行う

予約フロー:
  1. GET  /reserve/register/100371/detail/?datetime=...&kbn=1  → CSRFトークン取得
  2. POST /reserve/register/100371/confirm                      → 予約内容確認
  3. GET  /reserve/register/100371/save                          → 予約確定
"""

import os
import re
import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup

try:
    import jpholiday
except ImportError:
    jpholiday = None
    print("⚠️ jpholiday 未インストール（祝日判定なし、土日のみ対象）")

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://mitagh.miwalinks.jp"
LOGIN_URL = f"{BASE_URL}/login"
FACILITY_ID = "100371"  # 昼枠・夜枠片方ご利用
CALCFEE_API = f"{BASE_URL}/api/reserve/calcfee"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 抽選申込する枠
SLOTS = [
    {
        "name": "昼枠（11:00〜15:00）",
        "time": "1100",
        "options": {
            # index: (チェックするか, 数量)
            0: (True, 8),   # 21時までの利用人数: 8名
            1: (False, 0),  # 21時以降の利用人数: 不要
            2: (False, 0),  # チェックイン予定時間: 不要
        },
    },
    {
        "name": "夜枠（17:00〜翌9:00）",
        "time": "1700",
        "options": {
            0: (True, 8),   # 21時までの利用人数: 8名
            1: (True, 4),   # 21時以降の利用人数: 4名
            2: (True, 1),   # チェックイン予定時間: 1 = 17:00〜19:00
        },
    },
]

# 60日後を対象（抽選受付開始日）
DAYS_AHEAD = 60


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
# ログイン
# ============================================================
def login(session: requests.Session) -> bool:
    user_id = os.environ.get("MIWA_USER_ID", "")
    password = os.environ.get("MIWA_PASSWORD", "")
    if not user_id or not password:
        print("⚠️ MIWA_USER_ID / MIWA_PASSWORD が未設定です")
        return False

    resp = session.get(LOGIN_URL, timeout=15)
    resp.raise_for_status()
    match = re.search(r'name="_token"\s+value="([^"]+)"', resp.text)
    if not match:
        print("⚠️ CSRFトークンが取得できません")
        return False

    resp = session.post(LOGIN_URL, data={
        "_token": match.group(1),
        "email": user_id,
        "password": password,
    }, timeout=15)

    if "/login" in resp.url:
        print("⚠️ ログイン失敗")
        return False

    print("ログイン成功")
    return True


# ============================================================
# 1枠分の抽選申込
# ============================================================
def apply_lottery(session: requests.Session, target_date: date, slot: dict) -> bool:
    datetime_str = target_date.strftime("%Y%m%d") + slot["time"]
    name = slot["name"]
    print(f"\n  [{name}] 申込開始 (datetime={datetime_str})")

    # --- Step 1: 詳細ページ取得 → CSRFトークン・オプションID ---
    detail_url = f"{BASE_URL}/reserve/register/{FACILITY_ID}/detail/?datetime={datetime_str}&kbn=1"
    resp = session.get(detail_url, timeout=15)
    if resp.status_code != 200:
        print(f"  ⚠️ 詳細ページ取得失敗: {resp.status_code}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "_token"})
    if not token_input:
        print("  ⚠️ CSRFトークンが見つかりません")
        return False
    csrf = token_input["value"]

    # オプションIDを動的に取得
    option_values = {}
    for i in range(10):
        inp = soup.find("input", {"name": f"option_id_{i}"})
        if not inp:
            break
        option_values[i] = inp.get("value", "")

    # --- Step 1.5: 料金API ---
    fee_resp = session.get(CALCFEE_API, params={
        "id": FACILITY_ID, "kbn": "1",
        "datetime": datetime_str, "reserve_number": "1",
    }, timeout=15)
    fee_data = fee_resp.json()
    cost = fee_data.get("cost", 0)
    print(f"  料金: ¥{cost:,}")

    # --- Step 2: POST → 確認ページ ---
    form_data = [
        ("_token", csrf),
        ("reserve_kbn", "1"),
        ("datetime", datetime_str),
        ("reserve_number", "1"),
        ("reserve_fee", str(cost)),
    ]

    for i in sorted(option_values.keys()):
        checked, num = slot["options"].get(i, (False, 0))
        if checked:
            form_data.append((f"option_id_{i}", option_values[i]))
        form_data.append(("num[]", str(num) if checked else ""))
        form_data.append((f"price_{i}", "0"))

    form_data.append(("sum_cost", str(cost)))
    form_data.append(("check_term", "1"))

    resp = session.post(
        f"{BASE_URL}/reserve/register/{FACILITY_ID}/confirm",
        data=form_data,
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"  ⚠️ 確認ページ取得失敗: {resp.status_code}")
        return False

    # 確認ページに「save」リンクがあるか検証
    if "save" not in resp.text:
        print(f"  ⚠️ 確認ページにsaveリンクがありません（入力エラーの可能性）")
        # エラーメッセージがあれば表示
        err_soup = BeautifulSoup(resp.text, "html.parser")
        for err in err_soup.select(".error, .alert, .warning"):
            print(f"    → {err.get_text(strip=True)}")
        return False

    print(f"  確認ページOK")

    # --- Step 3: GET → 予約確定 ---
    resp = session.get(
        f"{BASE_URL}/reserve/register/{FACILITY_ID}/save",
        params={"action": ""},
        timeout=15,
    )

    if resp.status_code == 200:
        print(f"  ✅ {name} 抽選申込完了！")
        return True
    else:
        print(f"  ❌ 申込失敗: {resp.status_code}")
        return False


# ============================================================
# メイン
# ============================================================
def main():
    target = date.today() + timedelta(days=DAYS_AHEAD)
    weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][target.weekday()]

    print(f"{'='*50}")
    print(f"ゲストハウス抽選自動申込")
    print(f"対象日: {target}（{weekday_ja}）")
    print(f"{'='*50}")

    if not is_weekend_or_holiday(target):
        print(f"→ 平日のためスキップ")
        return

    holiday_name = ""
    if jpholiday:
        h = jpholiday.is_holiday_name(target)
        if h:
            holiday_name = f"（{h}）"

    print(f"→ 土日祝{holiday_name}のため抽選申込を実行")

    session = requests.Session()
    session.headers.update(HEADERS)

    if not login(session):
        return

    results = []
    for slot in SLOTS:
        ok = apply_lottery(session, target, slot)
        results.append((slot["name"], ok))

    print(f"\n{'='*50}")
    print("結果:")
    for name, ok in results:
        status = "✅ 成功" if ok else "❌ 失敗"
        print(f"  {name}: {status}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
共用施設予約サイト 抽選自動申込スクリプト（Livewire v3 対応版）
毎日1回実行 → 60日後が土日祝なら昼枠・夜枠の抽選申込を自動で行う

予約フロー（Livewire ベース）:
  1. GET  /facilities/{id}      → wire:snapshot 取得
  2. POST /livewire/update      → 日付選択
  3. POST /livewire/update      → オプション・日時設定
  4. POST /livewire/update      → next メソッド呼び出し → 確認ページへ
  5. GET/POST 確認ページ        → 申込確定
"""

import os
import json
import re
import html
import urllib.parse
import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup

try:
    import jpholiday
except ImportError:
    jpholiday = None
    print("⚠️ jpholiday 未インストール（祝日判定なし、土日のみ対象）")

# ============================================================
# 設定（マンション固有の値は環境変数から取得）
# ============================================================
BASE_URL = os.environ.get("MIWA_BASE_URL", "")
LOGIN_URL = f"{BASE_URL}/login"
LIVEWIRE_UPDATE = f"{BASE_URL}/livewire/update"

FACILITY_ID_RESERVE = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")  # 100371

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 抽選申込する枠
# option_id: Livewire の selectOptionQuantities.{option_id} に対応
SLOTS = [
    {
        "name": "昼枠（11:00〜15:00）",
        "time": "1100",
        "options": {
            46: 8,   # 21時までの利用人数: 8名
            49: 0,   # 21時以降の利用人数: 不要
            61: 0,   # チェックイン予定時間: 不要
        },
    },
    {
        "name": "夜枠（17:00〜翌9:00）",
        "time": "1700",
        "options": {
            46: 8,   # 21時までの利用人数: 8名
            49: 4,   # 21時以降の利用人数: 4名
            61: 1,   # チェックイン予定時間: 1 = 17:00〜19:00
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
def create_session() -> requests.Session:
    user_id = os.environ.get("MIWA_USER_ID", "")
    password = os.environ.get("MIWA_PASSWORD", "")
    if not user_id or not password:
        print("⚠️ MIWA_USER_ID / MIWA_PASSWORD が未設定です")
        return None

    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(LOGIN_URL, timeout=15)
    resp.raise_for_status()
    match = re.search(r'name="_token"\s+value="([^"]+)"', resp.text)
    if not match:
        print("⚠️ CSRFトークンが取得できません")
        return None

    resp = session.post(LOGIN_URL, data={
        "_token": match.group(1),
        "email": user_id,
        "password": password,
    }, timeout=15)
    resp.raise_for_status()

    if "/login" in resp.url:
        print("⚠️ ログイン失敗")
        return None

    print("ログイン成功")
    return session


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
                xsrf_raw = next(
                    (c.value for c in reversed(list(session.cookies)) if c.name == "XSRF-TOKEN"),
                    ""
                )
                xsrf = urllib.parse.unquote(xsrf_raw)
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
        print(f"  ⚠️ Livewire エラー: {resp.status_code}")
        return None, None, {}

    # XSRF トークンを更新
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
                xsrf_raw = next(
                    (c.value for c in reversed(list(session.cookies)) if c.name == "XSRF-TOKEN"),
                    ""
                )
                xsrf = urllib.parse.unquote(xsrf_raw)
                for method in ["confirm", "save", "complete", "submit"]:
                    snap_str2, data2, effects2 = livewire_call(
                        session, decoded, xsrf,
                        calls=[{"method": method, "params": []}],
                    )
                    if snap_str2 and not effects2.get("errors"):
                        print(f"    ✅ 申込確定完了！")
                        return True
        except (json.JSONDecodeError, KeyError):
            continue

    # スナップショットが見つからない場合、ページに /reserves/{id} があれば成功
    if "/reserves/" in resp.url or "/reserves/" in resp.text:
        print(f"    ✅ 申込完了！（確認ページ: {resp.url}）")
        return True

    print(f"    ⚠️ 確認処理が不明: {url}")
    return False


# ============================================================
# 1枠分の抽選申込（Livewire ベース）
# ============================================================
def apply_lottery(session: requests.Session, target_date: date, slot: dict) -> bool:
    date_str = target_date.strftime("%Y-%m-%d")
    time_code = slot["time"]
    name = slot["name"]
    hour = int(time_code[:2])
    minute = int(time_code[2:])
    start_dt = f"{date_str}T{hour:02d}:{minute:02d}:00+09:00"

    print(f"\n  [{name}] 申込開始 (date={date_str}, start={start_dt})")

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
        print(f"  ⚠️ 日付選択失敗")
        return False

    # 選択後の予約区分を確認（デバッグ用）
    reserve_kbn = data.get("selectReserveKbn", data.get("reserveKbn", "不明"))
    print(f"  予約区分: {reserve_kbn}")

    # Step 3: 全オプションと日時をセット
    # 抽選（kbn=1）の場合、Livewire コンポーネントは日付選択時に
    # 自動的に抽選モードへ切り替わるため、selectStartDate 指定のみで十分な場合がある。
    # 念のため selectReserveKbn も送る（コンポーネントに当該プロパティがなければ無視される）。
    updates = {
        "selectStartDate": date_str,
        "selectStartDateTime": start_dt,
        "selectReserveNumber": 1,
    }
    for opt_id, value in slot["options"].items():
        updates[f"selectOptionQuantities.{opt_id}"] = value

    snap_str, data, _ = livewire_call(
        session, snap_str, xsrf,
        updates=updates,
    )
    if not snap_str:
        print(f"  ⚠️ オプション設定失敗")
        return False

    # Step 4: next を呼び出して申込を進める
    snap_str, data, effects = livewire_call(
        session, snap_str, xsrf,
        calls=[{"method": "next", "params": []}],
    )
    if not snap_str:
        print(f"  ⚠️ 申込送信失敗（受付期間外・既申込の可能性）")
        return False

    # リダイレクトがあれば確認ページへ
    redirect_url = effects.get("redirect")
    if redirect_url:
        print(f"  確認ページへ移動: {redirect_url}")
        return _handle_confirm_page(session, redirect_url)

    # エラーがあれば表示
    errors = effects.get("errors", [])
    if errors:
        print(f"  ⚠️ バリデーションエラー: {errors}")
        return False

    # リダイレクトなし・エラーなし → 申込完了と判断
    print(f"  ✅ {name} 抽選申込完了！")
    return True


# ============================================================
# メイン
# ============================================================
def main():
    if not BASE_URL or not FACILITY_ID_RESERVE:
        print("⚠️ MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE が未設定です")
        return

    target = date.today() + timedelta(days=DAYS_AHEAD)
    weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][target.weekday()]

    print(f"{'='*50}")
    print(f"共用施設 抽選自動申込")
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

    session = create_session()
    if not session:
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

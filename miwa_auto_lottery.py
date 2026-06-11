#!/usr/bin/env python3
"""
共用施設予約サイト 抽選自動申込スクリプト（Livewire v3 対応版 2）
毎日1回実行 → 60日後が土日祝なら昼枠・夜枠の抽選申込を自動で行う

予約フロー（Livewire ベース・診断で確認済み）:
  1. GET  /facilities/{id}      → facility-detail の wire:snapshot 取得
  2. POST /livewire/update      → 日付選択（selectStartDate）
  3. POST /livewire/update      → 日時・人数・オプション設定
  4. POST /livewire/update      → next メソッド → 確認ページへ redirect
  5. 確認ページで確定操作        → /reserves/{id} 到達で完了確定
"""

import os
import json
import re
import html
import time
import urllib.parse
import requests
from datetime import datetime, date, timedelta

try:
    import jpholiday
except ImportError:
    jpholiday = None
    print("⚠️ jpholiday 未インストール（祝日判定なし、土日のみ対象）")

# ============================================================
# 設定（マンション固有の値は環境変数から取得）
# ============================================================
BASE_URL = os.environ.get("MIWA_BASE_URL", "").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login"
LIVEWIRE_UPDATE = f"{BASE_URL}/livewire/update"

FACILITY_ID_RESERVE = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")
FACILITY_URL = f"{BASE_URL}/facilities/{FACILITY_ID_RESERVE}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# 抽選申込する枠（option_id は診断で確認済み）
#   46 = 21時までの利用人数（0〜8）
#   49 = 21時以降の利用人数（0〜4）
#   61 = 夜枠用チェックイン予定時間（0=未選択 / 1=17:00〜19:00 ...）
SLOTS = [
    {"name": "昼枠（11:00〜15:00）", "time": "1100", "options": {46: 8, 49: 0, 61: 0}},
    {"name": "夜枠（17:00〜翌9:00）", "time": "1700", "options": {46: 8, 49: 4, 61: 1}},
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
        print("⚠️ ログイン失敗（ID/パスワードをご確認ください）")
        return None

    print("ログイン成功")
    return session


# ============================================================
# Livewire API ヘルパー
# ============================================================
def extract_facility_snapshot(session: requests.Session) -> tuple:
    """施設詳細ページから facility-detail の wire:snapshot を取得する。"""
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
    if isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict) and v[1].get("s") == "arr":
        return v[0]
    return v


def _carbon_to_str(item):
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


def find_start_datetime(data: dict, time_code: str) -> str:
    """選択日の startDateTimes から、指定枠（昼=11時/夜=17時）の実際の開始日時を探す。
    見つからなければ None。"""
    target_hour = 11 if time_code == "1100" else 17
    raw = _unwrap_arr(data.get("startDateTimes", []))
    items = list(raw.keys()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    for item in items:
        dt_str = _carbon_to_str(item)
        if not dt_str:
            continue
        try:
            hour = datetime.fromisoformat(dt_str).hour
        except ValueError:
            m = re.search(r'\b(\d{1,2}):(\d{2})\b', dt_str)
            hour = int(m.group(1)) if m else None
        if hour == target_hour:
            return dt_str
    return None


# ============================================================
# 確認ページの確定処理
# ============================================================
def _confirm_and_verify(session, url: str) -> bool:
    """確認ページで確定操作を行い、/reserves/{id} に到達できたら True。"""
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    ⚠️ 確認ページ取得エラー: {e}")
        return False

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

        # 確定系メソッドを順に試し、/reserves/{id} 到達で成功確定
        for method in ["apply", "reserve", "complete", "confirm", "submit",
                       "save", "next", "store", "applyLottery", "reserveLottery"]:
            snap2, _, effects2 = livewire_call(session, use_snap, xsrf,
                                               calls=[{"method": method, "params": []}])
            if snap2 is None:
                continue
            redirect2 = effects2.get("redirect")
            if redirect2:
                try:
                    resp3 = session.get(redirect2, timeout=15)
                    if re.search(r"/reserves/\d+", resp3.url) or re.search(r"/reserves/\d+", resp3.text):
                        print(f"    ✅ 申込確定完了！（method={method}）")
                        return True
                except requests.RequestException:
                    pass

    print(f"    ⚠️ 確認ページの確定に至りませんでした: {url}")
    return False


# ============================================================
# 1枠分の抽選申込（Livewire ベース）
# ============================================================
def apply_lottery(session, target_date: date, slot: dict) -> str:
    """戻り値: "applied"（確定） / "partial"（未確定） / "failed"。"""
    date_str = target_date.strftime("%Y-%m-%d")
    time_code = slot["time"]
    name = slot["name"]
    print(f"\n  [{name}] 申込開始 (date={date_str})")

    snap_str, xsrf = extract_facility_snapshot(session)
    if not snap_str:
        return "failed"

    # 日付を選択
    snap_str, data, _ = livewire_call(session, snap_str, xsrf, updates={"selectStartDate": date_str})
    if not snap_str:
        print("  ⚠️ 日付選択失敗")
        return "failed"

    reserve_kbn = data.get("reserveKbn", "不明")
    print(f"  予約区分: {reserve_kbn}")

    # 実際に提示されている開始日時を取得（無ければ固定値にフォールバック）
    start_dt = find_start_datetime(data, time_code)
    if not start_dt:
        hour = int(time_code[:2])
        start_dt = f"{date_str}T{hour:02d}:00:00+09:00"
        print(f"  （startDateTimes に該当枠なし → 固定値 {start_dt} で試行）")
    else:
        print(f"  開始日時: {start_dt}")

    # 日時・人数・オプションをまとめて設定
    updates = {
        "selectStartDate": date_str,
        "selectStartDateTime": start_dt,
        "selectReserveNumber": 1,
    }
    for opt_id, value in slot["options"].items():
        updates[f"selectOptionQuantities.{opt_id}"] = value
    snap_str, data, _ = livewire_call(session, snap_str, xsrf, updates=updates)
    if not snap_str:
        print("  ⚠️ オプション設定失敗")
        return "failed"

    # next を呼び出して確認ステップへ
    snap_str, data, effects = livewire_call(session, snap_str, xsrf, calls=[{"method": "next", "params": []}])
    if not snap_str:
        print("  ⚠️ 申込送信失敗（受付期間外・既申込の可能性）")
        return "failed"

    errors = effects.get("errors")
    if errors:
        print(f"  ⚠️ バリデーションエラー: {errors}")
        return "failed"

    redirect_url = effects.get("redirect")
    if redirect_url:
        print(f"  確認ページへ移動: {redirect_url}")
        return "applied" if _confirm_and_verify(session, redirect_url) else "partial"

    print("  ⚠️ 確認ページへ進めませんでした（未確定）")
    return "partial"


# ============================================================
# LINE通知（任意：トークンがあれば結果を通知）
# ============================================================
def _send_line_message(message: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
    except Exception as e:
        print(f"  ❌ LINE通知エラー: {e}")


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
        print("→ 平日のためスキップ")
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
        outcome = apply_lottery(session, target, slot)
        results.append((slot["name"], outcome))
        time.sleep(1)

    print(f"\n{'='*50}")
    print("結果:")
    label = {"applied": "✅ 申込完了", "partial": "🟠 要手動確認", "failed": "❌ 失敗"}
    lines = []
    for name, outcome in results:
        print(f"  {name}: {label.get(outcome, outcome)}")
        lines.append(f"  ・{name}: {label.get(outcome, outcome)}")
    print(f"{'='*50}")

    # 1枠でも完了/要確認があれば通知
    if any(o in ("applied", "partial") for _, o in results):
        body = "\n".join(lines)
        _send_line_message(
            f"🎫【共用施設 抽選自動申込】\n対象日: {target}（{weekday_ja}）\n\n{body}\n\n"
            f"申込状況の確認👇\n{BASE_URL}/reserves"
        )


if __name__ == "__main__":
    main()

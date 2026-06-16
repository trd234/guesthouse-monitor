#!/usr/bin/env python3
"""
診断スクリプト（予約は一切しない・読み取り専用）
予約サイトの実際のページ構造をログに出力して、
  - GUEST HOUSE / 「片方・連続」の選び方
  - 日付・時刻・人数・チェックイン時間の項目名（wire:model）
  - 「予約内容を確認する」「抽選予約する」ボタンのメソッド名（wire:click）
  - 人数/チェックイン時間プルダウンの選択肢
を確認するためのもの。申込ボタンは押さない。

★強化点（v2）:
  施設ページで「対象日を選択した後」にサイトが返す中身（再描画HTML・
  Livewire データ構造）を丸ごと出力する。予約枠の時間帯・人数プルダウンは
  日付選択後に初めて画面へ出てくるため、ここを見ないと本当の項目名・
  メソッド名が分からない。selectStartDate を送るだけで、next/apply など
  確定系メソッドは一切呼ばない（＝予約は発生しない）。
"""

import os
import json
import re
import html
import urllib.parse
import requests
from datetime import datetime, date, timedelta
from bs4 import BeautifulSoup

try:
    import jpholiday
except ImportError:
    jpholiday = None

BASE_URL = os.environ.get("MIWA_BASE_URL", "").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login"
LIVEWIRE_UPDATE = f"{BASE_URL}/livewire/update"
FACILITY_ID = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")
FACILITY_URL = f"{BASE_URL}/facilities/{FACILITY_ID}"

DAYS_AHEAD = 60  # 本番（抽選）と同じ対象日の決め方

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def line(title=""):
    print("\n" + "=" * 60)
    if title:
        print(title)
        print("=" * 60)


def login():
    user_id = os.environ.get("MIWA_USER_ID", "")
    password = os.environ.get("MIWA_PASSWORD", "")
    if not user_id or not password:
        print("⚠️ 認証情報が未設定です")
        return None
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(LOGIN_URL, timeout=15)
    resp.raise_for_status()
    m = re.search(r'name="_token"\s+value="([^"]+)"', resp.text)
    if not m:
        print("⚠️ CSRFトークン取得失敗")
        return None
    resp = session.post(LOGIN_URL, data={
        "_token": m.group(1), "email": user_id, "password": password,
    }, timeout=15)
    resp.raise_for_status()
    if "/login" in resp.url:
        print("⚠️ ログイン失敗")
        return None
    print("ログイン成功")
    return session


def short(text, n=60):
    t = " ".join((text or "").split())
    return t[:n]


# ------------------------------------------------------------
# Livewire ヘルパー（抽選スクリプトと同じ仕組み・読み取りのみ）
# ------------------------------------------------------------
def _iter_xsrf_values(jar):
    return [c.value for c in jar if c.name == "XSRF-TOKEN"]


def _get_xsrf_token(session):
    values = _iter_xsrf_values(session.cookies)
    return urllib.parse.unquote(values[-1]) if values else ""


def _set_xsrf_token(session, raw_value):
    for cookie in list(session.cookies):
        if cookie.name == "XSRF-TOKEN":
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
    session.cookies.set("XSRF-TOKEN", raw_value)


def extract_facility_snapshot(session):
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


def livewire_call(session, snap_str, xsrf, updates=None, calls=None):
    """Livewire update を呼ぶ。戻り値: (new_snap_str, new_data, effects)。"""
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
            LIVEWIRE_UPDATE, json=body,
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
        print(f"  ⚠️ Livewire エラー: {resp.status_code} {resp.text[:300]}")
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


# ------------------------------------------------------------
# 構造ダンプ
# ------------------------------------------------------------
def dump_wire_attrs(soup, label="wire: 属性（メソッド名・プロパティ名）"):
    print(f"\n--- {label} ---")
    seen = set()
    found = False
    for el in soup.find_all(True):
        for k, v in el.attrs.items():
            if k.startswith("wire:"):
                txt = short(el.get_text())
                key = (k, str(v), txt)
                if key in seen:
                    continue
                seen.add(key)
                found = True
                print(f"  [{el.name}] {k} = {v!r}   テキスト: 「{txt}」")
    if not found:
        print("  （wire: 属性なし）")


def dump_links(soup):
    print("\n--- リンク（a要素）---")
    for a in soup.find_all("a"):
        txt = short(a.get_text())
        href = a.get("href", "")
        if txt or href:
            print(f"  「{txt}」 -> {href}")


def dump_buttons(soup):
    print("\n--- ボタン（button / submit）---")
    found = False
    for b in soup.find_all(["button"]):
        txt = short(b.get_text())
        wc = b.get("wire:click", "")
        typ = b.get("type", "")
        found = True
        print(f"  「{txt}」  wire:click={wc!r}  type={typ}")
    for inp in soup.find_all("input", {"type": ["submit", "button"]}):
        found = True
        print(f"  [input] value=「{short(inp.get('value',''))}」")
    if not found:
        print("  （ボタンなし）")


def dump_selects(soup):
    """プルダウン（人数・チェックイン時間）の項目名と選択肢を列挙する。"""
    print("\n--- プルダウン（select）---")
    found = False
    for sel in soup.find_all("select"):
        found = True
        model = ""
        for k, v in sel.attrs.items():
            if k.startswith("wire:model"):
                model = f"{k}={v}"
        name = sel.get("name", "")
        opts = []
        for o in sel.find_all("option"):
            opts.append(f"{o.get('value','')}:「{short(o.get_text(),20)}」")
        print(f"  {model or name}")
        print(f"     選択肢: {opts}")
    if not found:
        print("  （select なし）")


def dump_inputs(soup):
    """日付・人数などの input（radio/checkbox/number 等）の wire:model を列挙する。"""
    print("\n--- 入力欄（input） ---")
    found = False
    for inp in soup.find_all("input"):
        model = ""
        for k, v in inp.attrs.items():
            if k.startswith("wire:model"):
                model = f"{k}={v}"
        typ = inp.get("type", "")
        val = inp.get("value", "")
        if model:
            found = True
            print(f"  [{typ}] {model}  value={val!r}")
    if not found:
        print("  （wire:model 付き input なし）")


def dump_snapshots(text):
    print("\n--- Livewire スナップショット（コンポーネント）---")
    snaps = re.findall(r'wire:snapshot="((?:&[^;]+;|[^"])+)"', text)
    for s in snaps:
        try:
            d = json.loads(html.unescape(s))
        except (json.JSONDecodeError, ValueError):
            continue
        memo_name = d.get("memo", {}).get("name", "")
        data = d.get("data", {})
        keys = list(data.keys()) if isinstance(data, dict) else []
        print(f"  ● component: {memo_name}")
        print(f"     data keys: {keys}")
        for k in keys:
            if "ption" in k or "eserve" in k or "tartDate" in k or "kbn" in k.lower():
                val = data.get(k)
                print(f"       {k} = {json.dumps(val, ensure_ascii=False)[:200]}")


def dump_data_full(data, label):
    """Livewire data を項目ごとに（長すぎる分は省略しつつ）全部出す。"""
    print(f"\n--- {label}：data 全項目 ---")
    if not isinstance(data, dict):
        print(f"  data が dict ではありません: {type(data)}")
        return
    for k in data.keys():
        val = data.get(k)
        dumped = json.dumps(val, ensure_ascii=False)
        if len(dumped) > 600:
            dumped = dumped[:600] + f" …(全{len(dumped)}文字)"
        print(f"  {k} = {dumped}")


def dump_page(session, url, label):
    line(f"ページ取得: {label}  ({url})")
    try:
        resp = session.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  取得エラー: {e}")
        return None
    print(f"  status={resp.status_code}  final_url={resp.url}")
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text() if soup.title else ""
    print(f"  title: {short(title, 80)}")
    dump_snapshots(resp.text)
    dump_selects(soup)
    dump_buttons(soup)
    dump_wire_attrs(soup)
    dump_links(soup)
    return resp


# ------------------------------------------------------------
# ★ 日付選択後の構造を再現してダンプ（読み取りのみ）
# ------------------------------------------------------------
def is_weekend_or_holiday(d):
    if d.weekday() >= 5:
        return True
    if jpholiday and jpholiday.is_holiday(d):
        return True
    return False


# ------------------------------------------------------------
# Livewire データ構造のヘルパー（タイムライン枠の解析用）
# ------------------------------------------------------------
def _unwrap_arr(v):
    """Livewire の [値, {"s":"arr"}] / [値, {"s":"..."}] ラッパーを外す。"""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict) and "s" in v[1]:
        return v[0]
    return v


def _status_str(reservable_status):
    """reservableStatus（["reserved",{enm}] 形式 or 文字列）から状態文字列を取り出す。"""
    rs = _unwrap_arr(reservable_status)
    if isinstance(rs, str):
        return rs
    if isinstance(reservable_status, list) and reservable_status and isinstance(reservable_status[0], str):
        return reservable_status[0]
    return str(reservable_status)


def _carbon_str(v):
    v = _unwrap_arr(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], str):
        return v[0]
    return None


def extract_snapshots_from_html(text):
    """HTML文字列中の全 wire:snapshot を取り出し、[(name, snap_str, data), ...] を返す。"""
    out = []
    for s in re.findall(r'wire:snapshot="((?:&[^;]+;|[^"])+)"', text):
        decoded = html.unescape(s)
        try:
            d = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        name = d.get("memo", {}).get("name", "")
        out.append((name, decoded, d.get("data", {})))
    return out


def dump_timeline_slots(timeline_data, label="タイムライン枠"):
    """reserve-timeline の computed（枠リスト）を index/start/end/状態 で一覧表示し、
    予約可（reservable）な枠の index リストを返す。"""
    print(f"\n--- {label}（computed）---")
    reservable_indexes = []
    computed = _unwrap_arr(timeline_data.get("computed", []))
    if not isinstance(computed, list) or not computed:
        print("  （computed が空。＝この日付の枠が読み込まれていない可能性）")
        return reservable_indexes
    for raw_slot in computed:
        slot = _unwrap_arr(raw_slot)
        if not isinstance(slot, dict):
            continue
        idx = slot.get("index")
        start = slot.get("start") or _carbon_str(slot.get("orig_start"))
        end = slot.get("end") or _carbon_str(slot.get("orig_end"))
        status = _status_str(slot.get("reservableStatus"))
        kbn = slot.get("reserveKbn")
        mark = "  ★予約可" if status == "reservable" else ""
        print(f"  index={idx}  {start}〜{end}  status={status}  reserveKbn={kbn}{mark}")
        if status == "reservable":
            reservable_indexes.append(idx)
    return reservable_indexes


def get_timeline_snapshot(html_text):
    """再描画HTMLから reserve-timeline コンポーネントの (snap_str, data) を取り出す。"""
    for name, snap, data in extract_snapshots_from_html(html_text):
        if "reserve-timeline" in name:
            return snap, data
    return None, None


def diagnose_slot_flow(session, snap_str, xsrf, date_str, weekday_ja, label):
    """指定日を選択 → タイムライン枠を一覧 → 予約可枠を selectSlot で選んでみて、
    サイトが返すやり取り（dispatches / 親へ渡す値）を観察する。確定はしない。"""
    line(f"▼ {label}: {date_str}（{weekday_ja}）の枠を診断")

    # 対象月をカレンダーに読み込ませるため visibleMonth も一緒に送る
    target_month = date_str[:7]
    print(f"  → updates: visibleMonth={target_month}, selectStartDate={date_str}（確定なし）")
    snap2, data2, effects2 = livewire_call(
        session, snap_str, xsrf,
        updates={"visibleMonth": target_month, "selectStartDate": date_str})
    if snap2 is None:
        print("  → 日付選択の Livewire 呼び出しが失敗。上のエラーを参照。")
        return

    # 親側の重要項目
    print("\n--- 親(facility-detail) 選択後の重要項目 ---")
    for key in ("selectStartDate", "selectStartDateTime", "selectReserveNumber",
                "reserveKbn", "reservableStatus", "displayStartDateTime",
                "displayReserveNumber", "reserveCost"):
        val = data2.get(key) if isinstance(data2, dict) else None
        print(f"  {key} = {json.dumps(val, ensure_ascii=False)[:200]}")

    html_eff = effects2.get("html", "") if isinstance(effects2, dict) else ""
    if not html_eff:
        print("  （effects.html なし。枠の解析ができません）")
        return

    tl_snap, tl_data = get_timeline_snapshot(html_eff)
    if tl_snap is None:
        print("  （再描画HTMLに reserve-timeline が見つかりません）")
        return

    print(f"\n--- timeline.selectedDate = {tl_data.get('selectedDate')} ---")
    reservable = dump_timeline_slots(tl_data)

    # 予約可枠があればそれを、無ければ 11時/17時開始の枠を選んで挙動を観察
    target_index = None
    if reservable:
        target_index = reservable[0]
        print(f"\n  → 予約可枠あり。index={target_index} を selectSlot で選択して観察")
    else:
        computed = _unwrap_arr(tl_data.get("computed", []))
        for raw_slot in (computed if isinstance(computed, list) else []):
            slot = _unwrap_arr(raw_slot)
            if isinstance(slot, dict) and str(slot.get("start", "")).startswith(("11:", "17:")):
                target_index = slot.get("index")
                break
        if target_index is not None:
            print(f"\n  → 予約可枠なし。参考に index={target_index}（11時/17時枠）を選んで挙動だけ観察")
        else:
            print("\n  → 選択できる枠が見つからないため selectSlot は試しません")
            return

    # selectSlot を呼ぶ（＝UI上で枠を選ぶだけ。next/確定は呼ばないので予約は発生しない）
    fresh_xsrf = _get_xsrf_token(session)
    s_snap, s_data, s_effects = livewire_call(
        session, tl_snap, fresh_xsrf,
        calls=[{"method": "selectSlot", "params": [target_index]}])
    if s_snap is None:
        print("  → selectSlot 呼び出しが失敗。上のエラーを参照。")
        return

    print("\n--- selectSlot 後の timeline 状態 ---")
    for key in ("selectedStart", "selectedEnd", "clicked"):
        print(f"  {key} = {json.dumps(s_data.get(key), ensure_ascii=False)[:200]}")

    print("\n--- selectSlot の effects（★親へ渡すやり取り）---")
    if isinstance(s_effects, dict):
        print(f"  effects keys = {list(s_effects.keys())}")
        if s_effects.get("dispatches"):
            print(f"  dispatches = {json.dumps(s_effects.get('dispatches'), ensure_ascii=False)}")
        if s_effects.get("returns"):
            print(f"  returns = {json.dumps(s_effects.get('returns'), ensure_ascii=False)[:300]}")
        if s_effects.get("errors"):
            print(f"  errors = {json.dumps(s_effects.get('errors'), ensure_ascii=False)}")


def reproduce_date_selection(session):
    """新UI（タイムライン方式）の申込フローを読み取り専用で再現・観察する。
    確定系メソッド（next/apply/reserve 等）は一切呼ばないので予約は発生しない。"""
    snap_str, xsrf = extract_facility_snapshot(session)
    if not snap_str:
        line("★ 枠の診断")
        print("  → 施設スナップショットが取れないため中断")
        return
    init_data = json.loads(snap_str)["data"]

    # 1) 本番の対象日（60日後＝抽選対象）
    target = date.today() + timedelta(days=DAYS_AHEAD)
    weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][target.weekday()]
    diagnose_slot_flow(session, snap_str, xsrf, target.strftime("%Y-%m-%d"),
                       weekday_ja, "本番の対象日（60日後）")

    # 2) 直近で予約可能そうな日（カレンダーに既に出ている、unreservable でない最初の日）
    #    ＝予約可枠の実データと selectSlot の挙動を確実に観察するため
    soonest = None
    dates_raw = _unwrap_arr(init_data.get("dates", []))
    today = date.today()
    for raw_day in (dates_raw if isinstance(dates_raw, list) else []):
        day = _unwrap_arr(raw_day)
        if not isinstance(day, dict):
            continue
        ds = _carbon_str(day.get("date"))
        if not ds:
            continue
        try:
            d_obj = datetime.fromisoformat(ds).date()
        except ValueError:
            continue
        status = _status_str(day.get("reservableStatus"))
        if d_obj > today and status != "unreservable":
            soonest = d_obj
            break
    if soonest:
        # 別セッション状態を避けるため施設ページを取り直してから実施
        snap_str2, xsrf2 = extract_facility_snapshot(session)
        if snap_str2:
            wj = ["月", "火", "水", "木", "金", "土", "日"][soonest.weekday()]
            diagnose_slot_flow(session, snap_str2, xsrf2, soonest.strftime("%Y-%m-%d"),
                               wj, "直近の予約可能そうな日")
    else:
        print("\n（カレンダー上に unreservable 以外の日が見つかりませんでした）")


def main():
    if not BASE_URL or not FACILITY_ID:
        print("⚠️ MIWA_BASE_URL / MIWA_FACILITY_ID_RESERVE が未設定です")
        return
    session = login()
    if not session:
        return

    # 1) ログイン直後のトップ（施設一覧・GUEST HOUSE への入口を探す）
    dump_page(session, f"{BASE_URL}/", "トップ/ダッシュボード")

    # 2) 施設一覧ページ（片方/連続の選択がある可能性）
    dump_page(session, f"{BASE_URL}/facilities", "施設一覧")

    # 3) 目的の施設ページ（日付・人数・確認ボタンがある想定）
    dump_page(session, f"{BASE_URL}/facilities/{FACILITY_ID}", "施設詳細(片方/日付選択)")

    # 4) ★ 日付選択後の構造（本命）
    reproduce_date_selection(session)

    line("診断おわり")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
診断スクリプト（予約は一切しない・読み取り専用）
予約サイトの実際のページ構造をログに出力して、
  - GUEST HOUSE / 「片方・連続」の選び方
  - 日付・時刻・人数・チェックイン時間の項目名（wire:model）
  - 「予約内容を確認する」「抽選予約する」ボタンのメソッド名（wire:click）
  - 人数/チェックイン時間プルダウンの選択肢
を確認するためのもの。申込ボタンは押さない。
"""

import os
import json
import re
import html
import urllib.parse
import requests
from bs4 import BeautifulSoup

BASE_URL = os.environ.get("MIWA_BASE_URL", "")
LOGIN_URL = f"{BASE_URL}/login"
FACILITY_ID = os.environ.get("MIWA_FACILITY_ID_RESERVE", "")

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


def dump_wire_attrs(soup):
    """wire:click / wire:model 等を、要素のテキスト付きで列挙する。"""
    print("\n--- wire: 属性（メソッド名・プロパティ名）---")
    seen = set()
    for el in soup.find_all(True):
        for k, v in el.attrs.items():
            if k.startswith("wire:"):
                txt = short(el.get_text())
                key = (k, str(v), txt)
                if key in seen:
                    continue
                seen.add(key)
                print(f"  [{el.name}] {k} = {v!r}   テキスト: 「{txt}」")


def dump_links(soup):
    print("\n--- リンク（a要素）---")
    for a in soup.find_all("a"):
        txt = short(a.get_text())
        href = a.get("href", "")
        if txt or href:
            print(f"  「{txt}」 -> {href}")


def dump_buttons(soup):
    print("\n--- ボタン（button / submit）---")
    for b in soup.find_all(["button"]):
        txt = short(b.get_text())
        wc = b.get("wire:click", "")
        typ = b.get("type", "")
        print(f"  「{txt}」  wire:click={wc!r}  type={typ}")
    for inp in soup.find_all("input", {"type": ["submit", "button"]}):
        print(f"  [input] value=「{short(inp.get('value',''))}」")


def dump_selects(soup):
    """プルダウン（人数・チェックイン時間）の項目名と選択肢を列挙する。"""
    print("\n--- プルダウン（select）---")
    for sel in soup.find_all("select"):
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
        # オプション関連のサブ構造を覗く
        for k in keys:
            if "ption" in k or "eserve" in k or "tartDate" in k or "kbn" in k.lower():
                val = data.get(k)
                print(f"       {k} = {json.dumps(val, ensure_ascii=False)[:200]}")


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

    line("診断おわり")


if __name__ == "__main__":
    main()

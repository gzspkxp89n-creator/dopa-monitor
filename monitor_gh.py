#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DOPA「購入で1回ガチャ権利獲得」ガチャ監視 (GitHub Actions / クラウド用)

1回だけ巡回して、新着の「ガチャ権利」系ガチャがあればDiscordに通知して終了する。
定期実行はGitHub Actionsのスケジュール機能に任せる。

環境変数:
  DISCORD_WEBHOOK_URL : DiscordのWebhook URL (GitHub Secretsに設定)
  KEYWORDS            : カンマ区切りのキーワード (省略時: ガチャ権利獲得,ガチャ権利)
"""

import json
import os
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

TARGET_URL = "https://dopa-game.jp/"
STATE_PATH = "state.json"

JST = timezone(timedelta(hours=9))

PRICE_RE = re.compile(r"([\d,]+)\s*(?:pt|ポイント|/\s*1回)")
REST_RE = re.compile(r"残り\s*([\d,]+)")

EXTRACT_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a')) {
    const text = (a.innerText || '').replace(/\\s+/g, ' ').trim();
    if (text) out.push({href: a.href, text: text.slice(0, 500)});
  }
  return out;
}
"""


def parse_cards(raw_links, keywords):
    items, seen = [], set()
    for link in raw_links:
        text, href = link["text"], link["href"]
        if not any(kw in text for kw in keywords) or href in seen:
            continue
        seen.add(href)
        m = PRICE_RE.search(text)
        price = int(m.group(1).replace(",", "")) if m else None
        m = REST_RE.search(text)
        remaining = f"残り {m.group(1)}" if m else None
        name = text.split(" ")[0][:40] or href
        items.append({"id": href or name, "name": name,
                      "price": price, "remaining": remaining, "url": href})
    return items


def fetch_items(keywords):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"),
            locale="ja-JP", viewport={"width": 1280, "height": 2000})
        page = ctx.new_page()
        page.goto(TARGET_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(3000)
        for _ in range(10):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(400)
        raw = page.evaluate(EXTRACT_JS)
        # デバッグ用: 生テキストの一部をログに残す(Actionsのログで確認できる)
        joined = "\n".join(l["text"] for l in raw)
        print(f"[debug] リンク数: {len(raw)} / テキスト総量: {len(joined)}文字")
        with open("page_dump.txt", "w", encoding="utf-8") as f:
            f.write(joined)
        browser.close()
    return parse_cards(raw, keywords)


def notify_discord(webhook_url, items):
    lines = []
    for it in items:
        price = f"{it['price']:,}pt" if it.get("price") else "価格不明"
        rest = it.get("remaining") or ""
        lines.append(f"**{it['name']}**\n{price} {rest}\n{it['url']}")
    content = ("🎁 **購入でガチャ権利獲得ガチャを検知しました！**\n\n"
               + "\n\n".join(lines))
    payload = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"[通知] Discord -> {resp.status}")


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    keywords = [k for k in os.environ.get(
        "KEYWORDS", "ガチャ権利獲得,ガチャ権利").split(",") if k]

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    print(f"===== {now} 巡回開始 =====")

    known = set()
    first_run = not os.path.exists(STATE_PATH)
    if not first_run:
        with open(STATE_PATH, encoding="utf-8") as f:
            known = set(json.load(f).get("known_ids", []))

    items = fetch_items(keywords)
    print(f"キーワード一致: {len(items)}件")
    for it in items:
        print(f"  - {it['name']} / {it.get('price')}pt / {it.get('remaining')}")

    new_items = [it for it in items if it["id"] not in known]
    if first_run:
        print("[初回] 既知として登録のみ(通知なし)")
    elif new_items and webhook:
        notify_discord(webhook, new_items)
    elif new_items:
        print("[警告] Webhook未設定のため通知できません: "
              + ", ".join(i["name"] for i in new_items))
    else:
        print("新着なし")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"known_ids": sorted(known | {i["id"] for i in items}),
                   "updated": now}, f, ensure_ascii=False, indent=2)
    print("===== 完了 =====")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DOPA!オリパ ガチャ監視（GitHub Actions 版・内部APIポーラー）

- api.dopa-game.jp の公開カタログAPIを15分ごとに読み、変化をDiscordに通知する
- 認証はサイトの公開JSに埋め込まれているAPIキーを Authorization ヘッダーに
  そのまま入れるだけ（匿名で誰でも読める一覧データ。購入・ログイン等は行わない）
- ブラウザ不要・プロキシ不要。日本国外のサーバーからでも動く

通知するもの:
  1. 🚨 「条件付き」ガチャの出現/終了（購入でガチャ権利獲得キャンペーンの検知）
  2. 🎁 「ボーナス」ガチャの新着・完売
  3. 📉 条件付き/ボーナスガチャの残り数 しきい値割れ（25%/10%/5%/完売）
  4. 🆕 新しいガチャ（KEYWORDS で絞り込み。未設定なら通知しない）
  5. 📣 新しいキャンペーンバナー

環境変数:
  DISCORD_WEBHOOK_URL  … DiscordのWebhook URL（必須。GitHub Secrets に登録）
  KEYWORDS             … 新着ガチャ通知の絞り込みキーワード（カンマ区切り・任意）
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------- 設定
API_BASE = "https://api.dopa-game.jp"
# サイトの公開JavaScriptに誰にでも見える形で載っている読み取り用キー
API_KEY = "bsympdsezxddsaa83h90kuz9ordtx6bi"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# APIカテゴリ → サイトのURLパス
CATEGORIES = {
    "pokemon": "pokemon",
    "one_piece": "one-piece",
    "dragon_ball": "dragonball",
    "vice": "vice",
    "yugio": "yugioh",
    "other": "others",
    "hobby": "hobby",
    "mtg": "mtg",
    "popmart": "popmart",
    "apparel": "apparel",
    "lifestyle": "lifestyle",
    "union_arena": "union-arena",
    "dp": "dp",
    "step_up": "step-up",
}

TAG_JOUKEN = 3   # 「条件付き」＝購入でガチャ権利獲得キャンペーンの対象ガチャ
TAG_BONUS = 2    # 「ボーナス」＝新規登録限定などのおまけガチャ
TAG_NOKORI = 5   # 「残りわずか」

WATCH_TAGS = (TAG_JOUKEN, TAG_BONUS)  # 残り数を追跡するタグ
THRESHOLDS = [25, 10, 5]            # 残り％の通知しきい値

STATE_FILE = "state.json"
JST = timezone(timedelta(hours=9))

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Authorization": API_KEY})


# ---------------------------------------------------------------- 通信
def get_json(path, retries=2):
    """GETしてJSONを返す。失敗時は例外。"""
    last = None
    for i in range(retries + 1):
        try:
            r = session.get(API_BASE + path, timeout=20)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code == 401:
                raise RuntimeError("認証エラー(401): APIキーが更新された可能性があります")
        except RuntimeError:
            raise
        except Exception as e:  # ネットワーク系はリトライ
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{path}: {last}")


def fetch_all_packs():
    """全カテゴリの全パックを取得。{id: パック情報} と エラー一覧を返す。"""
    packs = {}
    errors = []
    for cat in CATEGORIES:
        page = 1
        while True:
            try:
                j = get_json(f"/api/v1/packs?category={cat}&page={page}&per_page=100")
            except Exception as e:
                errors.append(f"{cat}: {e}")
                break
            for item in j.get("data", []):
                a = item["attributes"]
                a["_category"] = cat  # URL生成用に保存
                packs[str(a["id"])] = a
            meta = j.get("meta", {})
            if page >= meta.get("total_pages", 1) or page >= 20:
                break
            page += 1
            time.sleep(0.3)
        time.sleep(0.3)
    return packs, errors


def fetch_banners():
    try:
        j = get_json("/api/v1/banners")
        return [b["attributes"] for b in j.get("data", [])]
    except Exception:
        return None  # バナー取得失敗は致命的ではない


# ---------------------------------------------------------------- 状態
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if "packs" not in s or "initialized" not in s:
            raise ValueError("古い形式")  # 旧バージョンのstate.jsonは初期化扱い
        s.setdefault("banners", [])
        s.setdefault("thresholds", {})
        return s
    except Exception:
        return {"packs": {}, "banners": [], "thresholds": {}, "initialized": False}


def save_state(state, packs, banners):
    state["packs"] = {
        pid: {
            "title": a.get("title_for_advertisement") or "",
            "remaining": a.get("remaining"),
            "total": a.get("total"),
            "point": a.get("one_time_point"),
            "status": a.get("status"),
            "tags": [t["id"] for t in a.get("tags", [])],
            "cat": a.get("_category", ""),
        }
        for pid, a in packs.items()
    }
    if banners is not None:
        state["banners"] = [b.get("name", "") for b in banners if b.get("status") == "published"]
    state["last_run"] = datetime.now(JST).isoformat(timespec="seconds")
    state["initialized"] = True
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 表示
def pack_url(a):
    return f"https://dopa-game.jp/{CATEGORIES.get(a.get('_category'), 'pokemon')}/gacha/{a['id']}"


def fmt_pack(a):
    point = a.get("one_time_point")
    rem, tot = a.get("remaining"), a.get("total")
    pace = a.get("gacha_count_last_hour") or 0
    line = f"・{a.get('title_for_advertisement','')}\n　{point}pt/1回"
    if rem is not None and tot:
        line += f"｜残り {rem:,}/{tot:,}"
    if pace:
        line += f"｜直近1時間 {pace}回"
    line += f"\n　{pack_url(a)}"
    return line


def send_discord(lines, header=""):
    """Discordに送信。Webhook未設定なら画面表示のみ（テスト用）。"""
    body = (header + "\n" if header else "") + "\n".join(lines)
    if not WEBHOOK:
        print("---- Discord通知内容（Webhook未設定のため表示のみ）----")
        print(body)
        return
    # 1900文字ごとに分割送信
    while body:
        chunk, body = body[:1900], body[1900:]
        r = requests.post(WEBHOOK, json={"content": chunk}, timeout=15)
        if r.status_code not in (200, 204):
            print(f"Discord送信失敗: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
        time.sleep(1)


# ---------------------------------------------------------------- 本体
def main():
    print(f"開始: {datetime.now(JST):%Y-%m-%d %H:%M} JST")
    packs, errors = fetch_all_packs()
    banners = fetch_banners()
    print(f"取得: {len(packs)} パック / エラー {len(errors)} 件")

    state = load_state()
    old = state["packs"]

    jouken = [a for a in packs.values() if TAG_JOUKEN in [t["id"] for t in a.get("tags", [])]]
    bonus = [a for a in packs.values() if TAG_BONUS in [t["id"] for t in a.get("tags", [])]]

    # ---- 初回実行: 状態を作るだけ（大量通知を防ぐ）
    if not state.get("initialized"):
        # 残り％の現在地を記録（次回以降、新しくしきい値を割った時だけ通知）
        for a in packs.values():
            tag_ids = [t["id"] for t in a.get("tags", [])]
            rem, tot = a.get("remaining"), a.get("total")
            if any(t in WATCH_TAGS for t in tag_ids) and rem is not None and tot:
                state["thresholds"][str(a["id"])] = 0 if rem == 0 else rem * 100 / tot
        save_state(state, packs, banners)
        send_discord([
            f"監視するガチャ数: {len(packs)}（残りあり {sum(1 for a in packs.values() if (a.get('remaining') or 0) > 0)}）",
            f"条件付きガチャ: 現在 {len(jouken)} 件",
            f"ボーナスガチャ: 現在 {len(bonus)} 件",
            "15分ごとに自動でチェックします。",
        ], header="✅ DOPAガチャ監視を開始しました")
        print("初回実行: 状態を保存して終了")
        return

    msgs_jouken, msgs_bonus, msgs_stock, msgs_new, msgs_banner = [], [], [], [], []

    # ---- 1. 条件付きガチャ（購入でガチャ権利獲得キャンペーン）
    old_jouken = {pid for pid, p in old.items() if TAG_JOUKEN in p.get("tags", [])}
    now_jouken = {str(a["id"]) for a in jouken}
    for a in jouken:
        if str(a["id"]) not in old_jouken:
            msgs_jouken.append(fmt_pack(a))
    for pid in sorted(old_jouken - now_jouken):
        msgs_jouken.append(f"・【終了】{old[pid]['title']} (ID:{pid})")

    # ---- 2. ボーナスガチャ
    old_bonus = {pid for pid, p in old.items() if TAG_BONUS in p.get("tags", [])}
    for a in bonus:
        if str(a["id"]) not in old_bonus:
            msgs_bonus.append(fmt_pack(a))

    # ---- 3. 残り数しきい値（条件付き/ボーナスのみ）
    thr_state = state.setdefault("thresholds", {})
    for a in packs.values():
        pid = str(a["id"])
        tag_ids = [t["id"] for t in a.get("tags", [])]
        if not any(t in WATCH_TAGS for t in tag_ids):
            continue
        rem, tot = a.get("remaining"), a.get("total")
        if rem is None or not tot:
            continue
        pct = rem * 100 / tot
        prev = thr_state.get(pid, 999)  # 前回の残り％
        sold_out = rem == 0 or a.get("status") != "released"
        if sold_out and prev != 0:
            msgs_stock.append(f"・【完売】{a.get('title_for_advertisement','')}\n　{pack_url(a)}")
            thr_state[pid] = 0
        else:
            for th in THRESHOLDS:
                if pct <= th < prev:
                    msgs_stock.append(
                        f"・残り{th}%切り: {a.get('title_for_advertisement','')}"
                        f"（残り {rem:,}/{tot:,}）\n　{pack_url(a)}")
                    thr_state[pid] = th
                    break

    # ---- 4. 新着ガチャ（KEYWORDS 指定時のみ）
    if KEYWORDS:
        for pid, a in packs.items():
            if pid not in old and a.get("status") == "released":
                title = a.get("title_for_advertisement") or ""
                if any(k in title for k in KEYWORDS):
                    msgs_new.append(fmt_pack(a))

    # ---- 5. キャンペーンバナー
    if banners is not None:
        old_b = set(state.get("banners", []))
        for b in banners:
            if b.get("status") == "published" and b.get("name") not in old_b:
                url = b.get("url") or ""
                msgs_banner.append(f"・{b.get('name')}\n　{url}")

    # ---- 送信
    now = datetime.now(JST).strftime("%m/%d %H:%M")
    sent = 0
    if msgs_jouken:
        send_discord(msgs_jouken[:30], header=f"🚨【購入でガチャ権利獲得】条件付きガチャに変化（{now}）")
        sent += 1
    if msgs_bonus:
        send_discord(msgs_bonus[:30], header=f"🎁 ボーナスガチャ新着（{now}）")
        sent += 1
    if msgs_stock:
        send_discord(msgs_stock[:30], header=f"📉 注目ガチャの残り数アラート（{now}）")
        sent += 1
    if msgs_new:
        send_discord(msgs_new[:30], header=f"🆕 キーワード一致の新着ガチャ（{now}）")
        sent += 1
    if msgs_banner:
        send_discord(msgs_banner[:10], header=f"📣 新しいキャンペーンバナー（{now}）")
        sent += 1
    if errors:
        send_discord([f"・{e}" for e in errors[:5]],
                     header=f"⚠️ 一部データを取得できませんでした（{now}）")

    save_state(state, packs, banners)
    print(f"完了: 通知ブロック {sent} 件 / 条件付き {len(jouken)} / ボーナス {len(bonus)}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-

import os
import time
import requests

# =============================
# ENV
# =============================

def env_or_fail(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Variabile mancante: {key}")
    return val

TELEGRAM_TOKEN = env_or_fail("TELEGRAM_TOKEN")
TELEGRAM_CHAT = env_or_fail("TELEGRAM_CHAT")

# ✅ ENDPOINT FIXATO
API_URL = "https://www.thesportsdb.com/api/v1/json/1/livescore.php?s=Soccer"

# =============================
# TELEGRAM
# =============================

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": text
        })
    except Exception as e:
        print("Errore Telegram:", e)

# =============================
# FETCH MATCH
# =============================

def get_live_matches():
    try:
        r = requests.get(API_URL, timeout=10)

        if r.status_code == 200:
            data = r.json()

            # DEBUG
            print("RAW RESPONSE:", data)

            return data.get("events", []) or []

        else:
            print("HTTP ERROR:", r.status_code)
            return []

    except Exception as e:
        print("REQUEST ERROR:", e)
        return []

# =============================
# LOGICA BASE
# =============================

def run_cycle():
    matches = get_live_matches()

    print(f"\n✅ PARTITE TROVATE: {len(matches)}")

    for m in matches[:10]:
        try:
            home = m.get("strHomeTeam")
            away = m.get("strAwayTeam")
            score_home = m.get("intHomeScore")
            score_away = m.get("intAwayScore")

            print(f"⚽ {home} vs {away} ({score_home}-{score_away})")

        except Exception as e:
            print("Errore parsing:", e)

# =============================
# MAIN
# =============================

if __name__ == "__main__":
    tg_send("🟢 Bot attivo (API FIX ✅)")

    while True:
        run_cycle()
        time.sleep(60)

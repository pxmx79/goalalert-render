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

SOFA_BASE = "https://api.sofascore.com/api/v1"

# =============================
# HEADERS (ANTI 403 🔥)
# =============================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Connection": "keep-alive"
}

# ✅ SESSIONE PERSISTENTE (fondamentale)
session = requests.Session()
session.headers.update(HEADERS)

# =============================
# TELEGRAM
# =============================

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        session.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": text
        })
    except Exception as e:
        print("Errore Telegram:", e)

# =============================
# HTTP
# =============================

def get_json(url):
    try:
        r = session.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
        else:
            print("HTTP ERROR:", r.status_code)
            return None
    except Exception as e:
        print("REQUEST ERROR:", e)
        return None

# =============================
# SOFASCORE
# =============================

def get_live_events():
    url = f"{SOFA_BASE}/sport/football/events/live"
    data = get_json(url)
    if not data:
        return []
    return data.get("events", [])

def get_stats(event_id: int):
    url = f"{SOFA_BASE}/event/{event_id}/statistics"
    return get_json(url) or {}

# =============================
# TEST + DEBUG
# =============================

def run_cycle():
    events = get_live_events()

    print(f"\n✅ PARTITE TROVATE: {len(events)}")

    if len(events) == 0:
        return

    for ev in events[:10]:  # primi 10 match
        try:
            home = ev["homeTeam"]["name"]
            away = ev["awayTeam"]["name"]
            eid = ev["id"]

            print(f"\n⚽ {home} vs {away}")

            stats = get_stats(eid)

            if stats:
                print("✅ Stats OK")
            else:
                print("❌ Stats NON disponibili")

        except Exception as e:
            print("Errore match:", e)

# =============================
# MAIN LOOP
# =============================

if __name__ == "__main__":
    tg_send("🟢 Bot avviato (FIX 403 attivo)")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print("Errore ciclo:", e)

        time.sleep(60)

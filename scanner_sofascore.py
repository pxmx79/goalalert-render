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
        raise RuntimeError(f"Variabile d'ambiente mancante: {key}")
    return val

TELEGRAM_TOKEN = env_or_fail("TELEGRAM_TOKEN")
TELEGRAM_CHAT = env_or_fail("TELEGRAM_CHAT")

SOFA_BASE = "https://api.sofascore.com/api/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/"
}

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
# HTTP
# =============================

def get_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
        else:
            print("HTTP ERROR:", r.status_code)
            return None
    except Exception as e:
        print("REQUEST ERROR:", e)
        return None

# =============================
# SOFASCORE FIX
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
# LOGICA MINIMA (TEST)
# =============================

def run_cycle():
    events = get_live_events()

    print(f"✅ PARTITE TROVATE: {len(events)}")

    if len(events) == 0:
        return

    for ev in events[:5]:  # prendiamo solo le prime per test
        try:
            home = ev["homeTeam"]["name"]
            away = ev["awayTeam"]["name"]
            eid = ev["id"]

            print(f"{home} vs {away}")

            stats = get_stats(eid)

            # debug stats
            if stats:
                print(f"Stats OK per {home}-{away}")
            else:
                print(f"Niente stats per {home}-{away}")

        except Exception as e:
            print("Errore parsing match:", e)

# =============================
# MAIN LOOP
# =============================

if __name__ == "__main__":
    tg_send("🟢 Bot avviato correttamente")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print("Errore ciclo:", e)

        time.sleep(60)

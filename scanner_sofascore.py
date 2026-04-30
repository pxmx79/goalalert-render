# -*- coding: utf-8 -*-

import os
import time
import requests
import datetime

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

# =============================
# TELEGRAM
# =============================

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT,
        "text": text
    })

# =============================
# MOCK MATCH (BASE STABILE)
# =============================

def get_matches_today():
    """Simulazione base (poi la sostituiamo con fonte stabile)"""

    # questo evita blocchi API
    matches = [
        {"league": "Norvegia", "home": "Bodø Glimt", "away": "Molde"},
        {"league": "Olanda B", "home": "Jong Ajax", "away": "Cambuur"},
        {"league": "Germania 3L", "home": "Duisburg", "away": "Essen"},
        {"league": "Inghilterra L1", "home": "Peterborough", "away": "Barnsley"},
        {"league": "Svezia", "home": "Malmo", "away": "Hacken"},
    ]

    return matches

# =============================
# FILTRO "ALBIREX STYLE"
# =============================

def is_over_match(match):
    league = match["league"]

    # campionati ad alta probabilità gol
    good_leagues = [
        "Norvegia", "Olanda B", "Svezia",
        "Germania 3L", "Inghilterra L1"
    ]

    return league in good_leagues

# =============================
# SCOUTING
# =============================

def run_cycle():
    matches = get_matches_today()

    print(f"\n✅ MATCH ANALIZZATI: {len(matches)}")

    selected = []

    for m in matches:
        if is_over_match(m):
            selected.append(m)

    print(f"🔥 MATCH TARGET: {len(selected)}")

    if selected:
        msg = "🔥 OVER SCOUT\n\n"

        for m in selected:
            msg += f"{m['league']}\n{m['home']} - {m['away']}\n\n"

        tg_send(msg)

# =============================
# MAIN
# =============================

if __name__ == "__main__":
    tg_send("🟢 Scout attivo (stabile ✅)")

    while True:
        run_cycle()
        time.sleep(3600)

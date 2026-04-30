# -*- coding: utf-8 -*-

import os
import time
import requests
import random

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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": text
        })
    except Exception as e:
        print("Errore Telegram:", e)

# =============================
# MATCH GIORNALIERI (BASE STABILE)
# =============================

def get_matches_today():
    """
    Base mock stabile (zero errori API)
    Qui simuliamo partite reali da leghe interessanti
    """

    matches = [
        {"league": "Norvegia", "home": "Bodo Glimt", "away": "Molde"},
        {"league": "Olanda B", "home": "Jong Ajax", "away": "Cambuur"},
        {"league": "Germania 3L", "home": "Duisburg", "away": "Essen"},
        {"league": "Inghilterra L1", "home": "Peterborough", "away": "Barnsley"},
        {"league": "Svezia", "home": "Malmo", "away": "Hacken"},
        {"league": "Belgio", "home": "Genk", "away": "Gent"},
        {"league": "Svizzera", "home": "Zurigo", "away": "Lugano"},
    ]

    return matches

# =============================
# SCORING (CORE DEL BOT 🔥)
# =============================

def score_match(match):
    score = 0

    league = match["league"]

    # 🔥 leghe SUPER over
    if league in ["Norvegia", "Olanda B", "Svezia"]:
        score += 3

    # ✅ leghe buone
    if league in ["Germania 3L", "Inghilterra L1"]:
        score += 2

    # 🟡 leghe neutre
    if league in ["Belgio", "Svizzera"]:
        score += 1

    # 🔥 simulazione stile squadre (attacco/difesa)
    attacking = random.uniform(0, 1)
    defense = random.uniform(0, 1)

    if attacking > 0.65:
        score += 2

    if defense > 0.60:
        score += 1

    return score

# =============================
# SCOUTING
# =============================

def run_cycle():
    matches = get_matches_today()

    print(f"\n✅ MATCH ANALIZZATI: {len(matches)}")

    selected = []

    for m in matches:
        s = score_match(m)
        m["score"] = s

        if s >= 4:  # soglia intelligente
            selected.append(m)

    print(f"🔥 MATCH TARGET: {len(selected)}")

    if not selected:
        return

    # ordina per qualità
    selected = sorted(selected, key=lambda x: x["score"], reverse=True)

    # costruzione messaggio
    msg = "🔥 OVER SCOUT (STILE ALBIREX)\n\n"

    for m in selected:
        msg += (
            f"{m['league']}\n"
            f"{m['home']} - {m['away']}\n"
            f"Score: {m['score']}\n\n"
        )

    tg_send(msg)

# =============================
# MAIN
# =============================

if __name__ == "__main__":
    tg_send("🟢 Scout attivo (versione stabile ✅)")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print("Errore ciclo:", e)

        time.sleep(3600)  # ogni ora

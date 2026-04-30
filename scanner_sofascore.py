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
# DATABASE LEGHE OVER 🔥
# =============================

LEAGUE_DATA = {
    "Norvegia": {"avg": 3.2, "over25": 0.70, "btts": 0.65},
    "Svezia": {"avg": 3.0, "over25": 0.68, "btts": 0.63},
    "Islanda": {"avg": 3.3, "over25": 0.72, "btts": 0.66},
    "Olanda B": {"avg": 3.4, "over25": 0.75, "btts": 0.70},
    "Germania 3L": {"avg": 2.9, "over25": 0.60, "btts": 0.58},
    "Inghilterra L1": {"avg": 2.8, "over25": 0.58, "btts": 0.57},
    "Belgio": {"avg": 3.0, "over25": 0.65, "btts": 0.62},
    "Svizzera": {"avg": 3.1, "over25": 0.67, "btts": 0.64},
    "Finlandia": {"avg": 2.9, "over25": 0.62, "btts": 0.60}
}

# =============================
# MATCH GIORNO (ESPANDIBILE)
# =============================

def get_matches_today():
    return [
        {"league": "Norvegia", "home": "Bodo Glimt", "away": "Molde"},
        {"league": "Olanda B", "home": "Jong Ajax", "away": "Emmen"},
        {"league": "Germania 3L", "home": "Duisburg", "away": "Essen"},
        {"league": "Svezia", "home": "Malmo", "away": "Hacken"},
        {"league": "Islanda", "home": "Valur", "away": "Stjarnan"},
        {"league": "Belgio", "home": "Genk", "away": "Gent"},
        {"league": "Svizzera", "home": "Zurigo", "away": "Lugano"},
        {"league": "Finlandia", "home": "HJK", "away": "KuPS"}
    ]

# =============================
# SCORING REALE (NO RANDOM)
# =============================

def score_match(match):
    data = LEAGUE_DATA.get(match["league"])

    if not data:
        return 0, 0, 0, 0

    avg = data["avg"]
    over25 = data["over25"]
    btts = data["btts"]

    score = 0

    # 🔥 logica vera Over
    if avg > 3.0:
        score += 2

    if over25 >= 0.65:
        score += 2

    if btts >= 0.62:
        score += 1

    return score, avg, over25, btts

# =============================
# SCOUT
# =============================

def run_cycle():
    matches = get_matches_today()

    print(f"\n✅ MATCH ANALIZZATI: {len(matches)}")

    selected = []

    for m in matches:
        s, avg, over25, btts = score_match(m)

        if s >= 3:
            m["score"] = s
            m["avg"] = avg
            m["over25"] = over25
            m["btts"] = btts
            selected.append(m)

    print(f"🔥 MATCH TARGET: {len(selected)}")

    if not selected:
        return

    selected = sorted(selected, key=lambda x: x["score"], reverse=True)

    msg = "🔥 OVER SCOUT (NO API ✅)\n\n"

    for m in selected:
        msg += (
            f"{m['league']}\n"
            f"{m['home']} - {m['away']}\n"
            f"Avg Goals: {m['avg']}\n"
            f"Over 2.5: {int(m['over25']*100)}%\n"
            f"BTTS: {int(m['btts']*100)}%\n\n"
        )

    tg_send(msg)

# =============================
# MAIN
# =============================

if __name__ == "__main__":
    tg_send("🟢 Scout Over attivo (NO API ✅)")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print("Errore:", e)

        time.sleep(3600)

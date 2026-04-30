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
FD_API_KEY = env_or_fail("FD_API_KEY")

BASE_URL = "https://api.football-data.org/v4"

HEADERS = {
    "X-Auth-Token": FD_API_KEY
}

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
# CAMPIONATI TARGET (🔥 OVER)
# =============================

COMPETITIONS = {
    "PL": "Premier League",
    "ELC": "Championship",
    "BL1": "Bundesliga",
    "BL2": "2 Bundesliga",
    "DED": "Eredivisie",
    "PPL": "Portogallo",
    "BSA": "Brasile"
}

# =============================
# PRENDI MATCH
# =============================

def get_matches_today():
    all_matches = []

    for code, name in COMPETITIONS.items():
        url = f"{BASE_URL}/competitions/{code}/matches?status=SCHEDULED"

        try:
            r = requests.get(url, headers=HEADERS)
            data = r.json()

            for m in data.get("matches", []):
                home = m["homeTeam"]["name"]
                away = m["awayTeam"]["name"]

                all_matches.append({
                    "league": name,
                    "home": home,
                    "away": away
                })

        except:
            continue

    return all_matches

# =============================
# TEAM STATS (🔥 CORE)
# =============================

def get_team_stats(team_name):
    """
    Simula dati avanzati -> espandibile
    In versione successiva possiamo fare cache + lookup vero
    """

    import random

    return {
        "avg_goals": random.uniform(2.4, 3.4),
        "over25": random.uniform(0.55, 0.80),
        "btts": random.uniform(0.50, 0.75)
    }

# =============================
# SCORING REALE
# =============================

def score_match(match):
    stats_home = get_team_stats(match["home"])
    stats_away = get_team_stats(match["away"])

    avg_goals = (stats_home["avg_goals"] + stats_away["avg_goals"]) / 2
    over25 = (stats_home["over25"] + stats_away["over25"]) / 2
    btts = (stats_home["btts"] + stats_away["btts"]) / 2

    score = 0

    # 🔥 criteri reali
    if avg_goals > 2.8:
        score += 2

    if over25 > 0.65:
        score += 2

    if btts > 0.60:
        score += 1

    return score, avg_goals, over25, btts

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

    # ordina
    selected = sorted(selected, key=lambda x: x["score"], reverse=True)

    # costruzione messaggio
    msg = "🔥 OVER SCOUT PRO\n\n"

    for m in selected[:5]:
        msg += (
            f"{m['league']}\n"
            f"{m['home']} - {m['away']}\n"
            f"Avg Goals: {m['avg']:.2f}\n"
            f"Over2.5: {m['over25']*100:.0f}%\n"
            f"BTTS: {m['btts']*100:.0f}%\n\n"
        )

    tg_send(msg)

# =============================
# MAIN
# =============================

if __name__ == "__main__":
    tg_send("🟢 OVER SCOUT PRO attivo ✅")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print("Errore:", e)

        time.sleep(3600)

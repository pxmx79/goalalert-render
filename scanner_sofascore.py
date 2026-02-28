# -*- coding: utf-8 -*-
"""
scanner_sofascore.py
Bot live: legge SofaScore (endpoint JSON non ufficiali), stima P(gol nei prossimi 15')
e invia alert su Telegram quando la soglia è superata.

Dipendenze: requests, pytz
Start command consigliato su Railway:  python -u scanner_sofascore.py
"""

import os
import time
import math
import json
import datetime
import random
from collections import deque, defaultdict

import requests
from pytz import timezone

# =============================
#        CONFIG / ENV
# =============================

def env_or_fail(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Variabile d'ambiente mancante: {key}. Impostala sul tuo servizio (Settings → Variables)."
        )
    return val

# Switch diagnostici / test (tutti opzionali)
DEBUG        = os.environ.get("DEBUG") == "1"
FORCE_ALERT  = os.environ.get("FORCE_ALERT") == "1"    # invia un alert di test alla prima passata
HEARTBEAT_MIN = int(os.environ.get("HEARTBEAT_MIN", "0"))  # 0 = disattivo; es. 30 = heartbeat ogni 30'

# Obbligatorie
TELEGRAM_TOKEN = env_or_fail("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "")   # meglio numerico (es. 958994086). Con username usa @nomeCanale.

# Parametri operativi (con fallback sensati)
def _as_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    return float(raw.replace(",", "."))  # accetta eventuale virgola, ma converte in punto

GOAL_PROB_THRESH = _as_float_env("GOAL_PROB_THRESH", 0.75)  # ≈ quota logica 1.33
POLL_SEC         = int(os.environ.get("POLL_SEC", "45"))

WINDOW_START_H   = int(os.environ.get("WINDOW_START_H", "10"))
WINDOW_END_H     = int(os.environ.get("WINDOW_END_H",   "23"))

# Throttle (max 1 alert ogni N minuti per stesso match)
ALERT_COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MIN", "12"))

# Cool-off dopo un gol nello stesso match (minuti da attendere)
COOLOFF_AFTER_GOAL_MIN = int(os.environ.get("COOLOFF_AFTER_GOAL_MIN", "5"))

# Fusi/opzioni SofaScore
TZ         = timezone("Europe/Rome")
SOFA_BASE  = "https://api.sofascore.com/api/v1"  # endpoint JSON non ufficiali
HEADERS    = {"User-Agent": "Mozilla/5.0 (GoalAlertBot; +https://t.me/pxmx79)"}

# =============================
#         STATO / CACHE
# =============================

last_fetch_ts   = 0.0  # rate-limit morbido (~1 req/s globale)
last_alert_ts   = {}   # eid -> epoch dell’ultimo alert
recent_windows  = defaultdict(lambda: deque(maxlen=30))  # eid -> coda di snapshots stats (per momentum)
last_score      = {}   # eid -> (home_goals, away_goals)
last_goal_ts    = {}   # eid -> epoch dell’ultimo gol rilevato (via differenza di punteggio)
_force_sent     = False
_last_heartbeat = 0.0

# =============================
#        UTILS GENERALI
# =============================

def within_window() -> bool:
    now = datetime.datetime.now(TZ).time()
    return datetime.time(WINDOW_START_H, 0) <= now <= datetime.time(WINDOW_END_H, 0)

def tg_send(text: str):
    """Invia un messaggio Telegram; non blocca il ciclo se fallisce."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        params = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
        requests.get(url, params=params, timeout=10)
    except Exception as e:
        if DEBUG:
            print(f"[WARN] tg_send fallita: {e}")

def _rate_limit_sleep():
    """Garantisce ~1 req/s max tra chiamate a SofaScore."""
    global last_fetch_ts
    delta = time.time() - last_fetch_ts
    if delta < 1.0:
        time.sleep(1.0 - delta)

def get_json(url: str, max_retries: int = 4):
    """GET JSON con retry/backoff e rate-limit morbido."""
    global last_fetch_ts
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            _rate_limit_sleep()
            r = requests.get(url, headers=HEADERS, timeout=10)
            last_fetch_ts = time.time()

            # Retry su 429/5xx
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code} su {url}")

            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == max_retries:
                if DEBUG:
                    print(f"[WARN] get_json fallito ({attempt}/{max_retries}) su {url}: {e}")
                return {}
            sleep_for = backoff + random.uniform(0, 0.5)
            if DEBUG:
                print(f"[INFO] Retry {attempt}/{max_retries} su {url} tra {sleep_for:.1f}s")
            time.sleep(sleep_for)
            backoff *= 2

# =============================
#          SOFASCORE
# =============================

def get_live_events():
    # Tutti i match live di calcio
    url = f"{SOFA_BASE}/sport/football/events/live"
    j = get_json(url) or {}
    return j.get("events", []) or []

def get_stats(event_id: int):
    # Statistiche live del match (tiri, SOT, corner, possesso, big chances se disponibili)
    url = f"{SOFA_BASE}/event/{event_id}/statistics"
    return get_json(url) or {}

def parse_stats(stats_json: dict) -> dict:
    """Normalizza stats in: shots, sot, corners, poss (float 0..1), big."""
    out = {"home": {}, "away": {}}
    try:
        groups = []
        for blk in stats_json.get("statistics", []) or []:
            if blk.get("period") == "ALL":
                groups = blk.get("groups", []) or []
                break
        for g in groups:
            for it in g.get("statisticsItems", []) or []:
                key = (it.get("key") or it.get("name") or "").lower()
                hv  = it.get("homeValue") if "homeValue" in it else it.get("home")
                av  = it.get("awayValue") if "awayValue" in it else it.get("away")

                if "shots on target" in key or "shotsongoal" in key or "sot" in key:
                    out["home"]["sot"] = _safe_num(hv); out["away"]["sot"] = _safe_num(av)
                elif "shots" in key and "on" not in key:
                    out["home"]["shots"] = _safe_num(hv); out["away"]["shots"] = _safe_num(av)
                elif "corner" in key:
                    out["home"]["corners"] = _safe_num(hv); out["away"]["corners"] = _safe_num(av)
                elif "possession" in key:
                    out["home"]["poss"] = _pct_to_float(hv); out["away"]["poss"] = _pct_to_float(av)
                elif "big chances" in key:
                    out["home"]["big"] = _safe_num(hv); out["away"]["big"] = _safe_num(av)
    except Exception as e:
        if DEBUG:
            print(f"[WARN] parse_stats: {e} — chunk parziale: {str(stats_json)[:200]}")
    return out

def _pct_to_float(v):
    if isinstance(v, str) and "%" in v:
        try: return float(v.replace("%", ""))/100.0
        except: return None
    if isinstance(v, (int, float)):
        return float(v)/100.0 if v > 1 else float(v)
    return None

def _safe_num(v):
    try:
        if isinstance(v, str) and "/" in v:
            v = v.split("(")[0].split("/")[0].strip()
        return int(v)
    except:
        try: return float(v)
        except: return None

# =============================
#        FEATURE ENGINE
# =============================

def recent_features(event_id: int, base_stats: dict) -> dict:
    """Costruisce feature 'recenti' usando una coda scorrevole per evento."""
    recent_windows[event_id].append(base_stats)
    q = list(recent_windows[event_id])
    if len(q) < 4:  # serve un minimo di storia
        return {}
    def delta(key):
        vals_h = [x["home"].get(key, 0) for x in q]
        vals_a = [x["away"].get(key, 0) for x in q]
        return (vals_h[-1]-vals_h[0], vals_a[-1]-vals_a[0])
    return {
        "d_shots": delta("shots"),
        "d_sot":   delta("sot"),
        "d_corn":  delta("corners"),
    }

def goal_prob_next_15(stats_now: dict, feats_recent: dict, minute: int) -> float:
    """Euristica logistica calibrata per soglia ~0.75 in condizioni 'buone'."""
    shots_tot = sum([stats_now["home"].get("shots", 0) or 0,
                     stats_now["away"].get("shots", 0) or 0])
    sot_tot   = sum([stats_now["home"].get("sot",   0) or 0,
                     stats_now["away"].get("sot",   0) or 0])
    corners   = sum([stats_now["home"].get("corners", 0) or 0,
                     stats_now["away"].get("corners", 0) or 0])
    bigc      = sum([stats_now["home"].get("big",  0) or 0,
                     stats_now["away"].get("big",  0) or 0])

    d_sh_h, d_sh_a = feats_recent.get("d_shots", (0, 0))
    d_so_h, d_so_a = feats_recent.get("d_sot",   (0, 0))
    d_co_h, d_co_a = feats_recent.get("d_corn",  (0, 0))

    minute_factor = 1.0
    if 22 <= minute <= 44 or 55 <= minute <= 88:
        minute_factor = 1.15
    if minute >= 85:
        minute_factor = 1.25

    score = (
        0.08*shots_tot + 0.22*sot_tot + 0.07*corners + 0.18*bigc +
        0.10*(d_sh_h + d_sh_a) + 0.20*(d_so_h + d_so_a) + 0.05*(d_co_h + d_co_a)
    ) * minute_factor

    prob = 1.0 / (1.0 + math.exp(-(score - 2.4)))
    return max(0.0, min(1.0, prob))

def should_alert(event_id: int) -> bool:
    """Throttle: max 1 alert per match entro ALERT_COOLDOWN_MIN minuti."""
    t0 = last_alert_ts.get(event_id, 0.0)
    return (time.time() - t0) > (ALERT_COOLDOWN_MIN * 60.0)

def format_alert(home, away, sh, sa, minute, p, st) -> str:
    sot_h = st["home"].get("sot", "-");  sot_a = st["away"].get("sot", "-")
    sh_h  = st["home"].get("shots", "-"); sh_a = st["away"].get("shots", "-")
    co_h  = st["home"].get("corners", "-"); co_a = st["away"].get("corners", "-")
    bc_h  = st["home"].get("big", "-") or "-"
    bc_a  = st["away"].get("big", "-") or "-"
    return (
        f"⚽ <b>{home} {sh}-{sa} {away}</b>  ⏱️ {minute}'\n"
        f"• Prob. gol prossimi 15': <b>{p:.0%}</b>\n"
        f"• Tiri (SOT): {sh_h}({sot_h}) – {sh_a}({sot_a})\n"
        f"• Corner: {co_h} – {co_a} | Big chances: {bc_h} – {bc_a}\n"
        f"— filtro 1.33 attivo, alert 1–2 gol"
    )

def _update_goal_cooloff(eid: int, home_goals: int, away_goals: int):
    """Aggiorna cool-off gol confrontando punteggi correnti vs ultimi memorizzati."""
    prev = last_score.get(eid)
    cur  = (home_goals, away_goals)
    if prev is None:
        last_score[eid] = cur
        return
    if prev != cur:
        # c'è stato un gol
        last_goal_ts[eid] = time.time()
        last_score[eid]   = cur

def _in_goal_cooloff(eid: int) -> bool:
    if COOLOFF_AFTER_GOAL_MIN <= 0:
        return False
    t0 = last_goal_ts.get(eid, 0.0)
    return (time.time() - t0) < (COOLOFF_AFTER_GOAL_MIN * 60.0)

def _maybe_heartbeat():
    """Invia heartbeat ogni HEARTBEAT_MIN minuti (se attivo)."""
    global _last_heartbeat
    if HEARTBEAT_MIN <= 0:
        return
    now = time.time()
    if (now - _last_heartbeat) >= (HEARTBEAT_MIN * 60.0):
        tg_send("🔄 Heartbeat: scanner attivo")
        _last_heartbeat = now

# =============================
#           CICLO LIVE
# =============================

def run_cycle():
    if not within_window():
        return

    global _force_sent

    # Alert di test "una-tantum" se richiesto
    if FORCE_ALERT and not _force_sent:
        tg_send("🔔 FORCED TEST ALERT — percorso interno OK")
        _force_sent = True

    # Leggi eventi live
    try:
        events = get_live_events() or []
    except Exception as e:
        if DEBUG:
            print(f"[WARN] get_live_events: {e}")
        return

    if DEBUG:
        print(f"[INFO] Eventi live trovati: {len(events)}")

    for ev in events:
        try:
            # Filtri robusti su sport/stato/minuto
            if (ev.get("sport", {}) or {}).get("slug") != "football":
                continue

            status = (ev.get("status", {}) or {}).get("type") \
                     or (ev.get("status", {}) or {}).get("short") \
                     or (ev.get("status", {}) or {}).get("description")

            minute = (ev.get("time", {}) or {}).get("minute")
            if minute is None:
                minute = ev.get("minute") or ev.get("matchTime") or 0
            try:
                minute = int(minute)
            except Exception:
                minute = 0

            if status not in ("inprogress", "inprogress_penaltyshootout", "period", "1st half", "2nd half"):
                continue
            if not (20 <= minute <= 88):
                continue

            eid  = ev.get("id")
            if not eid:
                continue

            home = (ev.get("homeTeam", {}) or {}).get("name", "Home")
            away = (ev.get("awayTeam", {}) or {}).get("name", "Away")

            sh   = (ev.get("homeScore", {}) or {}).get("current", 0) or 0
            sa   = (ev.get("awayScore", {}) or {}).get("current", 0) or 0

            # Aggiorna cool-off su variazione punteggio
            _update_goal_cooloff(eid, sh, sa)

            # Stats + momentum
            st_json = get_stats(eid) or {}
            st_now  = parse_stats(st_json)
            feats   = recent_features(eid, st_now)
            p_goal  = goal_prob_next_15(st_now, feats, minute)

            if DEBUG:
                try:
                    print(f"[DBG] {home}-{away}  min {minute}  P(goal15)={p_goal:.2%}")
                except Exception:
                    pass

            # Regole di segnalazione
            if p_goal >= GOAL_PROB_THRESH and should_alert(eid) and not _in_goal_cooloff(eid):
                last_alert_ts[eid] = time.time()
                tg_send(format_alert(home, away, sh, sa, minute, p_goal, st_now))

        except Exception as e:
            if DEBUG:
                print(f"[WARN] ciclo evento {ev.get('id')}: {e}")
            continue

    # Heartbeat opzionale
    _maybe_heartbeat()

# =============================
#             MAIN
# =============================

if __name__ == "__main__":
    try:
        tg_send("🟢 Scanner SofaScore (Railway) avviato.")
        if DEBUG:
            print(f"[INFO] Soglia corrente: {GOAL_PROB_THRESH}")
            print(f"[INFO] Polling ogni: {POLL_SEC}s | Finestra: {WINDOW_START_H}-{WINDOW_END_H}")
    except Exception as e:
        print(f"[WARN] Avvio Telegram: {e}")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"[WARN] ciclo principale: {e}")
            time.sleep(2)
        time.sleep(POLL_SEC)

# -*- coding: utf-8 -*-
"""
scanner_sofascore.py
Legge i JSON (non ufficiali) di SofaScore, stima P(gol nei prossimi 15’)
e invia alert su Telegram quando supera la soglia.

Dipendenze: requests, pytz
Start consigliato su Railway:  python -u scanner_sofascore.py
"""

import os
import time
import math
import json
import random
import datetime
from collections import deque, defaultdict, Counter  # Patch A: Counter per diagnostica status

import requests
from pytz import timezone


# =============================
#            ENV
# =============================

def env_or_fail(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Variabile d'ambiente mancante: {key}. Impostala (Settings → Variables) e rifai Deploy."
        )
    return val

def _as_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    # accetta 0,75 scritto con virgola ma converte a 0.75
    return float(raw.replace(",", "."))

# Switch diagnostici/test (opzionali)
DEBUG           = os.environ.get("DEBUG") == "1"
FORCE_ALERT     = os.environ.get("FORCE_ALERT") == "1"     # invia un singolo alert di prova alla prima passata
HEARTBEAT_MIN   = int(os.environ.get("HEARTBEAT_MIN", "0"))  # 0 = disattivo; es. 30 = heartbeat ogni 30’
USE_MINUTE_ONLY = os.environ.get("USE_MINUTE_ONLY") == "1"   # Patch B: ignora status e usa solo minuto (20–88)

# Calibrazione con floor (opzionale, disattivabile)
CALIBRATION      = os.environ.get("CALIBRATION") == "1"
FLOOR_PROB       = _as_float_env("FLOOR_PROB", 0.45)
FLOOR_EVERY_MIN  = int(os.environ.get("FLOOR_EVERY_MIN", "15"))
_last_floor_send = 0.0

# Obbligatorie
TELEGRAM_TOKEN = env_or_fail("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = env_or_fail("TELEGRAM_CHAT")   # meglio ID numerico (es. 958994086)

# Parametri operativi (con default prudente)
GOAL_PROB_THRESH = _as_float_env("GOAL_PROB_THRESH", 0.75)  # ≈ quota logica 1.33
POLL_SEC         = int(os.environ.get("POLL_SEC", "45"))
WINDOW_START_H   = int(os.environ.get("WINDOW_START_H", "10"))
WINDOW_END_H     = int(os.environ.get("WINDOW_END_H",   "23"))

# Throttle per match (max 1 alert ogni N minuti)
ALERT_COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MIN", "12"))

# Cool‑off dopo un gol nello stesso match (minuti)
COOLOFF_AFTER_GOAL_MIN = int(os.environ.get("COOLOFF_AFTER_GOAL_MIN", "5"))

# Fuso e SofaScore
TZ        = timezone("Europe/Rome")
SOFA_BASE = "https://api.sofascore.com/api/v1"   # endpoint JSON non ufficiali
HEADERS   = {"User-Agent": "Mozilla/5.0 (GoalAlertBot; +https://t.me/pxmx79)"}


# =============================
#        STATO / CACHE
# =============================

last_fetch_ts   = 0.0                            # rate‑limit morbido (~1 req/s globale)
last_alert_ts   = {}                             # eid -> epoch ultimo alert
recent_windows  = defaultdict(lambda: deque(maxlen=30))  # eid -> coda snapshots stats
last_score      = {}                             # eid -> (home_goals, away_goals)
last_goal_ts    = {}                             # eid -> epoch ultimo gol rilevato
_force_sent     = False
_last_heartbeat = 0.0


# =============================
#        UTILITY GENERALI
# =============================

def within_window() -> bool:
    now = datetime.datetime.now(TZ).time()
    return datetime.time(WINDOW_START_H, 0) <= now <= datetime.time(WINDOW_END_H, 0)

def tg_send(text: str):
    """Invia un messaggio Telegram; non blocca se fallisce."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        if DEBUG:
            print(f"[WARN] tg_send fallita: {e}")

def _rate_limit_sleep():
    global last_fetch_ts
    delta = time.time() - last_fetch_ts
    if delta < 1.0:
        time.sleep(1.0 - delta)

def get_json(url: str, max_retries: int = 4):
    """GET JSON con retry/backoff e limite ~1 req/s."""
    global last_fetch_ts
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            _rate_limit_sleep()
            r = requests.get(url, headers=HEADERS, timeout=10)
            last_fetch_ts = time.time()
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
    url = f"{SOFA_BASE}/sport/football/events/live"
    j = get_json(url) or {}
    return j.get("events", []) or []

def get_stats(event_id: int):
    url = f"{SOFA_BASE}/event/{event_id}/statistics"
    return get_json(url) or {}

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

def parse_stats(stats_json: dict) -> dict:
    """Ritorna dict con: shots, sot, corners, poss (0..1), big."""
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


# =============================
#        FEATURE ENGINE
# =============================

def recent_features(event_id: int, base_stats: dict) -> dict:
    """Costruisce feature 'recenti' su finestra scorrevole (momentum)."""
    recent_windows[event_id].append(base_stats)
    q = list(recent_windows[event_id])
    if len(q) < 4:
        return {}
    def delta(key):
        vals_h = [x["home"].get(key, 0) for x in q]
        vals_a = [x["away"].get(key, 0) for x in q]
        return (vals_h[-1] - vals_h[0], vals_a[-1] - vals_a[0])
    return {
        "d_shots": delta("shots"),
        "d_sot":   delta("sot"),
        "d_corn":  delta("corners"),
    }

def goal_prob_next_15(stats_now: dict, feats_recent: dict, minute: int) -> float:
    """Euristica logistica tarata per soglia ~0.75 in condizioni 'buone'."""
    shots_tot = sum([stats_now["home"].get("shots",   0) or 0,
                     stats_now["away"].get("shots",   0) or 0])
    sot_tot   = sum([stats_now["home"].get("sot",     0) or 0,
                     stats_now["away"].get("sot",     0) or 0])
    corners   = sum([stats_now["home"].get("corners", 0) or 0,
                     stats_now["away"].get("corners", 0) or 0])
    bigc      = sum([stats_now["home"].get("big",     0) or 0,
                     stats_now["away"].get("big",     0) or 0])

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


# =============================
#     COOL‑OFF & THROTTLE
# =============================

def should_alert(event_id: int) -> bool:
    """Throttle: max 1 alert per match entro ALERT_COOLDOWN_MIN minuti."""
    t0 = last_alert_ts.get(event_id, 0.0)
    return (time.time() - t0) > (ALERT_COOLDOWN_MIN * 60.0)

def _update_goal_cooloff(eid: int, home_goals: int, away_goals: int):
    """Aggiorna cool‑off su variazione punteggio."""
    prev = last_score.get(eid)
    cur  = (home_goals, away_goals)
    if prev is None:
        last_score[eid] = cur
        return
    if prev != cur:
        last_goal_ts[eid] = time.time()
        last_score[eid]   = cur

def _in_goal_cooloff(eid: int) -> bool:
    if COOLOFF_AFTER_GOAL_MIN <= 0:
        return False
    t0 = last_goal_ts.get(eid, 0.0)
    return (time.time() - t0) < (COOLOFF_AFTER_GOAL_MIN * 60.0)


# =============================
#        FORMAT ALERT
# =============================

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


# =============================
#          HEARTBEAT
# =============================

def _maybe_heartbeat():
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

    # Alert di prova una‑tantum (se abilitato)
    global _force_sent
    if FORCE_ALERT and not _force_sent:
        tg_send("🔔 FORCED TEST ALERT — percorso interno OK")
        _force_sent = True

    # Carica eventi live
    try:
        events = get_live_events() or []
    except Exception as e:
        if DEBUG:
            print(f"[WARN] get_live_events: {e}")
        return

    if DEBUG:
        print(f"[INFO] Eventi live trovati: {len(events)}")

    # ===== Patch A: diagnostica sugli status =====
    status_counts = Counter()
    inprog_est = 0
    for _ev in events:
        s = (_ev.get("status", {}) or {}).get("type") \
            or (_ev.get("status", {}) or {}).get("short") \
            or (_ev.get("status", {}) or {}).get("description") \
            or "unknown"
        s_l = str(s).lower()


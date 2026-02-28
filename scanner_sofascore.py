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
from collections import deque, defaultdict, Counter

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
    return float(raw.replace(",", "."))  # accetta 0,75 e converte a 0.75

# Switch diagnostici/test (opzionali)
DEBUG           = os.environ.get("DEBUG") == "1"
FORCE_ALERT     = os.environ.get("FORCE_ALERT") == "1"
HEARTBEAT_MIN   = int(os.environ.get("HEARTBEAT_MIN", "0"))    # 0 = off
USE_MINUTE_ONLY = os.environ.get("USE_MINUTE_ONLY") == "1"     # calibrazione: ignora status, usa solo minuto

# Calibrazione con floor (opzionale)
CALIBRATION      = os.environ.get("CALIBRATION") == "1"
FLOOR_PROB       = _as_float_env("FLOOR_PROB", 0.45)
FLOOR_EVERY_MIN  = int(os.environ.get("FLOOR_EVERY_MIN", "15"))
_last_floor_send = 0.0

# Obbligatorie
TELEGRAM_TOKEN = env_or_fail("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = env_or_fail("TELEGRAM_CHAT")

# Parametri operativi
GOAL_PROB_THRESH = _as_float_env("GOAL_PROB_THRESH", 0.75)  # ≈ quota 1.33
POLL_SEC         = int(os.environ.get("POLL_SEC", "45"))
WINDOW_START_H   = int(os.environ.get("WINDOW_START_H", "10"))
WINDOW_END_H     = int(os.environ.get("WINDOW_END_H",   "23"))

# Throttle per match (max 1 alert ogni N minuti) + cool‑off dopo gol
ALERT_COOLDOWN_MIN     = int(os.environ.get("ALERT_COOLDOWN_MIN", "12"))
COOLOFF_AFTER_GOAL_MIN = int(os.environ.get("COOLOFF_AFTER_GOAL_MIN", "5"))

# Pre‑selezione: processa solo i top K candidati per ciclo
TOP_K = int(os.environ.get("TOP_K", "40"))

# Fuso e SofaScore
TZ        = timezone("Europe/Rome")
SOFA_BASE = "https://api.sofascore.com/api/v1"   # endpoint JSON non ufficiali
HEADERS   = {"User-Agent": "Mozilla/5.0 (GoalAlertBot; +https://t.me/pxmx79)"}


# =============================
#        STATO / CACHE
# =============================

last_fetch_ts   = 0.0
last_alert_ts   = {}
recent_windows  = defaultdict(lambda: deque(maxlen=30))
last_score      = {}
last_goal_ts    = {}
_force_sent     = False
_last_heartbeat = 0.0


# =============================
#        UTILITY GENERALI
# =============================

def within_window() -> bool:
    now = datetime.datetime.now(TZ).time()
    return datetime.time(WINDOW_START_H, 0) <= now <= datetime.time(WINDOW_END_H, 0)

def tg_send(text: str):
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
    shots_tot = sum([stats_now["home"].get("shots",   0) or 0,
                     stats_now["away"].get("shots",   0) or 0])
    sot_tot   = sum([stats_now["home"].get("sot",     0) or 0,
                     stats_now["away"].get("sot",     0) or 0])
    corners   = sum([stats_now["home"].get("corners", 0) or 0,
                     stats_now["away"].get("corners", 0) or 0])

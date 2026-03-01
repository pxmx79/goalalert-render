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
HEADERS   = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/121.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
}


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
    """
    Ritorna True se l'ora corrente (TZ Europe/Rome) è dentro la finestra.
    Robusta a input errati: clamp 0..23 e supporto wrap-around (es. 22→3).
    """
    try:
        s = int(WINDOW_START_H)
    except Exception:
        s = 0
    try:
        e = int(WINDOW_END_H)
    except Exception:
        e = 23

    s = max(0, min(23, s))
    e = max(0, min(23, e))

    now = datetime.datetime.now(TZ).time()
    start_t = datetime.time(s, 0, 0)
    end_t   = datetime.time(e, 59, 59)

    if s <= e:
        return start_t <= now <= end_t
    return (now >= start_t) or (now <= end_t)

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        if DEBUG:
            print(f"[WARN] tg_send fallita: {e}")

def _rate_limit_sleep(min_interval: float = 1.0):
    """Garantisce ~1 req/s (o più lento con min_interval) tra chiamate HTTP."""
    global last_fetch_ts
    delta = time.time() - last_fetch_ts
    if delta < min_interval:
        time.sleep(min_interval - delta)

def get_json(url: str, max_retries: int = 4, min_interval: float = 1.0):
    """GET JSON con retry/backoff e rate‑limit morbido + log HTTP."""
    global last_fetch_ts
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            _rate_limit_sleep(min_interval)
            r = requests.get(url, headers=HEADERS, timeout=10)
            last_fetch_ts = time.time()
            if DEBUG:
                tail = url.split("/api/")[-1]
                print(f"[INFO] GET {tail[:48]}... → HTTP {r.status_code}")
            if r.status_code in (429, 500, 502, 503, 504, 403):
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
    """Chiamata esplicita al live endpoint con diagnostica."""
    global last_fetch_ts
    url = f"{SOFA_BASE}/sport/football/events/live"
    try:
        _rate_limit_sleep(1.0)
        r = requests.get(url, headers=HEADERS, timeout=10)
        last_fetch_ts = time.time()
        if DEBUG:
            print(f"[INFO] live GET status: {r.status_code}")
        if r.status_code == 200:
            j = r.json() or {}
            return j.get("events", []) or []
        if r.status_code in (429, 403, 500, 502, 503, 504):
            if DEBUG:
                body = (r.text or "")[:120].replace("\n", " ")
                print(f"[WARN] live GET {r.status_code}: {body}")
            return []
        if DEBUG:
            print(f"[WARN] live GET unexpected {r.status_code}")
        return []
    except Exception as e:
        if DEBUG:
            print(f"[WARN] live GET exception: {e}")
        return []

def get_stats(event_id: int):
    url = f"{SOFA_BASE}/event/{event_id}/statistics"
    return get_json(url, min_interval=1.0) or {}

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
    t0 = last_alert_ts.get(event_id, 0.0)
    return (time.time() - t0) > (ALERT_COOLDOWN_MIN * 60.0)

def _update_goal_cooloff(eid: int, home_goals: int, away_goals: int):
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
#    ESTRAZIONE “MINUTE”
# =============================

def extract_minute(ev: dict) -> int:
    """
    Tenta più fonti per il minuto live, in ordine:
    1) time.minute
    2) status.elapsed / status.minute
    3) clock.sec // 60
    4) currentPeriodStartTimestamp (+45' se 2° tempo)
    5) fallback dallo status (1st/2nd) o 'inprogress' → 30/60/75
    """
    # 1) time.minute
    m = (ev.get("time", {}) or {}).get("minute")
    if isinstance(m, (int, float)) and m > 0:
        return int(m)

    # 2) status.elapsed / status.minute
    st = (ev.get("status", {}) or {})
    for k in ("elapsed", "minute"):
        v = st.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)

    # 3) clock.sec
    sec = (ev.get("clock", {}) or {}).get("sec")
    if isinstance(sec, (int, float)) and sec > 0:
        return int(sec // 60)

    # 4) currentPeriodStartTimestamp (+45’ se 2° tempo)
    try:
        cps = (ev.get("time", {}) or {}).get("currentPeriodStartTimestamp")
        if isinstance(cps, (int, float)) and cps > 0:
            now_ts = int(time.time())
            elapsed = max(0, now_ts - int(cps))
            base_min = int(elapsed // 60)
            s = " ".join([
                str(st.get("type") or ""),
                str(st.get("short") or ""),
                str(st.get("description") or "")
            ]).lower()
            if "2nd" in s or "second" in s or "2ndhalf" in s or "second-half" in s:
                base_min += 45
            return base_min
    except Exception:
        pass

    # 5) fallback dallo status
    s = " ".join([
        str(st.get("type") or ""),
        str(st.get("short") or ""),
        str(st.get("description") or "")
    ]).lower()

    if any(k in s for k in ("1st", "first", "1sthalf", "first-half")):
        return 30
    if any(k in s for k in ("2nd", "second", "2ndhalf", "second-half")):
        return 75
    if "inprogress" in s or "period" in s or "live" in s:
        if DEBUG:
            print("[DBG] minute non rilevato ma status live → fallback 60'")
        return 60

    return 0


# =============================
#        FORMAT ALERT
# =============================

def format_alert(home, away, sh, sa, minute, p, st) -> str:


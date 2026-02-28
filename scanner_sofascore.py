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
    Prova più fonti per ottenere il minuto live:
    1) time.minute
    2) clock.sec // 60
    3) currentPeriodStartTimestamp + status (stima)
    4) fallback grossolano in base a '1st half' / '2nd half'
    """
    # 1) standard
    minute = (ev.get("time", {}) or {}).get("minute")
    if isinstance(minute, (int, float)) and minute > 0:
        return int(minute)
    # 2) clock.sec
    sec = (ev.get("clock", {}) or {}).get("sec")
    try:
        if isinstance(sec, (int, float)) and sec > 0:
            return int(sec // 60)
    except Exception:
        pass
    # 3) stima con currentPeriodStartTimestamp
    try:
        cps = (ev.get("time", {}) or {}).get("currentPeriodStartTimestamp")
        if isinstance(cps, (int, float)) and cps > 0:
            now_ts = int(time.time())
            elapsed = max(0, now_ts - int(cps))
            base_min = int(elapsed // 60)
            # se 2° tempo, aggiungiamo 45'
            status = (ev.get("status", {}) or {}).get("type") \
                     or (ev.get("status", {}) or {}).get("description") \
                     or ""
            s = str(status).lower()
            if "2nd" in s or "second" in s:
                base_min += 45
            return base_min
    except Exception:
        pass
    # 4) fallback grossolano dallo status
    status = (ev.get("status", {}) or {}).get("type") \
             or (ev.get("status", {}) or {}).get("description") \
             or ""
    s = str(status).lower()
    if "1st" in s or "first" in s:
        return 30   # stima conservativa per far passare il filtro
    if "2nd" in s or "second" in s:
        return 75
    return 0


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

    global _force_sent

    # Alert di prova una‑tantum (se abilitato)
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

    # Diagnostica status
    status_counts = Counter()
    inprog_est = 0
    for _ev in events:
        s = (_ev.get("status", {}) or {}).get("type") \
            or (_ev.get("status", {}) or {}).get("short") \
            or (_ev.get("status", {}) or {}).get("description") \
            or "unknown"
        s_l = str(s).lower()
        status_counts[s_l] += 1
        if any(k in s_l for k in ("inprogress", "period", "1st half", "2nd half", "1sthalf", "2ndhalf")):
            inprog_est += 1
    if DEBUG:
        top5 = ", ".join([f"{k}:{v}" for k, v in status_counts.most_common(5)])
        print(f"[INFO] Distribuzione status (top5): {top5}")
        print(f"[INFO] In progress stimati: {inprog_est}")

    # --- Pre‑selezione candidati (TOP_K) ---
    candidates = []
    for ev in events:
        try:
            if (ev.get("sport", {}) or {}).get("slug") != "football":
                continue

            status = (ev.get("status", {}) or {}).get("type") \
                     or (ev.get("status", {}) or {}).get("description") \
                     or ""
            s_norm = str(status).lower()

            minute = extract_minute(ev)

            # criterio di ammissione
            admitted = False
            if USE_MINUTE_ONLY:
                admitted = minute > 0
            else:
                admitted = (("inprogress" in s_norm or "period" in s_norm or "1st" in s_norm or "2nd" in s_norm)
                            and minute > 0)
            if not admitted:
                continue

            # priorità semplice: minuto alto + punteggio ravvicinato
            sh = (ev.get("homeScore", {}) or {}).get("current", 0) or 0
            sa = (ev.get("awayScore", {}) or {}).get("current", 0) or 0
            score_shape = 1 if abs(int(sh) - int(sa)) <= 1 else 0
            late_bonus  = 1 if minute >= 70 else 0
            activity    = 1 if (int(sh) + int(sa)) >= 2 else 0
            priority    = (late_bonus * 3) + (score_shape * 2) + activity

            candidates.append((priority, minute, ev))
        except Exception:
            continue

    # ordina per priorità (desc), poi per minuto (desc)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    to_process = candidates[:max(1, TOP_K)]
    if DEBUG:
        print(f"[INFO] Candidati pre-selezionati: {len(to_process)} (TOP_K={TOP_K})")

    # --- Diagnostica: track best-of-cycle ---
    best = {"p": -1.0, "label": ""}

    # Elabora solo i top K
    for _, minute, ev in to_process:
        try:
            eid  = ev.get("id")
            if not eid:
                continue

            home = (ev.get("homeTeam", {}) or {}).get("name", "Home")
            away = (ev.get("awayTeam", {}) or {}).get("name", "Away")

            sh   = (ev.get("homeScore", {}) or {}).get("current", 0) or 0
            sa   = (ev.get("awayScore", {}) or {}).get("current", 0) or 0

            _update_goal_cooloff(eid, sh, sa)

            st_json = get_stats(eid) or {}
            st_now  = parse_stats(st_json)
            feats   = recent_features(eid, st_now)
            p_goal  = goal_prob_next_15(st_now, feats, minute)

            if DEBUG:
                try:
                    print(f"[DBG] {home}-{away}  min {minute}  P(goal15)={p_goal:.2%}")
                except Exception:
                    pass

            if p_goal > best["p"]:
                best["p"] = p_goal
                best["label"] = f"{home}-{away}  min {minute}  P={p_goal:.2%}"

            if p_goal >= GOAL_PROB_THRESH and should_alert(eid) and not _in_goal_cooloff(eid):
                last_alert_ts[eid] = time.time()
                tg_send(format_alert(home, away, sh, sa, minute, p_goal, st_now))

        except Exception as e:
            if DEBUG:
                print(f"[WARN] ciclo evento {ev.get('id')}: {e}")
            continue

    if DEBUG and best["p"] >= 0.0:
        print(f"[INFO] Miglior candidato ciclo: {best['label']}")

    # Calibrazione: 1 alert best-of-cycle ogni N minuti se sopra FLOOR_PROB
    global _last_floor_send
    if CALIBRATION and best["p"] >= FLOOR_PROB:
        now = time.time()
        if (now - _last_floor_send) > (FLOOR_EVERY_MIN * 60):
            tg_send(f"🟡 Calibrazione: {best['label']}\n— floor {FLOOR_PROB:.0%}, max 1 ogni {FLOOR_EVERY_MIN}′")
            _last_floor_send = now

    _maybe_heartbeat()


# =============================
#              MAIN
# =============================

if __name__ == "__main__":
    try:
        tg_send("🟢 Scanner SofaScore (Railway) avviato.")
        if DEBUG:
            print(f"[INFO] Soglia corrente: {GOAL_PROB_THRESH}")
            print(f"[INFO] Polling ogni: {POLL_SEC}s | Finestra: {WINDOW_START_H}-{WINDOW_END_H}")
            print(f"[INFO] USE_MINUTE_ONLY: {USE_MINUTE_ONLY} | CALIBRATION: {CALIBRATION} | FLOOR: {FLOOR_PROB}")
            print(f"[INFO] TOP_K: {TOP_K}")
    except Exception as e:
        print(f"[WARN] Avvio Telegram: {e}")

    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"[WARN] ciclo principale: {e}")
            time.sleep(2)
        time.sleep(POLL_SEC)

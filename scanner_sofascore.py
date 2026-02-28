import os, time, math, json, datetime
import requests
from collections import deque, defaultdict
from pytz import timezone
# --- DEBUG & TEST SWITCHES ---
DEBUG = os.environ.get("DEBUG") == "1"
FORCE_ALERT = os.environ.get("FORCE_ALERT") == "1"
_force_sent = False
# === Config ===
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "@pxmx79")
SOFA_BASE      = "https://api.sofascore.com/api/v1"   # endpoint JSON non ufficiali
TZ             = timezone("Europe/Rome")

# Finestra operativa + soglie
START_H = int(os.environ.get("WINDOW_START_H", "10"))
END_H   = int(os.environ.get("WINDOW_END_H",   "23"))
P_THRESH = float(os.environ.get("GOAL_PROB_THRESH", "0.75"))  # ≈ quota 1.33
POLL_SEC = int(os.environ.get("POLL_SEC", "45"))              # 45–60 consigliati

# Rate limit & headers (rispettiamo 1 req/s complessivo)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (GoalAlertBot; +https://t.me/pxmx79)"
}

# Cache (per momentum e anti-spam)
last_alert_ts   = {}
recent_windows  = defaultdict(lambda: deque(maxlen=30))  # ~15–20' di history se POLL_SEC ~30–45
last_fetch_ts   = 0.0

def within_window():
    now = datetime.datetime.now(TZ).time()
    return datetime.time(START_H, 0) <= now <= datetime.time(END_H, 0)

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass

def get_json(url):
    # rispettiamo ~1 req/s totale
    global last_fetch_ts
    delta = time.time() - last_fetch_ts
    if delta < 1.0:
        time.sleep(1.0 - delta)
    r = requests.get(url, headers=HEADERS, timeout=10)
    last_fetch_ts = time.time()
    r.raise_for_status()
    return r.json()

def get_live_events():
    # Tutti i match live di calcio
    # https://api.sofascore.com/api/v1/sport/football/events/live
    return get_json(f"{SOFA_BASE}/sport/football/events/live").get("events", [])  # [1](https://help.pythonanywhere.com/pages/AlwaysOnTasks/)

def get_stats(event_id):
    # Statistiche live del match (tiri, SOT, corner, possesso, big chances se disponibili)
    # https://api.sofascore.com/api/v1/event/{id}/statistics
    return get_json(f"{SOFA_BASE}/event/{event_id}/statistics")  # [2](https://www.pythonanywhere.com/pricing/)

def get_incidents(event_id):
    # Timeline/incidenti (gol, rossi, rigori, sostituzioni)
    # https://api.sofascore.com/api/v1/event/{id}/incidents
    return get_json(f"{SOFA_BASE}/event/{event_id}/incidents")   # [1](https://help.pythonanywhere.com/pages/AlwaysOnTasks/)

def parse_stats(stats_json):
    # Normalizza in un dict coerente: shots/sot/corners/possession/big chances
    out = {"home": {}, "away": {}}
    groups = []
    for blk in stats_json.get("statistics", []):
        if blk.get("period") == "ALL":
            groups = blk.get("groups", [])
            break
    for g in groups:
        for it in g.get("statisticsItems", []):
            key = (it.get("key") or it.get("name","")).lower()
            hv  = it.get("homeValue") if "homeValue" in it else it.get("home")
            av  = it.get("awayValue") if "awayValue" in it else it.get("away")
            # mapping minimale
            if "shots on target" in key or "shotsongoal" in key or "sot" in key:
                out["home"]["sot"] = safe_num(hv); out["away"]["sot"] = safe_num(av)
            elif "shots" in key and "on" not in key:
                out["home"]["shots"] = safe_num(hv); out["away"]["shots"] = safe_num(av)
            elif "corner" in key:
                out["home"]["corners"] = safe_num(hv); out["away"]["corners"] = safe_num(av)
            elif "possession" in key:
                out["home"]["poss"] = pct_to_float(hv); out["away"]["poss"] = pct_to_float(av)
            elif "big chances" in key:
                out["home"]["big"] = safe_num(hv); out["away"]["big"] = safe_num(av)
    return out

def safe_num(v):
    try:
        if isinstance(v, str) and "/" in v:
            v = v.split("(")[0].split("/")[0].strip()
        return int(v)
    except:
        try:
            return float(v)
        except:
            return None

def pct_to_float(v):
    if isinstance(v, str) and "%" in v:
        try: return float(v.replace("%",""))/100.0
        except: return None
    if isinstance(v, (int,float)):
        return float(v)/100.0 if v > 1 else float(v)
    return None

def recent_features(event_id, base_stats):
    recent_windows[event_id].append(base_stats)
    q = list(recent_windows[event_id])
    if len(q) < 4:  # serve un minimo di storia
        return {}
    def delta(key):
        vals_h = [x["home"].get(key,0) for x in q]
        vals_a = [x["away"].get(key,0) for x in q]
        return (vals_h[-1]-vals_h[0], vals_a[-1]-vals_a[0])
    return {
        "d_shots": delta("shots"),
        "d_sot":   delta("sot"),
        "d_corn":  delta("corners"),
    }

def goal_prob_next_15(stats_now, feats_recent, minute):
    # euristica logistica calibrata per soglia ~0.75
    shots_tot = sum([stats_now["home"].get("shots",0) or 0, stats_now["away"].get("shots",0) or 0])
    sot_tot   = sum([stats_now["home"].get("sot",0)   or 0, stats_now["away"].get("sot",0)   or 0])
    corners   = sum([stats_now["home"].get("corners",0) or 0, stats_now["away"].get("corners",0) or 0])
    bigc      = sum([stats_now["home"].get("big",0)   or 0, stats_now["away"].get("big",0)   or 0])

    d_sh_h, d_sh_a = feats_recent.get("d_shots", (0,0))
    d_so_h, d_so_a = feats_recent.get("d_sot",   (0,0))
    d_co_h, d_co_a = feats_recent.get("d_corn",  (0,0))

    minute_factor = 1.0
    if 22 <= minute <= 44 or 55 <= minute <= 88: minute_factor = 1.15
    if minute >= 85: minute_factor = 1.25

    score = (
        0.08*shots_tot + 0.22*sot_tot + 0.07*corners + 0.18*bigc +
        0.10*(d_sh_h + d_sh_a) + 0.20*(d_so_h + d_so_a) + 0.05*(d_co_h + d_co_a)
    ) * minute_factor

    prob = 1 / (1 + math.exp(-(score - 2.4)))
    return max(0.0, min(1.0, prob))

def should_alert(event_id):
    t = last_alert_ts.get(event_id, 0)
    return (time.time() - t) > 12*60   # max 1 alert / 12 minuti per match

def format_alert(home, away, sh, sa, minute, p, st):
    sot_h = st["home"].get("sot","-"); sot_a = st["away"].get("sot","-")
    sh_h  = st["home"].get("shots","-"); sh_a  = st["away"].get("shots","-")
    co_h  = st["home"].get("corners","-"); co_a  = st["away"].get("corners","-")
    bc_h  = st["home"].get("big","-") or "-"
    bc_a  = st["away"].get("big","-") or "-"
    return (
        f"⚽ <b>{home} {sh}-{sa} {away}</b>  ⏱️ {minute}'\n"
        f"• Prob. gol prossimi 15': <b>{p:.0%}</b>\n"
        f"• Tiri (SOT): {sh_h}({sot_h}) – {sh_a}({sot_a})\n"
        f"• Corner: {co_h} – {co_a} | Big chances: {bc_h} – {bc_a}\n"
        f"— filtro 1.33 attivo, alert 1–2 gol"
    )

def run_cycle():
    if not within_window():
        return

    # FORCED TEST: invia un solo alert di prova alla prima passata se FORCE_ALERT=1
    global _force_sent
    if FORCE_ALERT and not _force_sent:
        tg_send("🔔 FORCED TEST ALERT — percorso interno OK")
        _force_sent = True

    # Leggi eventi live in modo sicuro
    try:
        events = get_live_events() or []
    except Exception as e:
        if DEBUG:
            print(f"[WARN] get_live_events: {e}")
        return

    if DEBUG:
        print(f"[INFO] Eventi live trovati: {len(events)}")

            eid  = ev["id"]
            home = ev.get("homeTeam", {}).get("name","Home")
            away = ev.get("awayTeam", {}).get("name","Away")
            sh   = ev.get("homeScore", {}).get("current", 0)
            sa   = ev.get("awayScore", {}).get("current", 0)

            st_json = get_stats(eid)       # stats JSON (ALL-period)  [2](https://www.pythonanywhere.com/pricing/)
            st_now  = parse_stats(st_json)
            feats   = recent_features(eid, st_now)

            # (facoltativo) incidents_json = get_incidents(eid)  # timeline, se vuoi raffinare  [1](https://help.pythonanywhere.com/pages/AlwaysOnTasks/)
            p_goal  = goal_prob_next_15(st_now, feats, int(minute))

            if p_goal >= P_THRESH and should_alert(eid):
                last_alert_ts[eid] = time.time()
                tg_send(format_alert(home, away, sh, sa, minute, p_goal, st_now))
        except Exception:
            # log minimale (silenzioso in produzione)
            pass

if __name__ == "__main__":
    tg_send("🟢 Scanner SofaScore (Render) avviato.")
    while True:
        try:
            run_cycle()
        except Exception:
            time.sleep(2)
        time.sleep(POLL_SEC)

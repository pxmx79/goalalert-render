import os, time, math, datetime, requests
from pytz import timezone

API_BASE = "https://v3.football.api-sports.io"
API_KEY  = os.environ["APIFOOTBALL_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT  = os.environ.get("TELEGRAM_CHAT", "@pxmx79")
TZ = timezone("Europe/Rome")

# Finestra operativa e parametri
START_H = int(os.environ.get("WINDOW_START_H", "10"))
END_H   = int(os.environ.get("WINDOW_END_H",   "23"))
P_THRESH = float(os.environ.get("GOAL_PROB_THRESH", "0.75"))
POLL_SEC = int(os.environ.get("POLL_SEC", "45"))

HEADERS = {"x-apisports-key": API_KEY}

def within_window():
    now = datetime.datetime.now(TZ).time()
    return datetime.time(START_H,0) <= now <= datetime.time(END_H,0)

def tg_send(text):
    try:
        requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     params={"chat_id": TG_CHAT, "text": text, "parse_mode":"HTML"}, timeout=10)
    except:
        pass

def get_live_fixtures():
    r = requests.get(f"{API_BASE}/fixtures", params={"live":"all"}, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("response", []), r.headers

def get_stats(fixture_id):
    r = requests.get(f"{API_BASE}/fixtures/statistics", params={"fixture": fixture_id},
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("response", [])

def parse_stats(stats_resp):
    out = {"home":{}, "away":{}}
    if len(stats_resp)>=1:
        for it in stats_resp[0].get("statistics", []):
            k, v = (it.get("type","").lower(), it.get("value"))
            out["home"][k] = v
    if len(stats_resp)>=2:
        for it in stats_resp[1].get("statistics", []):
            k, v = (it.get("type","").lower(), it.get("value"))
            out["away"][k] = v

    def get_num(d, *keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v,(int,float)):
                return v
        return None

    return {
        "home":{
            "shots": get_num(out["home"], "total shots"),
            "sot":   get_num(out["home"], "shots on goal"),
            "corn":  get_num(out["home"], "corner kicks"),
            "poss":  out["home"].get("ball possession")
        },
        "away":{
            "shots": get_num(out["away"], "total shots"),
            "sot":   get_num(out["away"], "shots on goal"),
            "corn":  get_num(out["away"], "corner kicks"),
            "poss":  out["away"].get("ball possession")
        }
    }

def goal_prob_15(stats, minute):
    shots_tot = sum([x for x in [stats["home"]["shots"], stats["away"]["shots"]] if isinstance(x,(int,float))])
    sot_tot   = sum([x for x in [stats["home"]["sot"],   stats["away"]["sot"]]   if isinstance(x,(int,float))])
    corners   = sum([x for x in [stats["home"]["corn"],  stats["away"]["corn"]]  if isinstance(x,(int,float))])

    minute_factor = 1.0
    if 22 <= minute <= 44 or 55 <= minute <= 88:
        minute_factor = 1.15
    if minute >= 85:
        minute_factor = 1.25

    score = (0.08*shots_tot + 0.22*sot_tot + 0.07*corners) * minute_factor
    prob  = 1/(1+math.exp(-(score - 2.2)))
    return max(0.0, min(1.0, prob))

def run_cycle():
    if not within_window():
        return

    fixtures, headers = get_live_fixtures()

    for fx in fixtures:
        try:
            status = fx["fixture"]["status"]["short"]
            minute = fx["fixture"]["status"].get("elapsed") or 0
            if status not in ("1H","2H","ET"):
                continue
            if not (20 <= int(minute) <= 88):
                continue

            fid  = fx["fixture"]["id"]
            home = fx["teams"]["home"]["name"]
            away = fx["teams"]["away"]["name"]
            sh   = fx["goals"]["home"] or 0
            sa   = fx["goals"]["away"] or 0

            st = parse_stats(get_stats(fid))
            p  = goal_prob_15(st, int(minute))

            if p >= P_THRESH:
                msg = (
                    f"⚽ <b>{home} {sh}-{sa} {away}</b>  ⏱️ {minute}'\n"
                    f"• Prob. gol prossimi 15': <b>{p:.0%}</b>\n"
                    f"• Tiri (SOT): {st['home']['shots'] or '-'}({st['home']['sot'] or '-'}) – "
                    f"{st['away']['shots'] or '-'}({st['away']['sot'] or '-'})\n"
                    f"• Corner: {st['home']['corn'] or '-'} – {st['away']['corn'] or '-'}\n"
                    f"— filtro 1.33 attivo"
                )
                tg_send(msg)

        except Exception:
            continue

if __name__ == "__main__":
    tg_send("🟢 Scanner API‑Football (Render) avviato.")
    while True:
        run_cycle()
        time.sleep(POLL_SEC)

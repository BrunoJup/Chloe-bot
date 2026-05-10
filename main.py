import os
import json
import math
import base64
import requests
from flask import Flask, request

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL")

app = Flask(__name__)

# =========================
# TELEGRAM SEND
# =========================
def send(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
    except:
        pass

# =========================
# AUTO WEBHOOK MANAGER
# =========================
def get_webhook():
    return requests.get(f"{TELEGRAM_API}/getWebhookInfo").json()

def set_webhook(url):
    return requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": url}).json()

def delete_webhook():
    return requests.get(f"{TELEGRAM_API}/deleteWebhook").json()

def ensure_webhook():
    if not BASE_URL:
        print("⚠️ No BASE_URL found")
        return

    url = f"{BASE_URL}/"

    info = get_webhook()
    current = info.get("result", {}).get("url", "")

    if current != url:
        print("🔄 Fixing webhook...")
        delete_webhook()
        set_webhook(url)
        print("✅ Webhook set:", url)
    else:
        print("✅ Webhook OK")

# =========================
# GPT-4o VISION EXTRACTION
# =========================
def extract_matches(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()

    prompt = """
Extract ALL matches from this eFootball screenshot.

Return ONLY JSON:

{
  "matches": [
    {
      "league": "",
      "home_team": "",
      "away_team": "",
      "home_last_matches": ["2-1","1-0","3-2","2-2","0-1"],
      "away_last_matches": ["1-1","0-2","2-1","1-0","2-3"],
      "odds": {
        "home_win": null,
        "draw": null,
        "away_win": null,
        "over_2_5": null,
        "under_2_5": null,
        "btts_yes": null,
        "btts_no": null
      }
    }
  ]
}

Rules:
- Extract ALL visible matches
- Do NOT guess missing values
- Normalize scores X-Y
- Convert odds to decimal
"""

    res = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "openai/gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            }
                        }
                    ]
                }
            ],
            "temperature": 0
        }
    )

    return res.json()["choices"][0]["message"]["content"]

# =========================
# MATH ENGINE
# =========================
def poisson(l, k):
    return (math.exp(-l) * l**k) / math.factorial(k)

def simulate(home_l, away_l):
    probs = {
        "home_win": 0,
        "draw": 0,
        "away_win": 0,
        "over_2_5": 0,
        "under_2_5": 0,
        "btts_yes": 0,
        "btts_no": 0
    }

    for h in range(6):
        for a in range(6):
            p = poisson(home_l, h) * poisson(away_l, a)

            if h > a:
                probs["home_win"] += p
            elif h == a:
                probs["draw"] += p
            else:
                probs["away_win"] += p

            if h + a > 2:
                probs["over_2_5"] += p
            else:
                probs["under_2_5"] += p

            if h > 0 and a > 0:
                probs["btts_yes"] += p
            else:
                probs["btts_no"] += p

    return probs

# =========================
# FILTERS
# =========================
def valid(prob, odds, ev):
    if ev < 0.05:
        return False
    if prob > 0.80:
        return False
    return True

# =========================
# BEST PICK
# =========================
def pick_best(probs, odds, data):
    best = None
    best_score = -999

    for k, p in probs.items():
        if k not in odds or odds[k] is None:
            continue

        ev = (p * odds[k]) - 1

        if not valid(p, odds[k], ev):
            continue

        score = ev * 0.7 + p * 0.3

        if score > best_score:
            best_score = score
            best = (k, p, odds[k], ev)

    return best

# =========================
# UI CARD
# =========================
def clean(m):
    return {
        "home_win": "HOME WIN",
        "away_win": "AWAY WIN",
        "draw": "DRAW",
        "over_2_5": "OVER 2.5",
        "under_2_5": "UNDER 2.5",
        "btts_yes": "BTTS YES",
        "btts_no": "BTTS NO"
    }.get(m, m.upper())

def card(market, prob, odds, ev, league, home, away):
    return (
        "╔═══════════════════╗\n"
        "   ⚡ ELITE SIGNAL\n"
        "╚═══════════════════╝\n\n"
        f"🏟️ {home} vs {away}\n"
        f"🌍 {league}\n\n"
        f"🎯 {clean(market)}\n"
        f"💸 {odds}\n\n"
        f"📊 {round(prob*100,1)}% │ 📈 +{round(ev*100,1)}%\n"
    )

PASS = (
    "╔═══════════════════╗\n"
    "   🚫 NO EDGE\n"
    "╚═══════════════════╝"
)

# =========================
# WEBHOOK
# =========================
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.json
        if "message" not in data:
            return "ok"

        msg = data["message"]
        chat_id = msg["chat"]["id"]

        if "photo" not in msg:
            send(chat_id, "📸 Send screenshot")
            return "ok"

        file_id = msg["photo"][-1]["file_id"]

        file_info = requests.get(
            f"{TELEGRAM_API}/getFile?file_id={file_id}"
        ).json()

        file_path = file_info["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

        img = requests.get(file_url).content

        send(chat_id, "🔍 Analyzing...")

        raw = extract_matches(img)
        parsed = json.loads(raw)

        results = []

        for m in parsed.get("matches", []):
            home = m["home_team"]
            away = m["away_team"]

            hs = [int(x.split("-")[0]) for x in m["home_last_matches"]]
            as_ = [int(x.split("-")[0]) for x in m["away_last_matches"]]

            if len(hs) < 4:
                continue

            home_l = sum(hs) / len(hs)
            away_l = sum(as_) / len(as_)

            probs = simulate(home_l, away_l)

            best = pick_best(probs, m["odds"], m)

            if best:
                market, prob, odds, ev = best

                results.append(card(
                    market,
                    prob,
                    odds,
                    ev,
                    m["league"],
                    home,
                    away
                ))

        if not results:
            send(chat_id, PASS)
        else:
            for r in results[:3]:
                send(chat_id, r)

    except Exception:
        send(chat_id, "❌ Error")

    return "ok"

# =========================
# START
# =========================
if __name__ == "__main__":
    print("🚀 Bot starting...")
    ensure_webhook()
    app.run(host="0.0.0.0", port=10000)

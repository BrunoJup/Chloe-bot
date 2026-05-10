import os
import json
import math
import base64
import re
import requests
from flask import Flask, request
from PIL import Image
from io import BytesIO

# =========================
# ENV & CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

app = Flask(__name__)

# =========================
# CORE MATH (Poisson & EV)
# =========================
def poisson(l, k):
    if l <= 0: return 0
    return (math.exp(-l) * (l**k)) / math.factorial(k)

def simulate(home_avg, away_avg):
    # Apply Home Field Advantage (10% boost to home scoring)
    h_lambda = home_avg * 1.1
    a_lambda = away_avg
    
    probs = {
        "home_win": 0, "draw": 0, "away_win": 0,
        "over_2_5": 0, "under_2_5": 0,
        "btts_yes": 0, "btts_no": 0
    }

    # Iterate up to 10 goals for higher precision
    for h in range(10):
        for a in range(10):
            p = poisson(h_lambda, h) * poisson(a_lambda, a)
            
            if h > a: probs["home_win"] += p
            elif h == a: probs["draw"] += p
            else: probs["away_win"] += p

            if h + a > 2.5: probs["over_2_5"] += p
            else: probs["under_2_5"] += p

            if h > 0 and a > 0: probs["btts_yes"] += p
            else: probs["btts_no"] += p
            
    return probs

def pick_best(probs, odds):
    best = None
    best_score = -999

    for k, p in probs.items():
        if k not in odds or not odds[k] or odds[k] <= 1:
            continue

        # Edge calculation (Expected Value)
        ev = (p * odds[k]) - 1

        # SETTING: 0.02 = 2% Edge. Lower this for even more signals.
        if ev < 0.02:
            continue

        # Ranking score: Balance high probability with high value
        score = (ev * 0.6) + (p * 0.4)

        if score > best_score:
            best_score = score
            best = (k, p, odds[k], ev)

    return best

# =========================
# DATA EXTRACTION
# =========================
def safe_json(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")
        return json.loads(text[start:end+1]) if (start != -1 and end != -1) else None
    except:
        return None

def parse_scores(arr):
    out = []
    for x in arr:
        try:
            clean_x = str(x).replace(":", "-").replace("–", "-")
            score_parts = clean_x.split("-")
            out.append(int(score_parts[0])) # Extract goals
        except:
            continue
    return out

def call_model(model, image_bytes):
    try:
        b64 = base64.b64encode(image_bytes).decode()
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract match data. JSON only: {league, home_team, away_team, home_last_matches: [scores like '2-1'], away_last_matches: [scores], odds: {home_win, draw, away_win, over_2_5, under_2_5, btts_yes, btts_no}}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ]
                }],
                "temperature": 0
            },
            timeout=30
        )
        return res.json()["choices"][0]["message"]["content"]
    except:
        return None

# =========================
# TELEGRAM UTILS
# =========================
def send(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

def split_image(image_bytes):
    img = Image.open(BytesIO(image_bytes))
    w, h = img.size
    boxes = [(0, 0, w//2, h//2), (w//2, 0, w, h//2), (0, h//2, w//2, h), (w//2, h//2, w, h)]
    parts = []
    for box in boxes:
        crop = img.crop(box)
        buf = BytesIO()
        crop.save(buf, format="JPEG")
        parts.append(buf.getvalue())
    return parts

# =========================
# ROUTES
# =========================
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    if "message" not in data or "photo" not in data["message"]:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    file_id = data["message"]["photo"][-1]["file_id"]
    
    # Download Image
    f_info = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    f_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f_info['result']['file_path']}"
    img_data = requests.get(f_url).content

    send(chat_id, "🔍 <b>Smart Analyzing...</b>")
    
    parts = split_image(img_data)
    results = []

    for part in parts:
        raw_res = call_model("openai/gpt-4o", part)
        parsed = safe_json(raw_res)

        if not parsed: continue

        h_scores = parse_scores(parsed.get("home_last_matches", []))
        a_scores = parse_scores(parsed.get("away_last_matches", []))

        # SETTING: Adjusted to 3 matches for higher activity
        if len(h_scores) >= 3 and len(a_scores) >= 3:
            h_lambda = sum(h_scores[:3]) / 3
            a_lambda = sum(a_scores[:3]) / 3

            probs = simulate(h_lambda, a_lambda)
            best = pick_best(probs, parsed.get("odds", {}))

            if best:
                m, p, o, ev = best
                card = (
                    f"<b>🏆 {parsed.get('home_team')} vs {parsed.get('away_team')}</b>\n"
                    f"<i>{parsed.get('league', 'International')}</i>\n\n"
                    f"🎯 <b>PICK:</b> {str(m).upper().replace('_', ' ')}\n"
                    f"💰 <b>ODDS:</b> {o}\n"
                    f"📈 <b>EDGE:</b> +{round(ev*100, 1)}%\n"
                    f"📊 <b>PROB:</b> {round(p*100, 1)}%"
                )
                results.append(card)

    if not results:
        send(chat_id, "🚫 <b>NO EDGE FOUND</b>\n(Odds are too tight or data insufficient)")
    else:
        for r in results:
            send(chat_id, r)

    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

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
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")

if not TELEGRAM_TOKEN:
    raise Exception("Missing TELEGRAM_BOT_TOKEN")

if not OPENROUTER_API_KEY:
    raise Exception("Missing OPENROUTER_API_KEY")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

app = Flask(__name__)

# =========================
# SEND
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
# WEBHOOK
# =========================
def ensure_webhook():
    if not BASE_URL:
        return

    url = f"{BASE_URL}/"
    try:
        info = requests.get(f"{TELEGRAM_API}/getWebhookInfo").json()
        current = info.get("result", {}).get("url", "")

        if current != url:
            requests.get(f"{TELEGRAM_API}/deleteWebhook")
            requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": url})
    except:
        pass

# =========================
# SAFE JSON
# =========================
def safe_json(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1:
            return None

        return json.loads(text[start:end+1])
    except:
        return None

# =========================
# SCORES
# =========================
def parse_scores(arr):
    out = []
    for x in arr:
        try:
            x = x.replace(":", "-").replace("–", "-")
            out.append(int(x.split("-")[0]))
        except:
            continue
    return out

# =========================
# IMAGE SPLIT
# =========================
def split_image(image_bytes):
    img = Image.open(BytesIO(image_bytes))
    w, h = img.size

    boxes = [
        (0, 0, w//2, h//2),
        (w//2, 0, w, h//2),
        (0, h//2, w//2, h),
        (w//2, h//2, w, h),
    ]

    parts = []
    for box in boxes:
        crop = img.crop(box)
        buf = BytesIO()
        crop.save(buf, format="JPEG")
        parts.append(buf.getvalue())

    return parts

# =========================
# MULTI MODEL EXTRACTION
# =========================
def call_model(model, image_bytes):
    try:
        b64 = base64.b64encode(image_bytes).decode()

        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Extract match data as JSON only"},
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
            },
            timeout=30
        )

        data = res.json()

        if "choices" not in data:
            print("MODEL ERROR:", data)
            return None

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("MODEL FAIL:", model, e)
        return None


def extract_with_fallback(image_bytes):
    models = [
        "openai/gpt-4o",
        "anthropic/claude-3.5-sonnet"
    ]

    for model in models:
        result = call_model(model, image_bytes)
        if result:
            parsed = safe_json(result)
            if parsed:
                return parsed

    return None

# =========================
# POISSON
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
# PICK
# =========================
def pick_best(probs, odds):
    best = None
    best_score = -999

    for k, p in probs.items():
        if k not in odds or odds[k] is None:
            continue

        ev = (p * odds[k]) - 1

        if ev < 0.05:
            continue

        score = ev * 0.7 + p * 0.3

        if score > best_score:
            best_score = score
            best = (k, p, odds[k], ev)

    return best

# =========================
# UI
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
    }.get(m, m)

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

PASS = "🚫 NO EDGE"

# =========================
# ROUTES
# =========================
@app.route("/health")
def health():
    return "OK"

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

        send(chat_id, "🔍 Smart analyzing...")

        parts = split_image(img)
        results = []

        for part in parts:
            parsed = extract_with_fallback(part)

            if not parsed:
                continue

            hs = parse_scores(parsed.get("home_last_matches", []))
            as_ = parse_scores(parsed.get("away_last_matches", []))

            if len(hs) < 4 or len(as_) < 4:
                continue

            home_l = sum(hs) / len(hs)
            away_l = sum(as_) / len(as_)

            probs = simulate(home_l, away_l)
            best = pick_best(probs, parsed.get("odds", {}))

            if best:
                market, prob, odds, ev = best
                results.append(card(
                    market,
                    prob,
                    odds,
                    ev,
                    parsed.get("league", ""),
                    parsed.get("home_team", ""),
                    parsed.get("away_team", "")
                ))

        if not results:
            send(chat_id, PASS)
        else:
            for r in results:
                send(chat_id, r)

    except Exception as e:
        print("FATAL ERROR:", e)
        send(chat_id, "❌ Error")

    return "ok"

# =========================
# START
# =========================
if __name__ == "__main__":
    print("🚀 Starting multi-model bot...")
    ensure_webhook()
    app.run(host="0.0.0.0", port=10000)

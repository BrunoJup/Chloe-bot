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
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

app = Flask(__name__)

# =========================
# MATH ENGINE
# =========================
def poisson(l, k):
    if l <= 0: return 0.0001 # Prevent math errors
    return (math.exp(-l) * (l**k)) / math.factorial(k)

def simulate(h_avg, a_avg):
    # Apply Home Advantage (10%)
    h_l = h_avg * 1.1
    a_l = a_avg
    
    probs = {"home_win": 0, "draw": 0, "away_win": 0, "over_2_5": 0, "under_2_5": 0, "btts_yes": 0, "btts_no": 0}

    for h in range(12): # High range for accuracy
        for a in range(12):
            p = poisson(h_l, h) * poisson(a_l, a)
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
    # ULTRA-ACTIVE SETTING: 0.1% Edge
    MIN_EDGE = 0.001 

    for k, p in probs.items():
        if k not in odds or not odds[k] or odds[k] <= 1: continue
        ev = (p * odds[k]) - 1
        if ev < MIN_EDGE: continue
        
        score = (ev * 0.5) + (p * 0.5)
        if score > best_score:
            best_score = score
            best = (k, p, odds[k], ev)
    return best

# =========================
# VISION ENGINE (Gemini + GPT-4o)
# =========================
def call_openrouter(model, image_bytes):
    try:
        b64 = base64.b64encode(image_bytes).decode()
        prompt = (
            "Analyze this betting slip quadrant. Extract: League, Home Team, Away Team. "
            "Find last match scores for both (e.g. '2:1'). Find odds for 1, X, 2, Over 2.5, Under 2.5, BTTS Yes/No. "
            "Return ONLY JSON format."
        )
        
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ]
                }],
                "temperature": 0.1
            },
            timeout=45
        )
        return res.json()["choices"][0]["message"]["content"]
    except:
        return None

def extract_with_fallback(image_bytes):
    # 1. Try Gemini 1.5 Pro (Best for OCR/Dense Text)
    res = call_openrouter("google/gemini-pro-1.5", image_bytes)
    parsed = safe_json(res)
    if parsed: return parsed

    # 2. Fallback to GPT-4o
    res = call_openrouter("openai/gpt-4o", image_bytes)
    return safe_json(res)

def safe_json(text):
    if not text: return None
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end+1]) if start != -1 else None
    except:
        return None

def parse_scores(arr):
    out = []
    for x in arr:
        try:
            # Clean and take the first number (Home goals)
            s = str(x).replace(":", "-").replace("–", "-").split("-")[0]
            out.append(int(s))
        except: continue
    return out

# =========================
# TELEGRAM / IMAGE PROCESSING
# =========================
def split_image(image_bytes):
    img = Image.open(BytesIO(image_bytes))
    w, h = img.size
    boxes = [(0, 0, w//2, h//2), (w//2, 0, w, h//2), (0, h//2, w//2, h), (w//2, h//2, w, h)]
    return [save_crop(img, b) for b in boxes]

def save_crop(img, box):
    crop = img.crop(box)
    buf = BytesIO()
    crop.save(buf, format="JPEG", quality=95)
    return buf.getvalue()

@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    if "message" not in data or "photo" not in data["message"]: return "ok"
    
    chat_id = data["message"]["chat"]["id"]
    file_id = data["message"]["photo"][-1]["file_id"]
    
    # Download
    f_info = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    img_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f_info['result']['file_path']}"
    img_data = requests.get(img_url).content

    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": "🤖 <b>Gemini Pro Analyzing...</b>", "parse_mode": "HTML"})
    
    parts = split_image(img_data)
    found_any = False

    for part in parts:
        data = extract_with_fallback(part)
        if not data: continue

        h_s = parse_scores(data.get("home_last_matches", []))
        a_s = parse_scores(data.get("away_last_matches", []))

        # REQUIRES ONLY 1 MATCH TO START
        if len(h_s) >= 1 and len(a_s) >= 1:
            probs = simulate(sum(h_s)/len(h_s), sum(a_s)/len(a_s))
            best = pick_best(probs, data.get("odds", {}))

            if best:
                found_any = True
                m, p, o, ev = best
                msg = (
                    f"🔥 <b>SIGNAL: {data.get('home_team')} vs {data.get('away_team')}</b>\n"
                    f"🎯 <b>PICK:</b> {str(m).upper().replace('_', ' ')}\n"
                    f"💰 <b>ODDS:</b> {o}\n"
                    f"📊 <b>EDGE:</b> +{round(ev*100, 2)}%"
                )
                requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})

    if not found_any:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": "🚫 No clear edge or image too blurry."})

    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

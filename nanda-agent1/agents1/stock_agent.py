import os
from dotenv import load_dotenv, find_dotenv
from pathlib import Path
from anthropic import Anthropic

# Auto-discover .env from current working directory upward (e.g., nanda-agent or My_project)
dotenv_file = find_dotenv(filename=".env", usecwd=True)
if dotenv_file:
    load_dotenv(dotenv_file)
    print("Loaded .env from:", dotenv_file)
else:
    print("WARN: .env not found via find_dotenv")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
print("ANTHROPIC_KEY prefix:", (ANTHROPIC_KEY or "")[:10])  # should show 'sk-ant-'
claude = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None



# D:\My_project\nanda-agent\stock_agent.py
from flask import Flask, request, jsonify
# ↓↓↓ Replace with your actual utils package (e.g., if your file is in nanda-agent\agents1\stock_utils.py, write agents1)
from .stock_utils import extract_stock_symbols, get_stock_price

# === Added: load .env & initialize Claude SDK ===
import os
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
claude = Anthropic(api_key=ANTHROPIC_KEY)

app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status": "ok", "agent": "stock-agent"}), 200

@app.post("/invoke")
def invoke():
    data = request.get_json(force=True) or {}
    user_input = (data.get("message") or "").strip()

    # 1) If the message contains “stock” keywords → perform stock lookup
    if ("股票" in user_input) or ("stock" in user_input.lower()):
        syms = extract_stock_symbols(user_input)
        if not syms:
            return jsonify({"reply": "No stock symbol found (e.g., AAPL, TSLA)."}), 200
        lines = [get_stock_price(s) for s in syms]
        return jsonify({"reply": "\n".join(lines)}), 200

    # 2) Otherwise → forward to Claude
    if not ANTHROPIC_KEY:
        return jsonify({"reply": "Claude API Key not configured (set ANTHROPIC_API_KEY in .env)."}), 500

    try:
        resp = claude.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=400,
            messages=[{"role": "user", "content": user_input}],
        )
        text = resp.content[0].text if resp.content else "(empty)"
        return jsonify({"reply": text}), 200
    except Exception as e:
        return jsonify({"reply": f"Claude call failed: {e}"}), 500


@app.get("/")
def home():
    # A minimal HTML page: input text → call /invoke → display JSON response
    return """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Stock Agent</title></head>
<body style="font-family:sans-serif;max-width:720px;margin:40px auto;line-height:1.6">
  <h2>Stock Agent Demo</h2>
  <p>Enter your message below (e.g., "Check AAPL and TSLA stock prices").</p>
  <textarea id="msg" rows="3" style="width:100%;"></textarea><br/><br/>
  <button onclick="send()">Send</button>
  <pre id="out" style="background:#f6f8fa;padding:12px;border-radius:8px;white-space:pre-wrap;"></pre>
<script>
async function send() {
  const body = { message: document.getElementById('msg').value };
  const r = await fetch('/invoke', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const j = await r.json();
  document.getElementById('out').textContent = JSON.stringify(j, null, 2);
}
</script>
</body>
</html>
    """

if __name__ == "__main__":
    print("✅ Stock agent started at: http://127.0.0.1:8080")
    app.run(host="0.0.0.0", port=8080)   # change to 8081 if 8080 is already in use

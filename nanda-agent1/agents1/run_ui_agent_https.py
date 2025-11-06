# run_agent_ui.py - with Flask API wrapper for multiple HTTPS servers
import os
from dotenv import load_dotenv
load_dotenv()

import os
import subprocess
import time
import requests
import sys
import signal
import argparse
import threading
import json
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from python_a2a import A2AClient, Message, TextContent, MessageRole
from queue import Queue
from threading import Event
import ssl
import datetime
from anthropic import Anthropic
sys.stdout.reconfigure(line_buffering=True)
import re
# --- 修改：兼容导入，避免 attempted relative import 报错 ---
try:
    from .stock_service import quote as stock_quote, compare as stock_compare, help_text as stock_help
except Exception:
    try:
        from stock_service import quote as stock_quote, compare as stock_compare, help_text as stock_help
    except Exception:
        def stock_quote(ticker: str) -> str:
            return f"(stock service unavailable) Ticker: {ticker}"
        def stock_compare(t1: str, t2: str) -> str:
            return f"(stock service unavailable) Compare: {t1} vs {t2}"
        def stock_help() -> str:
            return "Commands:\n  /help\n  /quote <TICKER>\n  /compare <T1> <T2>\n  /weather <City>\n  /ask <question>\n"

# ★新增
from urllib.parse import quote as urlquote

# ✅ 自动加载 .env 里的变量
load_dotenv()


# ---- Minimal registry register helper (add once) ----
import requests, time, os

def _register_with_registry(agent_id: str, public_url: str, registry_url: str, tries: int = 3):
    if not registry_url or not public_url or not agent_id:
        print(f"[registry] skip: missing values agent_id={agent_id}, public_url={public_url}, registry_url={registry_url}")
        return False
    # 常见几个路径都试一下，避免兼容性问题
    paths = ["/register", "/api/register", "/agents/register", "/api/agents/register"]
    payload = {"agent_id": agent_id, "public_url": public_url}
    for attempt in range(1, tries + 1):
        for p in paths:
            url = registry_url.rstrip("/") + p
            try:
                print(f"[registry] try register {agent_id} => {public_url} via {url}")
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code < 300:
                    print(f"[registry] OK {r.status_code}: {r.text[:200]}")
                    return True
                else:
                    print(f"[registry] FAIL {r.status_code}: {r.text[:200]}")
            except Exception as e:
                print(f"[registry] EXC @ {url}: {e}")
        time.sleep(2 * attempt)
    print("[registry] all attempts failed")
    return False
# ---- end helper ----

# Global variables
bridge_process = None
registry_url = None
agent_id = None
agent_port = None
app = Flask(__name__)

# Enable CORS with support for credentials
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
    return response

# Message queues for SSE (Server-Sent Events)
# This allows us to push messages to the UI when they arrive
client_queues = {}

def cleanup(signum=None, frame=None):
    """Clean up processes on exit"""
    global bridge_process
    print("Cleaning up processes...")
    if bridge_process:
        try:
            bridge_process.terminate()
        except Exception:
            pass
    sys.exit(0)

def get_registry_url():
    """Get the registry URL from file or use default"""
    global registry_url
    if registry_url:
        return registry_url
    try:
        if os.path.exists("registry_url.txt"):
            with open("registry_url.txt", "r") as f:
                url = f.read().strip()
                print(f"Using registry URL from file: {url}")
                return url
    except Exception as e:
        print(f"Error reading registry URL: {e}")
    # Default if file doesn't exist
    print("Registry URL file not found. Using default: https://chat.nanda-registry.com:6900")
    return "https://chat.nanda-registry.com:6900"

def register_agent(agent_id, public_url):
    """Register the agent with the registry"""
    reg_url = get_registry_url()
    try:
        print(f"Registering agent {agent_id} at {public_url}")
        response = requests.post(
            f"{reg_url}/register", 
            json={"agent_id": agent_id, "agent_url": public_url},
            verify=False  # For development with self-signed certs
        )
        if response.status_code == 200:
            print(f"Agent {agent_id} registered successfully")
            return True
        else:
            print(f"Failed to register agent: {response.text}")
            return False
    except Exception as e:
        print(f"Error registering agent: {e}")
        return False

def lookup_agent(agent_id):
    """Look up an agent's URL in the registry"""
    reg_url = get_registry_url()
    try:
        print(f"Looking up agent {agent_id} in registry...")
        response = requests.get(
            f"{reg_url}/lookup/{agent_id}",
            verify=False  # For development with self-signed certs
        )
        if response.status_code == 200:
            agent_url = response.json().get("agent_url")
            print(f"Found agent {agent_id} at URL: {agent_url}")
            return agent_url
        print(f"Agent {agent_id} not found in registry")
        return None
    except Exception as e:
        print(f"Error looking up agent {agent_id}: {e}")
        return None

def add_message_to_queue(client_id, message):
    """Add a message to a client's queue for SSE streaming"""
    if client_id in client_queues:
        client_queues[client_id]['queue'].put(message)
        client_queues[client_id]['event'].set()

# ★新增：Open-Meteo 工具函数（免 Key）
def _geocode_city(city: str):
    """Use Open-Meteo Geocoding to resolve city to (lat, lon, name, country)."""
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={urlquote(city)}&count=1&language=en&format=json"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        item = results[0]
        return {
            "lat": item.get("latitude"),
            "lon": item.get("longitude"),
            "name": item.get("name"),
            "country": item.get("country"),
            "admin1": item.get("admin1"),
        }
    except Exception as e:
        print(f"Geocoding error: {e}")
        return None

def _fetch_weather(lat: float, lon: float):
    """Fetch current weather from Open-Meteo."""
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "auto"
        }
        r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Weather fetch error: {e}")
        return None

def _format_weather(city_label: str, wx: dict):
    c = wx.get("current") or {}
    ts = c.get("time")
    t = c.get("temperature_2m")
    rh = c.get("relative_humidity_2m")
    at = c.get("apparent_temperature")
    pr = c.get("precipitation")
    ws = c.get("wind_speed_10m")
    wd = c.get("wind_direction_10m")
    wc = c.get("weather_code")
    return (
        f"🌤️ Weather for {city_label}\n"
        f"Time: {ts}\n"
        f"Temp: {t} °C (feels like {at} °C)\n"
        f"Humidity: {rh}%\n"
        f"Precipitation: {pr} mm\n"
        f"Wind: {ws} km/h, dir {wd}°\n"
        f"Weather code: {wc}"
    )

# Message handling endpoints
@app.route('/api/health', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    return jsonify({"status": "ok", "agent_id": agent_id})

@app.route('/api/send', methods=['POST', 'OPTIONS'])
def send_message():
    """Send a message to the agent bridge and return the response"""

    if request.method == 'OPTIONS':
        response = app.make_default_options_response()
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Max-Age': '3600'
        }
        for key, value in headers.items():
            response.headers[key] = value
        return response

    try:
        data = request.json
        if not data or 'message' not in data:
            return jsonify({"error": "Missing message in request"}), 400
        
        message_text = data['message']
        conversation_id = data.get('conversation_id')
        client_id = data.get('client_id', 'ui_client')

        # ============= 指令分流：放在 A2AClient 之前 =============
        text = message_text.strip()
        low  = text.lower()

        # /help 或 @stock help
        if low == "/help" or low == "@stock help":
            return jsonify({
                "response": stock_help(),
                "conversation_id": conversation_id,
                "agent_id": agent_id
            })

        # /quote <TICKER>  或   @stock price <TICKER>
        m1 = re.match(r"^/quote\s+([A-Za-z\.\-=:]+)$", text)
        m2 = re.match(r"^@stock\s+price\s+([A-Za-z\.\-=:]+)$", low)
        if m1 or m2:
            ticker = (m1.group(1) if m1 else text.split()[-1]).upper()
            return jsonify({
                "response": stock_quote(ticker),
                "conversation_id": conversation_id,
                "agent_id": agent_id
            })

        # /compare <T1> <T2>
        m3 = re.match(r"^/compare\s+([A-Za-z\.\-=:]+)\s+([A-Za-z\.\-=:]+)$", text)
        if m3:
            t1, t2 = m3.group(1).upper(), m3.group(2).upper()
            return jsonify({
                "response": stock_compare(t1, t2),
                "conversation_id": conversation_id,
                "agent_id": agent_id
            })

        # ★新增：/weather <City>
        m4 = re.match(r"^/weather\s+(.+)$", text, re.IGNORECASE)
        if m4:
            city = m4.group(1).strip()
            geo = _geocode_city(city)
            if not geo:
                return jsonify({
                    "response": f"⚠️ Could not find city: {city}",
                    "conversation_id": conversation_id,
                    "agent_id": agent_id
                })
            wx = _fetch_weather(geo["lat"], geo["lon"])
            if not wx:
                return jsonify({
                    "response": f"⚠️ Weather service error for: {geo['name']}",
                    "conversation_id": conversation_id,
                    "agent_id": agent_id
                })
            label = f"{geo['name']}, {geo.get('admin1') or ''} {geo.get('country') or ''}".strip()
            return jsonify({
                "response": _format_weather(label, wx),
                "conversation_id": conversation_id,
                "agent_id": agent_id
            })

        # ★新增：/ask <question> 直连 Claude（Anthropic）
        m5 = re.match(r"^/ask\s+(.+)$", text, re.IGNORECASE)
        if m5:
            question = m5.group(1).strip()
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return jsonify({
                    "response": "⚠️ ANTHROPIC_API_KEY is not set in environment.",
                    "conversation_id": conversation_id,
                    "agent_id": agent_id
                })
            try:
                client = Anthropic(api_key=api_key)
                # 使用通用模型名；如需切换只改此处
                model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
                msg = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    temperature=0.7,
                    messages=[{"role": "user", "content": question}]
                )
                # 取出文本
                parts = []
                for c in msg.content:
                    if c.type == "text":
                        parts.append(c.text)
                claude_text = "\n".join(parts).strip() or "(empty response)"
                return jsonify({
                    "response": claude_text,
                    "conversation_id": conversation_id,
                    "agent_id": agent_id
                })
            except Exception as e:
                return jsonify({
                    "response": f"⚠️ Claude request failed: {e}",
                    "conversation_id": conversation_id,
                    "agent_id": agent_id
                })

        # ================== 分流结束，未命中则继续 =====================
        metadata = {'source': 'ui_client','client_id': client_id}

        # Create an A2A client to talk to the agent bridge
        # Use HTTP for local communication
        bridge_url = f"http://localhost:{agent_port}"  # Remove /a2a since A2AClient adds it
        client = A2AClient(bridge_url, timeout=60)

        # Send the message to the bridge WITHOUT preprocessing
        response = client.send_message(
            Message(
                role=MessageRole.USER,
                content=TextContent(text=message_text),
                conversation_id=conversation_id,
                metadata=metadata
            )
        )
        print(f"Response: {response}")
        # Extract the response from the agent
        if hasattr(response.content, 'text'):
            return jsonify({
                "response": response.content.text,
                "conversation_id": response.conversation_id,
                "agent_id": agent_id
            })
        else:
            return jsonify({"error": "Received non-text response"}), 500
            
    except Exception as e:
        print(f"Error in /api/send: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/agents/list', methods=['GET'])
def list_agents():
    """List all registered clients"""
    reg_url = get_registry_url()
    try:
        # Use clients endpoint if available
        try:
            response = requests.get(
                f"{reg_url}/clients",
                verify=False  # For development with self-signed certs
            )
        except:
            # Fall back to list endpoint
            response = requests.get(
                f"{reg_url}/list",
                verify=False  # For development with self-signed certs
            )
        if response.status_code == 200:
            return jsonify(response.json())
        return jsonify({"error": f"Failed to get agent list: {response.text}"}), response.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/receive_message', methods=['POST'])
def receive_message():
    """Receive a message from the agent bridge and display it"""
    try:
        data = request.json
        message = data.get('message', '')
        from_agent = data.get('from_agent', '')
        conversation_id = data.get('conversation_id', '')
        timestamp = data.get('timestamp', '')
       
        reg_url = get_registry_url()
        sender_name = requests.get(
                f"{reg_url}/sender/{from_agent}",
                verify=False  # For development with self-signed certs
            )
        sender_name = sender_name.json().get("sender_name")

        print("\n--- New message received ---")
        print(f"From: {from_agent}")
        print(f"Message: {message}")
        print(f"Conversation ID: {conversation_id}")
        print(f"Timestamp: {timestamp}")
        print(f"Sender Name: {sender_name}")
        print("----------------------------\n")
        
        message_file = f"latest_message.json"
        with open(message_file, "w") as f:
            json.dump({
                "message": message,
                "from_agent": from_agent,
                "sender_name": sender_name,
                "conversation_id": conversation_id,
                "timestamp": timestamp
            }, f)
        
        return jsonify({"status": "received"})
    except Exception as e:
        print(f"Error processing received message: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/render', methods=['GET'])
def render_on_ui():
    try:
        message_file = f"latest_message.json"
        if not os.path.exists(message_file):
            return jsonify({})
        else:
            latest_message = json.load(open(message_file))
            os.remove(message_file)
            return jsonify(latest_message)
    except Exception as e:
        print(f"No latest message found")
        return jsonify({"error": str(e)}), 500

# ============== 简易网页入口：根路径 ==============
@app.route("/", methods=["GET"])
def index_page():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>My Agent</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --ink: #1f2937;
      --muted: #6b7280;
      --brand: #4f46e5;
      --brand-weak: #eef2ff;
      --border: #e5e7eb;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
      --radius: 14px;
      --shadow: 0 6px 18px rgba(0,0,0,.08);
    }
    * { box-sizing: border-box; }
    body {
      font-family: var(--sans);
      background: var(--bg);
      margin: 0;
      color: var(--ink);
    }
    .container {
      max-width: 900px;
      margin: 56px auto;
      padding: 0 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 24px;
    }
    h1 {
      margin: 0 0 8px 0;
      font-size: 28px;
      text-align: center;
    }
    .desc {
      text-align: center;
      color: var(--muted);
      margin-bottom: 18px;
    }
    .hint {
      text-align: center;
      color: var(--muted);
      font-size: 14px;
      margin-bottom: 20px;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      margin-bottom: 16px;
    }
    .row input {
      padding: 12px 14px;
      font-size: 15px;
      border: 1px solid var(--border);
      border-radius: 10px;
      outline: none;
      background: #fff;
    }
    .row input:focus { border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-weak); }
    button {
      padding: 12px 16px;
      background: var(--brand);
      color: #fff;
      border: 0;
      border-radius: 10px;
      cursor: pointer;
      font-weight: 600;
    }
    button.secondary {
      background: #111827;
    }
    .out {
      margin-top: 14px;
    }
    .resp-header {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
      font-weight: 700;
    }
    .badge {
      font-size: 12px;
      background: var(--brand-weak);
      color: var(--brand);
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid #dbe6ff;
    }
    .pre {
      background: #0b1020;
      color: #d7e1ff;
      border-radius: 12px;
      padding: 14px;
      font-family: var(--mono);
      font-size: 14px;
      line-height: 1.45;
      white-space: pre-wrap;  /* keep wrapping, but preserve line breaks */
      overflow-x: auto;
    }
    details.raw {
      margin-top: 12px;
      font-size: 14px;
    }
    details.raw summary {
      cursor: pointer;
      color: var(--muted);
    }
    .cmds {
      text-align: center;
      color: var(--muted);
      font-size: 13px;
      margin-top: 6px;
    }
    @media (max-width: 640px) {
      .row { grid-template-columns: 1fr; }
      button, button.secondary { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>🤖 My Agent</h1>
    <div class="desc">This page calls the backend API <code>/api/send</code> directly.</div>
    <div class="hint">Commands: <code>/quote AAPL</code>, <code>/compare NVDA AMD</code>, <code>/weather Boston</code>, <code>/ask what is RL?</code></div>

    <div class="row">
      <input id="msg" placeholder="Type a command… e.g. /quote TSLA" />
      <button onclick="send()">Send</button>
      <button class="secondary" onclick="demo()">Demo</button>
    </div>

    <div class="card out">
      <div class="resp-header">
        <span>Response</span>
        <span id="respBadge" class="badge" style="display:none;"></span>
      </div>
      <div id="pretty" class="pre">Waiting for your input…</div>
      <details class="raw">
        <summary>Raw JSON</summary>
        <pre id="raw" class="pre" style="margin-top:8px;"></pre>
      </details>
      <div class="cmds">Tip: Pretty view shows <em>response</em> as plain text; open “Raw JSON” for full payload.</div>
    </div>
  </div>

<script>
function escapeHtml(s){
  return s.replace(/[&<>]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
}

function looksLikeBoxTable(text){
  // crude check for box-drawing layout
  return /[┌┬┐└┴┘├┼┤│─]/.test(text) || /^\s*\|.+\|\s*$/m.test(text);
}

function renderPretty(payload){
  const pretty = document.getElementById('pretty');
  const raw = document.getElementById('raw');
  const badge = document.getElementById('respBadge');

  // show raw json (debug)
  raw.textContent = JSON.stringify(payload, null, 2);

  // choose main text
  const text = (payload && typeof payload.response === 'string')
    ? payload.response
    : (payload ? JSON.stringify(payload, null, 2) : '—');

  // optional label
  badge.style.display = 'inline-block';
  badge.textContent = payload?.agent_id ? `agent: ${payload.agent_id}` : 'response';

  // IMPORTANT: do NOT stringify/escape newlines here; just set textContent
  // to preserve alignment of box tables and line breaks.
  pretty.textContent = text;

  // If you ever return HTML snippets, you could switch to innerHTML safely:
  // pretty.innerHTML = escapeHtml(text).replaceAll('\\n', '<br>');
}

async function callApi(message){
  try {
    const r = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message})
    });
    const j = await r.json();
    renderPretty(j);
  } catch (e) {
    renderPretty({ error: String(e) });
  }
}

function send(){
  const m = document.getElementById('msg').value.trim();
  if(!m){ alert('Please enter a message.'); return; }
  callApi(m);
}

function demo(){
  document.getElementById('msg').value = '/quote TSLA';
  send();
}
</script>
</body>
</html>
    """

# =================================================

@app.route('/api/messages/stream', methods=['GET'])
def stream_messages():
    """SSE endpoint for streaming messages to UI clients"""
    client_id = request.args.get('client_id')
    if not client_id or client_id not in client_queues:
        return jsonify({"error": "Client not registered"}), 400
    
    def generate():
        client_data = client_queues[client_id]
        queue = client_data['queue']
        event = client_data['event']
        while True:
            event.wait()
            while not queue.empty():
                message = queue.get()
                yield f"data: {json.dumps(message)}\n\n"
            event.clear()
    
    response = Response(
        stream_with_context(generate()), 
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache', 
            'X-Accel-Buffering': 'no',
            'Content-Type': 'text/event-stream',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        }
    )
    return response

def main():
    global bridge_process, registry_url, agent_id, agent_port
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    
    parser = argparse.ArgumentParser(description="Run an agent with Flask API wrapper")
    parser.add_argument("--id", required=True, help="Agent ID")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 6000)), help="Agent bridge port (default: 6000)")  # ★确保跟 .env 的 PORT 一致
    parser.add_argument("--api-port", type=int, default=int(os.environ.get("UI_PORT", 5000)), help="Flask API (UI) port (default: 5000)")  # ★新增 UI_PORT
    parser.add_argument("--registry", help="Registry URL")
    parser.add_argument("--public-url", help="Public URL for the Agent Bridge")
    parser.add_argument("--api-url", help="Api URL for the User Client")
    parser.add_argument("--cert", help="Path to SSL certificate file")
    parser.add_argument("--key", help="Path to SSL key file")
    parser.add_argument("--ssl", action="store_true", help="Enable SSL with default certificates")
    
    args = parser.parse_args()
    
    # Set global variables
    agent_id = args.id
    agent_port = args.port
    api_port = args.api_port
    registry_url = args.registry
    
    # Determine public URL for registration
    # --- 修改：优先命令行，其次环境变量 ---
    public_url = (args.public_url or os.environ.get("PUBLIC_URL") or "").strip()
    api_url = (args.api_url or os.environ.get("API_URL") or f"http://localhost:{api_port}").strip()

    # Set environment variables for the agent bridge
    os.environ["AGENT_ID"] = agent_id
    os.environ["PORT"] = str(agent_port)  # Bridge 端口（.env 默认 6000）
    os.environ["PUBLIC_URL"] = public_url
    os.environ['API_URL'] = api_url
    os.environ["REGISTRY_URL"] = get_registry_url()
    os.environ["UI_MODE"] = "true"
    os.environ["UI_CLIENT_URL"] = f"{os.environ['API_URL']}/api/receive_message"

    # Create unique log directories for each agent
    log_dir = f"logs_{agent_id}"
    os.makedirs(log_dir, exist_ok=True)
    os.environ["LOG_DIR"] = log_dir
    log_file = open(f"{log_dir}/bridge_run.txt","a")

    # . the agent bridge
    print(f"Starting agent bridge for {agent_id} on port {agent_port}...")

    # ★修改：Windows/跨平台统一用 sys.executable 启动，不再写死 python3
    bridge_process = subprocess.Popen([sys.executable, "agent_bridge.py"], stdout=log_file, stderr=log_file)

    # Give the bridge a moment to start
    time.sleep(2)

    print("\n" + "="*50)
    print(f"Agent {agent_id} is running")
    print(f"Agent Bridge URL: http://localhost:{agent_port}/a2a")
    print(f"Public Client API URL: {public_url or '(unset)'}")
    print("="*50)
    print("\nAPI Endpoints:")
    print(f"  GET  {os.environ['API_URL']}/api/health - Health check")
    print(f"  POST {os.environ['API_URL']}/api/send - Send a message to the client")
    print(f"  GET  {os.environ['API_URL']}/api/agents/list - List all registered agents")
    print(f"  POST {os.environ['API_URL']}/api/receive_message - Receive a message from agent")
    print(f"  GET  {os.environ['API_URL']}/api/render - Get the latest message")
    print("\nPress Ctrl+C to stop all processes.")
    
    # Configure SSL context if needed
    ssl_context = None
    if args.ssl:
        if args.cert and args.key:
            if os.path.exists(args.cert) and os.path.exists(args.key):
                ssl_context = (args.cert, args.key)
                print(f"Using SSL certificates from: {args.cert}, {args.key}")
            else:
                print("ERROR: Certificate files not found at specified paths")
                print(f"Certificate path: {args.cert}")
                print(f"Key path: {args.key}")
                sys.exit(1)
        else:
            print("ERROR: SSL enabled but certificate paths not provided")
            print("Please provide --cert and --key arguments")
            sys.exit(1)

    # Start the Flask API server (UI)
    try:
        app.run(host='0.0.0.0', port=api_port, threaded=True, ssl_context=ssl_context)
    except OSError as e:
        # --- 修改：若 5000 被占用，自动切到 5100 ---
        if "Address already in use" in str(e):
            fallback = 5100 if api_port == 5000 else (api_port + 1)
            print(f"[ui] Port {api_port} in use. Switching to {fallback} ...")
            os.environ['API_URL'] = f"http://localhost:{fallback}"
            os.environ["UI_CLIENT_URL"] = f"{os.environ['API_URL']}/api/receive_message"
            app.run(host='0.0.0.0', port=fallback, threaded=True, ssl_context=ssl_context)
        else:
            raise

if __name__ == "__main__":
    main()

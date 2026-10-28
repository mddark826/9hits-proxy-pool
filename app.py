"""
9Hits ExProxy auto-refresh pool — Render-ready.

What it does (official 9Hits ExProxy protocol, docs.9hits.com/traffic-exchange/viewer/proxy-pool/):
  * Every REFRESH_MINUTES it fetches public proxy lists, tests them with curl
    against https://api.ipify.org, and keeps only the working ones.
  * It serves those proxies to 9Hits Viewer sessions one-by-one:
      GET /pool?token=<viewer-token>&user=<name>&ver=..&platform=..&rand=..
        -> {"type":"socks5","server":"1.2.3.4:1080","id":"1.2.3.4:1080"}
        or {"error":"No proxy available"}  (Viewer waits 30s and asks again)
  * Viewer status reports come back to the SAME url:
      GET /pool?...&status=working&proxy=1.2.3.4:1080&session=..&camp=..&duration=..
      GET /pool?...&status=released&proxy=1.2.3.4:1080&session=..
    A proxy with no report for 15 minutes is treated as free again.

How to use it in 9Hits Viewer:
  * Set a session's connection type to ExProxy and paste this URL:
        https://<your-service>.onrender.com/pool
  * Create 20-30 sessions ALL pointing at the same URL. The pool hands a
    different proxy to each session. Keep live proxies > sessions, otherwise
    many sessions will just wait (TARGET_LIVE default 50 for 20-30 sessions).

Render notes (free tier):
  * This service is LIGHT (no Chromium, ~40-60 MB RAM) — it fits Render Free
    512 MB. It is NOT the Viewer itself. Do NOT try to run 20 Chromium
    viewers on this same free service; that OOMs (proven before).
  * Free services sleep after 15 min with no inbound traffic and wake in
    ~35-55s on the next request. While Viewer sessions are running they
    report every ~5 min, so the service stays awake. When nothing runs,
    sleep is fine. An UptimeRobot ping to /health every 10-14 min keeps it
    warm if you want instant wake-ups.
  * Filesystem is lost on spin-down/redeploy, so the pool is rebuilt by a
    refresh on startup. That is by design — never treat pool.json as durable.

Security:
  * NINEHITS_TOKEN (optional): if set, /pool only answers when ?token=
    equals it. Set it to your Viewer access key so strangers cannot drain
    your pool. The token is NEVER logged.
  * ADMIN_KEY (optional): if set, /refresh and /list require ?key=ADMIN_KEY.
  * /stats shows counts only, never proxy IPs. /list is admin-only.
"""

import ipaddress
import json
import os
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------- config
PORT = int(os.environ.get("PORT", "5000"))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "120"))
TARGET_LIVE = int(os.environ.get("TARGET_LIVE", "50"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "800"))
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "7"))
TEST_WORKERS = int(os.environ.get("TEST_WORKERS", "20"))
NINEHITS_TOKEN = os.environ.get("NINEHITS_TOKEN", "").strip()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
POOL_FILE = os.environ.get("POOL_FILE", "pool.json")

# Public lists only. TheSpeedX URLs are the canonical raw files; proxyscrape
# is its public getproxies API; proxifly is a 5-min-updated public list.
# If a source dies, the script just skips it — it never crashes the pool.
SOURCES = [
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("socks5", "https://api.proxyscrape.com/?request=getproxies&proxytype=socks5"),
    ("socks4", "https://api.proxyscrape.com/?request=getproxies&proxytype=socks4"),
    ("http", "https://api.proxyscrape.com/?request=getproxies&proxytype=http"),
    ("auto", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
]

IP_PORT_RE = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$")
IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

# ---------------------------------------------------------------- state
lock = threading.Lock()
# server -> {type, server, user, password, latency_ms, last_checked,
#            last_assigned, last_report_at, report_status, session, camp}
pool = {}
state = {
    "last_refresh": None,
    "last_refresh_ok": 0,
    "last_refresh_tried": 0,
    "last_error": "",
    "refreshing": False,
}


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def is_public_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
        return not (addr.is_private or addr.is_loopback or addr.is_reserved
                    or addr.is_link_local or addr.is_multicast)
    except ValueError:
        return False


def parse_line(line, default_type):
    """Accept: 1.2.3.4:8080 | socks5://1.2.3.4:1080 | socks5;1.2.3.4:1080;u;p
    Returns (type, server, user, password) or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    ptype, server, user, password = default_type, "", "", ""
    # scheme://server form
    if "://" in line:
        scheme, rest = line.split("://", 1)
        if scheme in ("http", "https", "socks4", "socks5"):
            ptype = "http" if scheme == "https" else scheme
        server = rest.split("/")[0].split()[0]
    else:
        parts = re.split(r"[;|\s]+", line)
        parts = [p for p in parts if p]
        if not parts:
            return None
        if parts[0] in ("http", "https", "socks4", "socks5") and len(parts) >= 2:
            ptype = "http" if parts[0] == "https" else parts[0]
            server = parts[1]
            user = parts[2] if len(parts) > 2 else ""
            password = parts[3] if len(parts) > 3 else ""
        else:
            server = parts[0]
    if ptype == "auto":
        ptype = "socks5" if server.endswith(":1080") else "http"
    m = IP_PORT_RE.match(server)
    if not m:
        return None
    ip, port = m.group(1), int(m.group(2))
    if not (1 <= port <= 65535) or not is_public_ip(ip):
        return None
    # quick octet sanity (ipaddress already filtered, this is just cheap)
    if any(int(o) > 255 for o in ip.split(".")):
        return None
    return ptype, server, user, password


def fetch_candidates():
    """Fetch all sources, dedupe, shuffle-ish (keep order per source)."""
    seen = {}
    headers = {"User-Agent": "9hits-proxy-pool/1.0"}
    for default_type, url in SOURCES:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                log(f"fetch skip {url}: HTTP {r.status_code}")
                continue
            n = 0
            for line in r.text.splitlines():
                parsed = parse_line(line, default_type)
                if parsed and parsed[1] not in seen:
                    seen[parsed[1]] = parsed
                    n += 1
            log(f"fetch ok {url}: {n} new candidates")
        except Exception as e:  # noqa: BLE001 - one dead source must not kill refresh
            log(f"fetch fail {url}: {e}")
    # Shuffle across ALL sources before capping. Sorting socks5-first was a
    # bug in testing: it meant only the oldest (stale, 14-day-old) TheSpeedX
    # socks5 entries were ever tested, and fresher proxyscrape/proxifly
    # entries never got tried. A live http proxy beats a dead socks5 one.
    ordered = list(seen.values())
    random.shuffle(ordered)
    return ordered[:MAX_CANDIDATES]


def _curl_via_proxy(scheme, server, user, password):
    proxy_url = f"{scheme}://{server}"
    if user:
        proxy_url = (f"{scheme}://{user}:{password}@{server}" if password
                     else f"{scheme}://{user}@{server}")
    out = subprocess.run(
        ["curl", "-s", "-m", str(TEST_TIMEOUT),
         "-w", "\n%{http_code} %{time_total}",
         "-x", proxy_url, "https://api.ipify.org"],
        capture_output=True, text=True, timeout=TEST_TIMEOUT + 3,
    )
    lines = out.stdout.strip().splitlines()
    if len(lines) < 2:
        return None
    body = lines[0].strip()
    code_time = lines[-1].split()
    if len(code_time) < 2 or code_time[0] != "200":
        return None
    if not IPV4_RE.match(body) or not is_public_ip(body):
        return None
    return int(float(code_time[1]) * 1000)


def test_proxy(candidate):
    """curl through the proxy to ipify. Returns entry dict or None.

    Free lists mislabel protocols constantly (a TCP-open port listed as
    socks5 is often really http, or vice versa), so if the listed protocol
    fails, try the others for the same server before giving up. The entry
    records the protocol that actually worked, not the listed one."""
    ptype, server, user, password = candidate
    order = [ptype] + [p for p in ("http", "socks5", "socks4") if p != ptype]
    for scheme in order:
        try:
            latency = _curl_via_proxy(scheme, server, user, password)
        except Exception:  # noqa: BLE001
            continue
        if latency is not None:
            return {
                "type": scheme, "server": server, "user": user, "password": password,
                "latency_ms": latency, "last_checked": time.time(),
                "last_assigned": 0, "last_report_at": 0,
                "report_status": "", "session": "", "camp": "",
            }
    return None


def refresh_pool():
    """One refresh cycle: fetch -> test in batches -> keep live ones."""
    with lock:
        if state["refreshing"]:
            return {"skipped": "already refreshing"}
        state["refreshing"] = True
    try:
        candidates = fetch_candidates()
        tried = len(candidates)
        live = {}
        # Keep previously-good proxies that are still free — they are not
        # re-tested every cycle to save Render CPU; dead ones fall out when
        # Viewer reports errors or after 15 min without a good report.
        # New candidates are tested in batches until TARGET_LIVE is reached.
        with ThreadPoolExecutor(max_workers=TEST_WORKERS) as ex:
            for i in range(0, len(candidates), TEST_WORKERS):
                if len(live) >= TARGET_LIVE:
                    break
                batch = candidates[i:i + TEST_WORKERS]
                for entry in ex.map(test_proxy, batch):
                    if entry and entry["server"] not in live:
                        live[entry["server"]] = entry
                        if len(live) >= TARGET_LIVE:
                            break
        with lock:
            # Preserve busy sessions from the old pool (do not yank a proxy
            # a Viewer is actively using just because refresh ran).
            for server, old in pool.items():
                if server in live:
                    live[server]["last_assigned"] = old.get("last_assigned", 0)
                    live[server]["last_report_at"] = old.get("last_report_at", 0)
                    live[server]["report_status"] = old.get("report_status", "")
                    live[server]["session"] = old.get("session", "")
                    live[server]["camp"] = old.get("camp", "")
            pool.clear()
            pool.update(live)
            state["last_refresh"] = datetime.now(timezone.utc).isoformat()
            state["last_refresh_ok"] = len(live)
            state["last_refresh_tried"] = tried
            state["last_error"] = ""
            try:
                with open(POOL_FILE, "w") as f:
                    json.dump(live, f)
            except Exception as e:  # noqa: BLE001 - disk loss is non-fatal
                log(f"pool save fail: {e}")
        log(f"refresh done: {len(live)} live / {tried} tried (target {TARGET_LIVE})")
        return {"live": len(live), "tried": tried}
    except Exception as e:  # noqa: BLE001
        with lock:
            state["last_error"] = str(e)
        log(f"refresh error: {e}")
        return {"error": str(e)}
    finally:
        with lock:
            state["refreshing"] = False


def refresher_loop():
    time.sleep(10)  # let the server answer /health first
    while True:
        refresh_pool()
        time.sleep(REFRESH_MINUTES * 60)


def pool_counts():
    now = time.time()
    busy = free = 0
    with lock:
        for e in pool.values():
            # "assigned" = handed out but Viewer has not reported yet. It
            # counts as busy, otherwise two sessions asking within seconds
            # would get the same proxy. It frees after 15 min without a report.
            is_busy = (e.get("report_status") in ("working", "idle", "error", "assigned")
                       and e.get("session")
                       and (now - e.get("last_report_at", 0)) < 900)
            if is_busy:
                busy += 1
            else:
                free += 1
    return len(pool), busy, free


def pick_free_proxy(session_id):
    """Oldest-assigned free proxy, per docs: free = released / no report /
    no report for 15 min."""
    now = time.time()
    best = None
    with lock:
        for e in pool.values():
            is_busy = (e.get("report_status") in ("working", "idle", "error", "assigned")
                       and e.get("session")
                       and (now - e.get("last_report_at", 0)) < 900)
            if is_busy:
                continue
            if best is None or e.get("last_assigned", 0) < best.get("last_assigned", 0):
                best = e
        if best is not None:
            best["last_assigned"] = now
            best["last_report_at"] = now
            best["report_status"] = "assigned"
            best["session"] = session_id or ""
    return best


# ---------------------------------------------------------------- routes
def exproxy_response():
    """Shared logic for / and /pool when Viewer params are present."""
    # Token check — never log the token value.
    if NINEHITS_TOKEN:
        if request.args.get("token", "") != NINEHITS_TOKEN:
            return jsonify({"error": "bad token"}), 403
    status = request.args.get("status")
    proxy_server = request.args.get("proxy", "")
    if status:
        # Status report: reply ignored by Viewer, any 2xx is fine.
        with lock:
            e = pool.get(proxy_server)
            if e:
                e["last_report_at"] = time.time()
                e["report_status"] = status
                e["session"] = request.args.get("session", e.get("session", ""))
                e["camp"] = request.args.get("camp", "")
                if status == "released":
                    e["session"] = ""
        return jsonify({"ok": True})
    # New proxy request
    entry = pick_free_proxy(request.args.get("session", ""))
    if not entry:
        return jsonify({"error": "No proxy available"})
    out = {"type": entry["type"], "server": entry["server"], "id": entry["server"]}
    if entry.get("user"):
        out["user"] = entry["user"]
        out["password"] = entry.get("password", "")
    return jsonify(out)


@app.route("/pool")
def pool_endpoint():
    return exproxy_response()


@app.route("/health")
def health():
    total, busy, free = pool_counts()
    return jsonify({"status": "ok", "live": total, "busy": busy, "free": free,
                    "last_refresh": state["last_refresh"]})


@app.route("/stats")
def stats():
    total, busy, free = pool_counts()
    return jsonify({"live": total, "busy": busy, "free": free,
                    "last_refresh": state["last_refresh"],
                    "last_refresh_ok": state["last_refresh_ok"],
                    "last_refresh_tried": state["last_refresh_tried"],
                    "refresh_minutes": REFRESH_MINUTES,
                    "target_live": TARGET_LIVE,
                    "refreshing": state["refreshing"],
                    "last_error": state["last_error"]})


@app.route("/list")
def list_proxies():
    if ADMIN_KEY and request.args.get("key", "") != ADMIN_KEY:
        return jsonify({"error": "admin key required"}), 403
    if ADMIN_KEY == "" and request.args.get("key", "") != "":
        return jsonify({"error": "admin key required"}), 403
    with lock:
        data = [{k: v for k, v in e.items() if k not in ("user", "password")}
                for e in pool.values()]
    return jsonify({"count": len(data), "proxies": data})


@app.route("/refresh", methods=["POST", "GET"])
def manual_refresh():
    if ADMIN_KEY and request.args.get("key", "") != ADMIN_KEY:
        return jsonify({"error": "admin key required"}), 403
    threading.Thread(target=refresh_pool, daemon=True).start()
    return jsonify({"refresh": "started"})


@app.route("/")
def index():
    # Viewer always sends ?token=..., so a request WITH token/status is a
    # pool request, not a human visit.
    if request.args.get("token") is not None or request.args.get("status"):
        return exproxy_response()
    total, busy, free = pool_counts()
    base = request.host_url.rstrip("/")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>9Hits Proxy Pool</title></head><body style="font-family:sans-serif;max-width:640px;margin:40px auto">
<h2>9Hits Proxy Pool — auto-refresh</h2>
<p>Live: <b>{total}</b> &nbsp; Busy: {busy} &nbsp; Free: {free}<br>
Last refresh: {state["last_refresh"]} &nbsp; Every {REFRESH_MINUTES} min &nbsp; Target: {TARGET_LIVE}</p>
<p><b>9Hits Viewer-এ ExProxy URL হিসেবে এটা বসাও:</b><br>
<code>{base}/pool</code></p>
<p>২০-৩০টা সেশন সবাই এই একই URL-এ পয়েন্ট করবে — পুল প্রতিটা সেশনকে
আলাদা proxy দেবে। Live সংখ্যা সেশন সংখ্যার চেয়ে বেশি থাকতে হবে, নইলে
অনেক সেশন Waiting-এ থাকবে।</p>
<p><a href="/health">/health</a> · <a href="/stats">/stats</a> (counts only, no IPs)</p>
<p style="color:#555">Free public proxies only. Quality is low, many campaigns
exclude proxies, and a proxy dies in ~hours — that is why this refreshes
automatically. This is not a paid/residential proxy service.</p>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    threading.Thread(target=refresher_loop, daemon=True).start()
    # gunicorn is used on Render; direct run is for local testing only.
    app.run(host="0.0.0.0", port=PORT)
else:
    # Under gunicorn, start the refresher once.
    if not getattr(app, "_refresher_started", False):
        app._refresher_started = True
        threading.Thread(target=refresher_loop, daemon=True).start()

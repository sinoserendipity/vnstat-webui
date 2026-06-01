#!/usr/bin/env python3
"""vnstat Web Dashboard - OpenWrt Network Traffic Analyzer"""

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------
SETTINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
TEMP_DIR = "/tmp/vnstat"
CACHE_TTL = 60

os.makedirs(SETTINGS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "source_mode": False,
    "protocol": "samba",
    "host": "192.168.1.1",
    "port": 445,
    "username": "vnstat",
    "password": "123456",
    "share": "vnstat",
    "file": "vnstat.db",
    "webdav_url": "http://192.168.1.1/vnstat/vnstat.db",
    "webdav_port": 80,
    "local_db_path": "/app/data/vnstat.db",
}

ENV_MAP = {
    "VNSTAT_SOURCE_MODE": "source_mode",
    "VNSTAT_LOCAL_DB":    "local_db_path",
    "VNSTAT_PROTOCOL":    "protocol",
    "VNSTAT_HOST":        "host",
    "VNSTAT_PORT":        "port",
    "VNSTAT_USER":        "username",
    "VNSTAT_PASS":        "password",
    "VNSTAT_SHARE":       "share",
    "VNSTAT_FILE":        "file",
    "VNSTAT_WEBDAV_URL":  "webdav_url",
    "VNSTAT_WEBDAV_PORT": "webdav_port",
}

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
# Use a stable key from env; fall back to random (sessions won't persist across restarts)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(16).hex()

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def load_settings():
    """Load settings from file (preferred) or environment variables."""
    merged = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                merged.update(json.load(fh))
            return merged  # success — skip env vars
        except Exception:  # noqa: BLE001  (file corrupt, fall through)
            pass
    # settings.json absent or unreadable → read environment variables
    for env_key, cfg_key in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if cfg_key in ("port", "webdav_port"):
            merged[cfg_key] = int(val)
        elif cfg_key == "source_mode":
            merged[cfg_key] = val.lower() in ("true", "1", "yes")
        else:
            merged[cfg_key] = val
    return merged


def save_settings(data):
    """Merge *data* into current settings, persist to disk, and return merged dict."""
    current = load_settings()
    current.update(data)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2)
    return current

# ---------------------------------------------------------------------------
# DB fetch helpers
# ---------------------------------------------------------------------------

# Module-level cache state
_last_fetch: float = 0.0
_db_path: str | None = None
_previous_hash: str | None = None


def _settings_hash(cfg: dict) -> str:
    """Return a stable hash of the connection-relevant settings keys."""
    keys = [
        "source_mode", "protocol", "host", "port", "username", "password",
        "share", "file", "webdav_url", "webdav_port", "local_db_path",
    ]
    return str(hash(frozenset({k: cfg.get(k) for k in keys}.items())))


def _fetch_samba(cfg: dict, dest: str) -> None:
    """Download the vnstat DB from a Samba/CIFS share into *dest*."""
    from smb.SMBConnection import SMBConnection  # noqa: PLC0415 (optional dep)
    host = cfg["host"]
    user, pw = cfg["username"], cfg["password"]
    share, filename = cfg["share"], cfg["file"]
    last_err = None
    for port, direct_tcp in [(int(cfg.get("port", 445)), True), (139, False)]:
        try:
            conn = SMBConnection(
                user, pw, "vnstat-dashboard", host,
                use_ntlm_v2=True, is_direct_tcp=direct_tcp,
            )
            if conn.connect(host, port, timeout=10):
                with open(dest, "wb") as fh:
                    conn.retrieveFile(share, filename, fh)
                conn.close()
                print(f"[OK] Samba port {port}")
                return
            conn.close()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise ConnectionError(f"Samba connection failed: {last_err}")


def _fetch_webdav(cfg: dict, dest: str) -> None:
    """Download the vnstat DB over HTTP/WebDAV into *dest*."""
    url = cfg.get("webdav_url", "")
    if not url:
        raise ValueError("No WebDAV URL configured")
    user, pw = cfg["username"], cfg["password"]
    auth = requests.auth.HTTPBasicAuth(user, pw) if user else None
    resp = requests.get(url, auth=auth, timeout=30)
    if resp.status_code == 401:
        raise ConnectionError("WebDAV 401 Unauthorized")
    if resp.status_code == 404:
        raise ConnectionError(f"WebDAV 404 Not Found: {url}")
    if resp.status_code != 200:
        raise ConnectionError(f"WebDAV returned HTTP {resp.status_code}")
    with open(dest, "wb") as fh:
        fh.write(resp.content)
    print("[OK] WebDAV")


def fetch_database() -> str:
    """Return a local path to the vnstat SQLite database, fetching if needed."""
    global _last_fetch, _db_path, _previous_hash  # noqa: PLW0603

    now = time.time()
    cfg = load_settings()
    current_hash = _settings_hash(cfg)

    # Invalidate cache when settings change
    if _previous_hash is None:
        _previous_hash = current_hash
    elif _previous_hash != current_hash:
        print("[INFO] Settings changed — cache invalidated")
        _last_fetch = 0.0
        _db_path = None
        _previous_hash = current_hash

    # Local-file mode: just validate and return the configured path
    if cfg.get("source_mode") in (True, "true"):
        local_path = cfg.get("local_db_path", "/app/data/vnstat.db")
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local DB not found: {local_path}")
        return local_path

    # Return cached copy if still fresh
    if _db_path and (now - _last_fetch) < CACHE_TTL:
        return _db_path

    # Download to a new temporary file
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=TEMP_DIR).name
    try:
        if cfg.get("protocol") == "webdav":
            _fetch_webdav(cfg, tmp)
        else:
            _fetch_samba(cfg, tmp)
    except Exception as exc:
        # Clean up the incomplete temp file without hiding the original error
        try:
            os.unlink(tmp)
        except OSError:
            pass
        # Fall back to the previous cached file if it still exists
        if _db_path and os.path.exists(_db_path):
            print(f"[WARN] Fetch failed ({exc}); serving stale cache")
            return _db_path
        raise

    # Remove the previous temp file now that we have a fresh one
    if _db_path and os.path.exists(_db_path) and _db_path != tmp:
        try:
            os.unlink(_db_path)
        except OSError:
            pass

    _db_path = tmp
    _last_fetch = now
    return _db_path

# ---------------------------------------------------------------------------
# DB query helpers
# ---------------------------------------------------------------------------

def get_db():
    """Open the vnstat SQLite database read-only and return the connection."""
    conn = sqlite3.connect(f"file:{fetch_database()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_schema(conn) -> dict:
    """Return {table_name: [column_names]} for every table in *conn*."""
    tables = {}
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        cols = conn.execute(f"PRAGMA table_info({row['name']})").fetchall()
        tables[row["name"]] = [c["name"] for c in cols]
    return tables


def get_interface(conn, iface_param: str):
    """Return the interface row matching *iface_param* (id or name), or the first one."""
    row = conn.execute(
        "SELECT * FROM interface WHERE id=? OR name=?",
        (iface_param, iface_param),
    ).fetchone()
    return row or conn.execute("SELECT * FROM interface LIMIT 1").fetchone()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def idx():
    """Serve the main dashboard page."""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Return database connection status, table list, and interface info."""
    try:
        conn = get_db()
        tables = get_schema(conn)
        interfaces = conn.execute("SELECT * FROM interface").fetchall()
        info_rows = conn.execute("SELECT * FROM info").fetchall()
        conn.close()
        cfg = load_settings()
        source_mode = cfg.get("source_mode", False)
        source_label = (
            f"local: {cfg.get('local_db_path')}"
            if source_mode
            else f"{cfg.get('protocol')}://{cfg.get('host')}"
        )
        return jsonify({
            "status": "ok",
            "source_mode": source_mode,
            "source": source_label,
            "tables": dict(tables),
            "interfaces": [dict(i) for i in interfaces],
            "info": {r["name"]: r["value"] for r in info_rows},
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/interfaces")
def api_interfaces():
    """Return all interface records."""
    try:
        conn = get_db()
        rows = [dict(i) for i in conn.execute("SELECT * FROM interface").fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    """Return current settings, omitting the password value."""
    cfg = load_settings()
    safe = {k: v for k, v in cfg.items() if k != "password"}
    safe["has_password"] = bool(cfg.get("password"))
    return jsonify(safe)


@app.route("/api/settings", methods=["PUT"])
def api_put_settings():
    """Persist new settings and invalidate the DB cache."""
    global _last_fetch, _previous_hash  # noqa: PLW0603
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data"}), 400
    # Don't overwrite the stored password with the placeholder sentinel
    if "password" in data and (not data["password"] or data["password"] == "********"):
        del data["password"]
    cfg = save_settings(data)
    _last_fetch = 0.0
    _previous_hash = None
    safe = {k: v for k, v in cfg.items() if k != "password"}
    safe["has_password"] = bool(cfg.get("password"))
    return jsonify({"status": "ok", "settings": safe})


@app.route("/api/settings/test", methods=["POST"])
def api_test_settings():
    """Test a connection without persisting settings."""
    data = request.get_json(force=True)
    if data.get("password") in (None, "", "********"):
        data.pop("password", None)
    cfg = load_settings()
    cfg.update(data)

    # Local-file mode
    if cfg.get("source_mode") in (True, "true"):
        path = cfg.get("local_db_path", "/app/data/vnstat.db")
        if not os.path.exists(path):
            return jsonify({"status": "error", "message": f"File not found: {path}"}), 400
        try:
            conn = sqlite3.connect(path)
            tables = [
                r[0] for r in
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            conn.close()
            size = os.path.getsize(path)
            return jsonify({
                "status": "ok",
                "file_size": size,
                "tables": tables,
                "message": f"OK! {len(tables)} tables",
            })
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "error", "message": str(exc)}), 400

    # Remote mode
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=TEMP_DIR).name
    try:
        t0 = time.time()
        if cfg.get("protocol") == "webdav":
            _fetch_webdav(cfg, tmp)
        else:
            _fetch_samba(cfg, tmp)
        elapsed = time.time() - t0
        size = os.path.getsize(tmp)
        conn = sqlite3.connect(tmp)
        tables = [
            r[0] for r in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        os.unlink(tmp)
        return jsonify({
            "status": "ok",
            "elapsed_ms": round(elapsed * 1000),
            "file_size": size,
            "tables": tables,
            "message": f"OK! {len(tables)} tables, {round(elapsed * 1000)}ms",
        })
    except Exception as exc:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/summary")
def api_summary():
    """Return today/yesterday/month/year traffic totals for one interface."""
    iface_param = request.args.get("interface", "1")
    try:
        conn = get_db()
        tables = get_schema(conn)
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404

        iid = iface_row["id"]
        iname = iface_row["name"]
        today_str = datetime.now().strftime("%Y-%m-%d")
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        month_str = datetime.now().strftime("%Y-%m")
        year_str = today_str[:4]

        today = yesterday = month = total = last_update = None

        if "day" in tables:
            row = conn.execute(
                "SELECT rx, tx FROM day WHERE interface=? AND date=?",
                (iid, today_str),
            ).fetchone()
            if row:
                today = {"rx": row["rx"], "tx": row["tx"]}

            row = conn.execute(
                "SELECT rx, tx FROM day WHERE interface=? AND date=?",
                (iid, yesterday_str),
            ).fetchone()
            if row:
                yesterday = {"rx": row["rx"], "tx": row["tx"]}

            row = conn.execute(
                "SELECT SUM(rx) rx, SUM(tx) tx FROM day "
                "WHERE interface=? AND SUBSTR(date,1,4)=?",
                (iid, year_str),
            ).fetchone()
            if row and row["rx"]:
                total = {"rx": row["rx"], "tx": row["tx"], "year": int(year_str)}

            row = conn.execute(
                "SELECT MAX(date) md FROM day WHERE interface=?", (iid,)
            ).fetchone()
            if row and row["md"]:
                last_update = row["md"]

        if "month" in tables:
            row = conn.execute(
                "SELECT rx, tx FROM month "
                "WHERE interface=? AND SUBSTR(date,1,7)=?",
                (iid, month_str),
            ).fetchone()
            if row:
                month = {"rx": row["rx"], "tx": row["tx"]}

        avg_daily = None
        if total and "day" in tables:
            cnt_row = conn.execute(
                "SELECT COUNT(*) c FROM day WHERE interface=?", (iid,)
            ).fetchone()
            if cnt_row and cnt_row["c"] > 0:
                count = cnt_row["c"]
                avg_daily = {
                    "rx": int(total["rx"]) // count,
                    "tx": int(total["tx"]) // count,
                    "days": count,
                }

        conn.close()
        return jsonify({
            "interface": iname,
            "interface_id": iid,
            "today": today,
            "yesterday": yesterday,
            "month": month,
            "total": total,
            "avg_daily": avg_daily,
            "last_update": last_update,
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/daily")
def api_daily():
    """Return per-day traffic for the last *days* days."""
    iface_param = request.args.get("interface", "1")
    days = int(request.args.get("days", 60))
    try:
        conn = get_db()
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404
        iid = iface_row["id"]
        result = []
        if "day" in get_schema(conn):
            rows = conn.execute(
                "SELECT date, rx, tx FROM day "
                "WHERE interface=? ORDER BY date DESC LIMIT ?",
                (iid, days),
            ).fetchall()
            result = [
                {"date": r["date"], "rx": r["rx"], "tx": r["tx"]}
                for r in reversed(rows)
            ]
        conn.close()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/monthly")
def api_monthly():
    """Return per-month traffic for the last *months* months."""
    iface_param = request.args.get("interface", "1")
    months = int(request.args.get("months", 24))
    try:
        conn = get_db()
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404
        iid = iface_row["id"]
        result = []
        if "month" in get_schema(conn):
            rows = conn.execute(
                "SELECT date, rx, tx FROM month "
                "WHERE interface=? ORDER BY date DESC LIMIT ?",
                (iid, months),
            ).fetchall()
            result = [
                {"date": r["date"][:7], "rx": r["rx"], "tx": r["tx"]}
                for r in reversed(rows)
            ]
        conn.close()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/hourly")
def api_hourly():
    """Return per-hour traffic for the last *hours* hours."""
    iface_param = request.args.get("interface", "1")
    hours = int(request.args.get("hours", 48))
    try:
        conn = get_db()
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404
        iid = iface_row["id"]
        result = []
        if "hour" in get_schema(conn):
            rows = conn.execute(
                "SELECT date, rx, tx FROM hour "
                "WHERE interface=? ORDER BY date DESC LIMIT ?",
                (iid, hours),
            ).fetchall()
            for row in reversed(rows):
                dt = row["date"]
                parts = dt.split(" ")
                result.append({
                    "date": parts[0],
                    "hour": int(parts[1][:2]),
                    "label": f"{parts[1][:2]}:00",
                    "rx": row["rx"],
                    "tx": row["tx"],
                })
        conn.close()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/today-hourly")
def api_today_hourly():
    """Return the last 24 hourly buckets for one interface."""
    iface_param = request.args.get("interface", "1")
    try:
        conn = get_db()
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404
        iid = iface_row["id"]
        result = []
        if "hour" in get_schema(conn):
            rows = conn.execute(
                "SELECT date, rx, tx FROM hour "
                "WHERE interface=? ORDER BY date DESC LIMIT 24",
                (iid,),
            ).fetchall()
            for row in reversed(rows):
                dt = row["date"]
                date_part = dt[:10]
                hour_val = int(dt[11:13]) if len(dt) > 11 else 0
                result.append({
                    "hour": hour_val,
                    "date": date_part,
                    "label": f"{date_part[5:]} {hour_val:02d}:00",
                    "rx": row["rx"],
                    "tx": row["tx"],
                })
        conn.close()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/fiveminute")
def api_fiveminute():
    """Return 5-minute-granularity traffic for the last *minutes* minutes."""
    iface_param = request.args.get("interface", "1")
    minutes = int(request.args.get("minutes", 720))
    try:
        conn = get_db()
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404
        iid = iface_row["id"]
        result = []
        if "fiveminute" in get_schema(conn):
            limit = max(minutes // 5, 1)
            rows = conn.execute(
                "SELECT date, rx, tx FROM fiveminute "
                "WHERE interface=? ORDER BY date DESC LIMIT ?",
                (iid, limit),
            ).fetchall()
            result = [
                {"time": r["date"], "rx": r["rx"], "tx": r["tx"]}
                for r in reversed(rows)
            ]
        conn.close()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/top-days")
def api_top_days():
    """Return the top *limit* highest-traffic days for one interface."""
    iface_param = request.args.get("interface", "1")
    limit = int(request.args.get("limit", 20))
    try:
        conn = get_db()
        iface_row = get_interface(conn, iface_param)
        if not iface_row:
            conn.close()
            return jsonify({"error": "No interfaces found"}), 404
        iid = iface_row["id"]
        result = []
        if "day" in get_schema(conn):
            rows = conn.execute(
                "SELECT date, rx, tx, (rx+tx) total FROM day "
                "WHERE interface=? ORDER BY total DESC LIMIT ?",
                (iid, limit),
            ).fetchall()
            result = [
                {
                    "rank": idx,
                    "date": r["date"],
                    "rx": r["rx"],
                    "tx": r["tx"],
                    "total": r["total"],
                }
                for idx, r in enumerate(rows, start=1)
            ]
        conn.close()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/refresh")
def api_refresh():
    """Force an immediate re-fetch of the remote database."""
    global _last_fetch  # noqa: PLW0603
    _last_fetch = 0.0
    try:
        fetch_database()
        return jsonify({"status": "ok", "message": "Refreshed"})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = load_settings()
    source_mode = cfg.get("source_mode", False)
    print("=" * 60)
    print("  vnstat Dashboard")
    print(f"  Source: {'local' if source_mode else 'remote'}")
    print(f"  Host:   {cfg.get('host')}")
    print("  Server: http://0.0.0.0:5050")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=False)

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

DATA_FILE = Path(os.environ.get("DATA_FILE", "/data/latest.json"))
PORT = int(os.environ.get("PORT", "80"))
lock = threading.Lock()
handled_scan: list[str | None] = [None]
stop = False


def esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        connect_timeout=5,
    )


def wait_for_database() -> None:
    while not stop:
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            print(f"waiting for database: {exc}", flush=True)
            time.sleep(2)


def schema(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            bssid text NOT NULL,
            ssid text NOT NULL,
            channel integer,
            signal_dbm integer NOT NULL,
            seen_at timestamptz NOT NULL,
            UNIQUE (bssid, seen_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS survey_state (
            id integer PRIMARY KEY,
            cleared_at timestamptz
        )
        """
    )
    conn.execute("INSERT INTO survey_state (id, cleared_at) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING")
    conn.commit()


def load_snapshot() -> dict | None:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ingest_once() -> None:
    snapshot = load_snapshot()
    if snapshot is None:
        return
    scanned_at = parse_time(snapshot.get("scanned_at"))
    if scanned_at is None:
        return
    stamp = scanned_at.isoformat()
    with lock:
        if handled_scan[0] == stamp:
            return
        with connect() as conn:
            cleared_at = conn.execute("SELECT cleared_at FROM survey_state WHERE id = 1").fetchone()[0]
            errored = bool(snapshot.get("error"))
            if errored or (cleared_at is not None and scanned_at <= cleared_at):
                handled_scan[0] = stamp
                return
            rows = []
            for network in snapshot.get("networks") or []:
                if not isinstance(network, dict):
                    continue
                bssid = network.get("bssid")
                signal = network.get("signal_dbm")
                if not isinstance(bssid, str) or len(bssid) != 17 or not isinstance(signal, int):
                    continue
                channel = network.get("channel")
                rows.append(
                    (
                        bssid.lower(),
                        network.get("ssid") if isinstance(network.get("ssid"), str) else "",
                        channel if isinstance(channel, int) else None,
                        signal,
                        scanned_at,
                    )
                )
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO observations (bssid, ssid, channel, signal_dbm, seen_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (bssid, seen_at) DO NOTHING
                        """,
                        rows,
                    )
            conn.commit()
        handled_scan[0] = stamp
        print(f"stored {len(rows)} networks from {stamp}", flush=True)


def clear_survey() -> None:
    with lock:
        with connect() as conn:
            conn.execute("UPDATE survey_state SET cleared_at = now() WHERE id = 1")
            conn.execute("DELETE FROM observations")
            conn.commit()
        print("cleared survey", flush=True)


def fetch_rows() -> tuple[datetime | None, list[tuple]]:
    with lock:
        with connect() as conn:
            cleared_at = conn.execute("SELECT cleared_at FROM survey_state WHERE id = 1").fetchone()[0]
            rows = conn.execute(
                """
                SELECT
                    COALESCE((ARRAY_AGG(ssid ORDER BY seen_at DESC) FILTER (WHERE ssid <> ''))[1], '') AS ssid,
                    bssid,
                    (ARRAY_AGG(channel ORDER BY seen_at DESC))[1] AS channel,
                    (ARRAY_AGG(signal_dbm ORDER BY seen_at DESC))[1] AS signal_dbm,
                    MAX(seen_at) AS last_seen,
                    COUNT(*) AS times_seen
                FROM observations
                GROUP BY bssid
                ORDER BY signal_dbm DESC, last_seen DESC
                """
            ).fetchall()
    return cleared_at, rows


def clock(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def link_text(snapshot: dict | None) -> str:
    if snapshot is None:
        return "No scan yet."
    link = snapshot.get("link") if isinstance(snapshot.get("link"), dict) else {}
    iface = snapshot.get("interface") if isinstance(snapshot.get("interface"), str) else "wlan0"
    if not link.get("connected"):
        return f"{iface} is not associated."
    ssid = link.get("ssid") if isinstance(link.get("ssid"), str) and link.get("ssid") else "(hidden)"
    bssid = link.get("bssid") if isinstance(link.get("bssid"), str) else ""
    signal = link.get("signal_dbm")
    freq = link.get("frequency_mhz")
    channel_no = None
    if isinstance(freq, int):
        if freq == 2484:
            channel_no = 14
        elif 2412 <= freq <= 2472:
            channel_no = (freq - 2407) // 5
        elif 5000 <= freq <= 5885:
            channel_no = (freq - 5000) // 5
    parts = [f"Connected to {ssid}"]
    if bssid:
        parts.append(f"({bssid})")
    if channel_no is not None:
        parts.append(f"on channel {channel_no}")
    if isinstance(signal, int):
        parts.append(f"at {signal} dBm")
    return " ".join(parts) + "."


def render(snapshot: dict | None, cleared_at: datetime | None, rows: list[tuple], problem: str | None) -> str:
    hostname = "this Pi"
    scanned = "No scan yet."
    error = ""
    if snapshot is not None:
        if isinstance(snapshot.get("hostname"), str) and snapshot["hostname"]:
            hostname = snapshot["hostname"]
        scanned_at = parse_time(snapshot.get("scanned_at"))
        if scanned_at is not None:
            scanned = clock(scanned_at)
        if isinstance(snapshot.get("error"), str) and snapshot["error"]:
            error = f'<p class="error">{esc(snapshot["error"])}</p>'
    if problem:
        error += f'<p class="error">{esc(problem)}</p>'

    if rows:
        body = []
        for ssid, bssid, channel, signal, last_seen, times_seen in rows:
            name = esc(ssid) if ssid else "(hidden)"
            channel_text = str(channel) if isinstance(channel, int) else "—"
            body.append(
                "<tr>"
                f"<td>{name}</td>"
                f'<td class="bssid">{esc(bssid)}</td>'
                f"<td>{channel_text}</td>"
                f"<td>{esc(signal)} dBm</td>"
                f"<td>{esc(clock(last_seen))}</td>"
                f"<td>{esc(times_seen)}</td>"
                "</tr>"
            )
        table_body = "".join(body)
    elif cleared_at is not None:
        table_body = '<tr><td colspan="6">Survey cleared. The next scan fills this table.</td></tr>'
    else:
        table_body = '<tr><td colspan="6">No networks stored yet.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <title>Wi-Fi survey</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #f4f6f8; color: #1c1c1c; }}
    .page {{ max-width: 960px; margin: 0 auto; min-height: 100vh; display: flex; flex-direction: column; padding: 1.25rem; box-sizing: border-box; }}
    main {{ flex: 1; }}
    h1 {{ font-size: 1.5rem; margin: 0 0 0.4rem; }}
    p {{ margin: 0.35rem 0; }}
    .meta {{ color: #444; }}
    .error {{ background: #fff4e5; border: 1px solid #e6c48a; padding: 0.6rem 0.8rem; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; margin-top: 1rem; }}
    th, td {{ padding: 0.5rem 0.65rem; border-bottom: 1px solid #e6e8eb; text-align: left; }}
    th {{ font-size: 0.85rem; color: #444; }}
    .bssid {{ font-family: ui-monospace, monospace; }}
    .clear {{ margin-top: 2rem; }}
    .clear p {{ margin-bottom: 0.5rem; }}
    button {{ width: 100%; padding: 0.8rem 1rem; font-size: 1rem; border: 1px solid #b9c0c8; background: #fff; border-radius: 6px; }}
  </style>
</head>
<body>
  <div class="page">
    <main>
      <h1>Wi-Fi survey</h1>
      <p class="meta">{esc(hostname)} · last scan {esc(scanned)}</p>
      <p>{esc(link_text(snapshot))}</p>
      <p class="meta">Scans start when the container starts, then about once a minute. This page refreshes itself.</p>
      {error}
      <table>
        <thead>
          <tr>
            <th>SSID</th>
            <th>BSSID</th>
            <th>Channel</th>
            <th>Signal</th>
            <th>Last seen</th>
            <th>Seen</th>
          </tr>
        </thead>
        <tbody>
          {table_body}
        </tbody>
      </table>
    </main>
    <form class="clear" method="post" action="/clear">
      <p class="meta">Clear removes the stored networks. The Pi stays connected.</p>
      <button type="submit">Clear</button>
    </form>
  </div>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/":
            self.send_error(404)
            return
        problem = None
        try:
            ingest_once()
            cleared_at, rows = fetch_rows()
        except Exception as exc:
            problem = f"Database is not reachable: {exc}"
            cleared_at, rows = None, []
        body = render(load_snapshot(), cleared_at, rows, problem).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/clear":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)
        try:
            clear_survey()
        except psycopg.Error:
            self.send_error(503)
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def ingest_loop() -> None:
    while not stop:
        try:
            ingest_once()
        except Exception:
            traceback.print_exc()
        time.sleep(2)


def main() -> None:
    wait_for_database()
    with connect() as conn:
        schema(conn)
    threading.Thread(target=ingest_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"listening on {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

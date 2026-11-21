import json
import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path(os.environ.get("DATA_FILE", "/data/latest.json"))
INTERFACE = os.environ.get("WLAN_IFACE", "wlan0")
try:
    INTERVAL = max(15, int(os.environ.get("SCAN_INTERVAL", "60") or "60"))
except ValueError:
    INTERVAL = 60
stop = False


def request_stop(signum, frame):
    global stop
    stop = True


def channel(freq: int) -> int | None:
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5000 <= freq <= 5885:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:
        return (freq - 5950) // 5
    return None


def number_at(stripped: str) -> float | None:
    parts = stripped.split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[1])
    except ValueError:
        return None


def parse_scan(text: str) -> list[dict]:
    networks: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("BSS "):
            if current is not None:
                networks.append(current)
            mac = line.split()[1].split("(", 1)[0].lower()
            current = {"bssid": mac, "ssid": "", "frequency_mhz": None, "signal_dbm": None, "channel": None}
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("freq:") and current["frequency_mhz"] is None:
            freq = number_at(stripped)
            if freq is not None:
                current["frequency_mhz"] = int(freq)
                current["channel"] = channel(current["frequency_mhz"])
        elif stripped.startswith("signal:") and current["signal_dbm"] is None:
            signal = number_at(stripped)
            if signal is not None:
                current["signal_dbm"] = int(signal)
        elif stripped.startswith("SSID:"):
            current["ssid"] = stripped[5:].strip()
    if current is not None:
        networks.append(current)

    strongest: dict[str, dict] = {}
    for network in networks:
        bssid = network["bssid"]
        if len(bssid) != 17 or network["signal_dbm"] is None:
            continue
        previous = strongest.get(bssid)
        if previous is None or network["signal_dbm"] > previous["signal_dbm"]:
            strongest[bssid] = network
    return list(strongest.values())[:200]


def parse_link(text: str) -> dict:
    link = {"connected": False, "ssid": "", "bssid": "", "signal_dbm": None, "frequency_mhz": None}
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Connected to "):
        return link
    parts = lines[0].split()
    if len(parts) < 3:
        return link
    link["connected"] = True
    link["bssid"] = parts[2].lower()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("SSID:") and link["ssid"] == "":
            link["ssid"] = stripped[5:].strip()
        elif stripped.startswith("freq:") and link["frequency_mhz"] is None:
            freq = number_at(stripped)
            if freq is not None:
                link["frequency_mhz"] = int(freq)
        elif stripped.startswith("signal:") and link["signal_dbm"] is None:
            signal = number_at(stripped)
            if signal is not None:
                link["signal_dbm"] = int(signal)
    return link


def run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else "iw timed out"
        return subprocess.CompletedProcess(args, 1, stdout, stderr)


def snapshot(iface: str) -> dict:
    link_proc = run(["iw", "dev", iface, "link"])
    scan_proc = run(["iw", "dev", iface, "scan"])
    data = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "interface": iface,
        "error": None,
        "link": parse_link(link_proc.stdout if link_proc.returncode == 0 else ""),
        "networks": [],
    }
    if scan_proc.returncode != 0:
        detail = (scan_proc.stderr or scan_proc.stdout or "").strip()
        data["error"] = (detail or f"iw scan exited {scan_proc.returncode}")[:300]
        return data
    data["networks"] = parse_scan(scan_proc.stdout)
    return data


def publish(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(DATA_FILE)


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop:
        try:
            data = snapshot(INTERFACE)
            publish(data)
        except Exception as exc:
            print(f"scan {INTERFACE} failed: {exc}", flush=True)
        else:
            if data["error"]:
                print(f"scan {INTERFACE} failed: {data['error']}", flush=True)
            else:
                state = "yes" if data["link"]["connected"] else "no"
                print(f"scan {INTERFACE} networks={len(data['networks'])} connected={state}", flush=True)
        for _ in range(INTERVAL):
            if stop:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()

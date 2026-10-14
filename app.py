import collections
import configparser
import datetime
import json
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
from flask import Flask, jsonify, render_template, request, make_response

# --- KONFIGURATION AUS INI LADEN ---
ini_path = os.path.splitext(os.path.abspath(__file__))[0] + '.ini'
config = configparser.ConfigParser()

if not os.path.exists(ini_path):
    print(f"[Warnung] Konfigurationsdatei '{ini_path}' nicht gefunden. Es werden Standardwerte ohne Pushover-Benachrichtigung verwendet.")
    config.read_dict({'Network': {}, 'Radio': {}, 'Pushover': {}, 'Database': {}})
else:
    try:
        config.read(ini_path)
    except Exception as e:
        print(f"[Fehler] Die Datei '{ini_path}' konnte nicht gelesen werden: {e}")
        sys.exit(1)

UDP_BIND_IP = config.get('Network', 'UDP_BIND_IP', fallback="0.0.0.0")
UDP_PORT = config.getint('Network', 'UDP_PORT', fallback=1799)
WEB_PORT = config.getint('Network', 'WEB_PORT', fallback=5000)
NODE_IP = config.get('Network', 'NODE_IP', fallback="192.168.30.174")

MY_CALL = config.get('Radio', 'MY_CALL', fallback="DO5RBB")
LOCAL_NODE_CALL = config.get('Radio', 'LOCAL_NODE_CALL', fallback="DO5RBB-12")
TARGET_TG = config.get('Radio', 'TARGET_TG', fallback="26257")

PUSHOVER_TOKEN = config.get('Pushover', 'TOKEN', fallback="")
PUSHOVER_USER = config.get('Pushover', 'USER', fallback="")

raw_db_path = config.get('Database', 'DB_FILE', fallback="~/meshcom-gateway/meshcom.db")
DB_FILE = os.path.expanduser(raw_db_path)

# --- ENDE KONFIGURATION ---

# Offizielle MeshCom Hardware-ID Zuordnung
HW_ID_MAP = {
    1: "TLORA V2", 2: "TLORA V1", 3: "TLORA V2.1", 4: "TBEAM",
    5: "TBEAM V1.2", 6: "TBEAM 0.7", 7: "T-ECHO", 8: "T-DECK",
    9: "RAK4631", 10: "HELTEC V2", 11: "HELTEC V1", 12: "TBEAM AX2101",
    13: "EBYTE E22", 39: "EBYTE E22", 40: "TBEAM SUPREME", 41: "PIO TLORA",
    42: "HELTEC V2", 43: "HELTEC V3", 44: "HELTEC T114", 45: "T-DECK PLUS",
    46: "T3-S3", 47: "T5 EPAPER", 48: "EBYTE E22 S3", 49: "T-LORA PAGER",
    50: "T-CONNECT PRO", 51: "HELTEC W-TRACKER", 61: "T-WATCH S3"
}

app = Flask(__name__)
last_udp_packets = collections.deque(maxlen=30)


def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    try:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            src TEXT,
            dst TEXT,
            text TEXT
        )""")

        try:
            c.execute("ALTER TABLE messages ADD COLUMN ack INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            c.execute("ALTER TABLE messages ADD COLUMN ack_id TEXT")
        except sqlite3.OperationalError:
            pass

        c.execute("""CREATE TABLE IF NOT EXISTS mheard (
            callsign TEXT PRIMARY KEY,
            path TEXT,
            last_seen TEXT,
            hw TEXT,
            snr REAL,
            rssi REAL,
            distance REAL,
            lat REAL,
            lon REAL,
            alt REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS direct_heard (
            callsign TEXT PRIMARY KEY,
            last_seen TEXT,
            hw TEXT,
            snr REAL,
            rssi REAL,
            distance REAL,
            neighbours INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS links (
            node1 TEXT,
            node2 TEXT,
            last_seen TEXT,
            PRIMARY KEY (node1, node2)
        )""")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Init Error] Die Datenbank konnte nicht initialisiert werden: {e}")


def haversine(lat1, lon1, lat2, lon2):
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0:
        return -1.0
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def resolve_hardware_name(node_data: dict) -> str:
    if isinstance(node_data.get("hw"), str) and node_data["hw"].strip():
        return node_data["hw"].strip()
    hw_id = node_data.get("hw_id")
    if hw_id is not None:
        try:
            hid = int(hw_id)
            if hid in HW_ID_MAP:
                return HW_ID_MAP[hid]
            return f"HW #{hid}"
        except (ValueError, TypeError):
            pass
    return "-"


def send_pushover(title: str, msg: str) -> None:
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return
    url = "https://api.pushover.net/1/messages.json"
    data = urllib.parse.urlencode({
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": msg
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=4):
            pass
    except Exception as e:
        print(f"[Pushover Error] Konnte Benachrichtigung nicht senden: {e}")


def save_link_chain(conn, path_str, now):
    if not path_str or "," not in path_str:
        return
    nodes = [n.strip() for n in path_str.split(",") if n.strip()]
    if len(nodes) < 2:
        return
    c = conn.cursor()
    for i in range(len(nodes) - 1):
        n1, n2 = nodes[i], nodes[i + 1]
        if n1 == n2:
            continue
        pair = sorted([n1, n2])
        c.execute("""INSERT INTO links (node1, node2, last_seen)
                     VALUES (?, ?, ?)
                     ON CONFLICT(node1, node2) DO UPDATE SET last_seen=excluded.last_seen""",
                  (pair[0], pair[1], now))


def clean_meshcom_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\{\d+\}?$", "", text).strip()


def extract_coordinates(payload: dict):
    lat = payload.get("lat") or payload.get("latitude") or payload.get("LAT")
    lon = (
        payload.get("lon")
        or payload.get("long")
        or payload.get("longitude")
        or payload.get("LON")
        or payload.get("LONG")
    )

    if (lat is None or lon is None) and isinstance(payload.get("pos"), dict):
        p = payload["pos"]
        lat = p.get("lat") or p.get("latitude")
        lon = p.get("lon") or p.get("long") or p.get("longitude")

    try:
        if lat is not None and lon is not None:
            f_lat = float(lat)
            f_lon = float(lon)
            if str(payload.get("lat_dir", "N")).upper() == "S" and f_lat > 0:
                f_lat = -f_lat
            if str(payload.get("long_dir", "E")).upper() == "W" and f_lon > 0:
                f_lon = -f_lon
            if f_lat == 0.0 and f_lon == 0.0:
                return None, None
            return f_lat, f_lon
        return None, None
    except (ValueError, TypeError):
        return None, None


def upsert_map_node(c, node_data: dict, fallback_call: str, now: str):
    raw_call = node_data.get("call") or node_data.get("callsign") or node_data.get("name") or fallback_call
    if not raw_call or raw_call == "Unbekannt":
        return

    parts = [p.strip() for p in str(raw_call).split(",") if p.strip()]
    if not parts:
        return
    origin_call = parts[0]
    path = ("," + ",".join(parts[1:])) if len(parts) > 1 else ""

    hw = resolve_hardware_name(node_data)
    snr = float(node_data.get("snr") or 0.0)
    rssi = float(node_data.get("rssi") or 0.0)
    dist = float(node_data.get("dist") or node_data.get("distance") or -1.0)
    alt = float(node_data.get("alt") or 0.0)
    lat, lon = extract_coordinates(node_data)

    c.execute("""INSERT INTO mheard (callsign, path, last_seen, hw, snr, rssi, distance, lat, lon, alt)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(callsign) DO UPDATE SET
                 path=CASE WHEN excluded.path != '' THEN excluded.path ELSE mheard.path END,
                 last_seen=excluded.last_seen,
                 hw=CASE WHEN excluded.hw != '-' THEN excluded.hw ELSE mheard.hw END,
                 snr=CASE WHEN excluded.snr != 0.0 THEN excluded.snr ELSE mheard.snr END,
                 rssi=CASE WHEN excluded.rssi != 0.0 THEN excluded.rssi ELSE mheard.rssi END,
                 distance=CASE WHEN excluded.distance != -1.0 THEN excluded.distance ELSE mheard.distance END,
                 lat=CASE WHEN excluded.lat IS NOT NULL THEN excluded.lat ELSE mheard.lat END,
                 lon=CASE WHEN excluded.lon IS NOT NULL THEN excluded.lon ELSE mheard.lon END,
                 alt=CASE WHEN excluded.alt != 0.0 THEN excluded.alt ELSE mheard.alt END""",
              (origin_call, path, now, hw, snr, rssi, dist, lat, lon, alt))


def upsert_direct_heard(c, node_data: dict, fallback_call: str, now: str):
    raw_call = node_data.get("call") or node_data.get("callsign") or node_data.get("name") or fallback_call
    if not raw_call or raw_call == "Unbekannt":
        return

    try:
        rssi = float(node_data.get("rssi") or 0.0)
        snr = float(node_data.get("snr") or 0.0)
    except (ValueError, TypeError):
        return

    if rssi == 0.0:
        return

    parts = [p.strip() for p in str(raw_call).split(",") if p.strip()]
    if not parts:
        return

    if parts[-1].upper() == LOCAL_NODE_CALL.upper():
        parts.pop()
    if not parts:
        return

    is_single_hop = (len(parts) == 1)
    direct_call = parts[-1]

    if direct_call.upper() == LOCAL_NODE_CALL.upper():
        return

    hw = resolve_hardware_name(node_data) if is_single_hop else "-"
    try:
        dist = float(node_data.get("dist") or node_data.get("distance") or -1.0) if is_single_hop else -1.0
    except (ValueError, TypeError):
        dist = -1.0

    try:
        neighbours = int(node_data.get("nb") or node_data.get("neighbours") or 0)
    except (ValueError, TypeError):
        neighbours = 0

    c.execute("""INSERT INTO direct_heard (callsign, last_seen, hw, snr, rssi, distance, neighbours)
                 VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(callsign) DO UPDATE SET
                 last_seen=excluded.last_seen,
                 hw=CASE WHEN excluded.hw != '-' THEN excluded.hw ELSE direct_heard.hw END,
                 snr=excluded.snr,
                 rssi=excluded.rssi,
                 distance=CASE WHEN excluded.distance != -1.0 THEN excluded.distance ELSE direct_heard.distance END,
                 neighbours=CASE WHEN excluded.neighbours != 0 THEN excluded.neighbours ELSE direct_heard.neighbours END""",
              (direct_call, now, hw, snr, rssi, dist, neighbours))


def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_BIND_IP, UDP_PORT))
    print(f"[UDP] Lausche auf Port {UDP_PORT}...")

    while True:
        conn = None
        try:
            data, addr = sock.recvfrom(8192)
            raw = data.decode("utf-8", errors="ignore").strip()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            last_udp_packets.appendleft({
                "time": now.split(" ")[1],
                "ip": f"RX ⬅ {addr[0]}",
                "raw": raw
            })

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            conn = get_db_connection()
            c = conn.cursor()

            src = payload.get("src", payload.get("from", "Unbekannt"))
            dst = str(payload.get("dst", payload.get("to", "")))

            try:
                pkt_rssi = float(payload.get("rssi") or 0.0)
            except (ValueError, TypeError):
                pkt_rssi = 0.0

            link_path = src
            if pkt_rssi < 0.0:
                parts_upper = [p.strip().upper() for p in src.split(",")]
                if LOCAL_NODE_CALL.upper() not in parts_upper:
                    link_path = f"{src},{LOCAL_NODE_CALL}"

            if "," in link_path:
                save_link_chain(conn, link_path, now)

            raw_msg = payload.get("msg", payload.get("text", ""))
            if raw_msg and payload.get("type") != "pos":
                ack_match = re.match(r"^[\w-]+:ack(\d+)$", raw_msg.strip(), re.IGNORECASE)

                if ack_match:
                    ack_id = ack_match.group(1)
                    c.execute("UPDATE messages SET ack = 1 WHERE ack_id = ?", (ack_id,))
                    conn.commit()
                else:
                    msg_id_match = re.search(r"\{(\d+)\}?$", raw_msg.strip())
                    ack_id_val = msg_id_match.group(1) if msg_id_match else None

                    clean_text = clean_meshcom_text(raw_msg)
                    if clean_text:
                        c.execute("INSERT INTO messages (timestamp, src, dst, text, ack, ack_id) VALUES (?, ?, ?, ?, 0, ?)",
                                  (now, src, dst, clean_text, ack_id_val))
                        conn.commit()

                        origin_call = src.split(",")[0].strip().upper()
                        is_local_gateway = (origin_call == LOCAL_NODE_CALL.upper() or origin_call == MY_CALL.upper())

                        if (not is_local_gateway) or (origin_call == "DO5RBB-44"):
                            should_alert = False
                            alert_title = ""

                            if dst.upper().startswith("DO5RBB"):
                                should_alert = True
                                alert_title = f"MeshCom [DM] {src} ➔ {dst}"
                            elif MY_CALL.upper() in clean_text.upper():
                                should_alert = True
                                alert_title = f"MeshCom [Erwähnung] von {src} (an {dst})"
                            elif dst == TARGET_TG:
                                should_alert = True
                                alert_title = f"MeshCom [TG {TARGET_TG}] von {src}"

                            if should_alert:
                                send_pushover(alert_title, clean_text)

            node_list = (
                payload.get("mh")
                or payload.get("nodes")
                or payload.get("mheard")
                or payload.get("list")
            )

            if isinstance(node_list, list):
                for item in node_list:
                    if isinstance(item, dict):
                        upsert_map_node(c, item, fallback_call="", now=now)
                        upsert_direct_heard(c, item, fallback_call="", now=now)
            else:
                upsert_map_node(c, payload, fallback_call=src, now=now)
                upsert_direct_heard(c, payload, fallback_call=src, now=now)

            conn.commit()
        except Exception as ex:
            print(f"[UDP Loop Exception] {ex}")
        finally:
            if conn:
                conn.close()


@app.route("/")
def index():
    resp = make_response(render_template("index.html", my_call=MY_CALL, default_tg=TARGET_TG))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/data")
def get_data():
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        hours = 24

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute("SELECT timestamp, src, dst, text, ack FROM messages ORDER BY id DESC LIMIT 100")
        msgs = [{"time": r[0], "src": r[1], "dst": r[2], "text": r[3], "ack": r[4]} for r in c.fetchall()]

        c.execute("SELECT lat, lon FROM mheard WHERE callsign = ?", (LOCAL_NODE_CALL,))
        loc_row = c.fetchone()
        my_lat, my_lon = (loc_row[0], loc_row[1]) if loc_row and loc_row[0] and loc_row[1] else (0.0, 0.0)

        c.execute("""SELECT d.callsign, d.last_seen, d.hw, d.snr, d.rssi, d.neighbours, m.lat, m.lon
                     FROM direct_heard d
                     LEFT JOIN mheard m ON d.callsign = m.callsign
                     ORDER BY d.last_seen DESC LIMIT 30""")
        
        direct_nodes = []
        for r in c.fetchall():
            call, last, hw, snr, rssi, nb, lat, lon = r
            
            calc_dist = -1.0
            if my_lat != 0.0 and my_lon != 0.0 and lat and lon and lat != 0.0 and lon != 0.0:
                calc_dist = haversine(my_lat, my_lon, lat, lon)
            
            c.execute("""SELECT COUNT(DISTINCT CASE WHEN node1 = ? THEN node2 ELSE node1 END) 
                         FROM links 
                         WHERE (node1 = ? OR node2 = ?) 
                         AND last_seen >= datetime('now', 'localtime', '-24 hours')""", (call, call, call))
            db_nb = c.fetchone()[0]
            final_nb = db_nb if db_nb > 0 else (nb or 0)

            direct_nodes.append({
                "call": call, "last": last, "hw": hw,
                "snr": snr, "rssi": rssi, "dist": calc_dist, "neighbours": final_nb
            })

        c.execute("SELECT callsign, path, last_seen, hw, snr, rssi, distance, lat, lon, alt FROM mheard ORDER BY last_seen DESC LIMIT 100")
        map_nodes = [{
            "call": r[0], "path": r[1] or "", "last": r[2], "hw": r[3],
            "snr": r[4], "rssi": r[5], "dist": r[6], "lat": r[7], "lon": r[8], "alt": r[9]
        } for r in c.fetchall()]

        c.execute("SELECT node1, node2, last_seen FROM links WHERE last_seen >= datetime('now', 'localtime', ?)", (f"-{hours} hours",))
        links = [{"from": r[0], "to": r[1], "last": r[2]} for r in c.fetchall()]
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()

    resp = jsonify({
        "messages": msgs,
        "direct_nodes": direct_nodes,
        "nodes": map_nodes,
        "links": links,
        "raw_udp": list(last_udp_packets)
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/clear_heard", methods=["POST"])
def clear_heard():
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM direct_heard")
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/send", methods=["POST"])
def send_msg():
    data = request.json or {}
    dst = data.get("dst", TARGET_TG).strip()
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"status": "error", "message": "Text darf nicht leer sein"}), 400

    try:
        payload = {
            "type": "msg",
            "dst": dst,
            "msg": text
        }
        raw_str = json.dumps(payload)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(raw_str.encode("utf-8"), (NODE_IP, UDP_PORT))

        now_time = datetime.datetime.now().strftime("%H:%M:%S")
        last_udp_packets.appendleft({
            "time": now_time,
            "ip": f"TX ➔ {NODE_IP}",
            "raw": raw_str
        })
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    init_db()
    threading.Thread(target=udp_listener, daemon=True).start()
    app.run(host=UDP_BIND_IP, port=WEB_PORT, debug=False)

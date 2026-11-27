import csv
import ctypes
import io
import json
import math
import os
import platform
import subprocess
import threading
import time
import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from ultralytics import YOLO
from core.gps import GPS, FakeGPS, DamageStore, project, haversine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))  
IS_WINDOWS = platform.system() == "Windows"
# Real GPS only on Linux (Raspberry Pi). Force the simulator anywhere with:  FAKE_GPS=1 python app.py
USE_SIM = platform.system() != "Linux" or os.environ.get("FAKE_GPS") == "1"

if USE_SIM:
    gps = FakeGPS()
    store = DamageStore(os.path.join(BASE_DIR, "damages_sim.db"))   # test data never mixes with real data
    if not store.cache:                                             # one fake pothole 300 m along the route
        lat, lon = project(gps.lat0, gps.lon0, gps.heading, 300)
        store.add(0, lat, lon)
        store.session.clear()
        store.seen_now.clear()
else:
    gps = GPS()
    # FIX: absolute path, so the database is the same file no matter where the app is started from
    store = DamageStore(os.path.join(BASE_DIR, "damages.db"))

CLASS_NAMES = {0: "Pothole", 1: "Crack", 2: "Manhole"}
BOX_COLORS = {0: (53, 57, 229), 1: (0, 140, 251), 2: (229, 136, 30)}   # BGR, same colours as the map

MIN_CONF = 0.5          # only save / announce confident detections

def env_float(name, default, lo, hi):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f".env: {name}={raw!r} is not a number")
    if not lo <= value <= hi:
        raise SystemExit(f".env: {name}={value} is outside the allowed range {lo} to {hi}")
    return value

CAM_HEIGHT = env_float("CAM_HEIGHT", 1.2, 0.2, 5)       # metres above the road
PITCH_DEG = env_float("PITCH_DEG", 25, -10, 80)         # downward tilt of the camera
VFOV_DEG = env_float("VFOV_DEG", 48.8, 10, 120)         # vertical field of view (Pi Camera v2 ~48.8, v3 ~41; USB cams vary)
CAMERA_INDEX = 0
FRAME_W, FRAME_H = 640, 480        # smaller frames = faster on the Pi
REPAIR_PASSES = 100                # delete a damage after this many drive-bys without seeing it
SEV_MEDIUM, SEV_LARGE = 0.3, 0.7   # size limits in metres
SEV_NAMES = {0: "Small", 1: "Medium", 2: "Large"}

WARN_SECONDS = 5                   # warn this many seconds before reaching a known damage
MIN_WARN, MAX_WARN = 40, 200       # but never closer than 40 m or farther than 200 m
CONFIRM_HITS = 3                   # detections needed before a damage is saved
CONFIRM_RADIUS = 8                 # metres: sightings this close count as the same damage
CONFIRM_TIMEOUT = 3                # seconds: forget a candidate not seen again
DETECT_EVERY = 1                   # run YOLO on every Nth frame (try 2 if still slow)
VOICE_COOLDOWN = 4

PHOTO_DIR = os.path.join(BASE_DIR, "static", "photos")
os.makedirs(PHOTO_DIR, exist_ok=True)

NCNN_DIR = os.path.join(BASE_DIR, "best_ncnn_model")
if os.path.isdir(NCNN_DIR):                        # Raspberry Pi: fast exported model
    model = YOLO(NCNN_DIR, task="detect")
    IMGSZ = 320                                    # must match the imgsz used in the export
else:                                              # PC: normal model
    model = YOLO(os.path.join(BASE_DIR, "best.pt"))
    IMGSZ = 640

CLASSES = {"option1": [1], "option2": [2], "option3": [0], "option4": [0, 1, 2]}
VOICE = {0: os.path.join(BASE_DIR, "voice", "pothole.mp3"),
         1: os.path.join(BASE_DIR, "voice", "crack.mp3"),
         2: os.path.join(BASE_DIR, "voice", "manhole.mp3")}
VOICE_AHEAD = {0: os.path.join(BASE_DIR, "voice", "pothole_ahead.mp3"),
               1: os.path.join(BASE_DIR, "voice", "crack_ahead.mp3"),
               2: os.path.join(BASE_DIR, "voice", "manhole_ahead.mp3")}

FONT = cv2.FONT_HERSHEY_SIMPLEX
app = Flask(__name__)

selected_option = "option4"
muted = False
announced = set()          # (damage_id, stage) already spoken
last_gps_check = 0
last_voice = 0
last_track = 0
current_fps = 0.0
player = None
candidates = []            # [cls, lat, lon, hits, last_seen, severity, last_frame]


# ---------------------------------------------------------------- camera
class Camera:
    def __init__(self):
        self.cap = None
        self.open()

    def open(self):
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(CAMERA_INDEX)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)       # no stale frames -> less lag

    def read(self):
        ok, frame = self.cap.read()
        return (ok and frame is not None), frame

camera = Camera()


# ---------------------------------------------------------------- voice
def _mci(command):
    buf = ctypes.create_unicode_buffer(255)
    err = ctypes.windll.winmm.mciSendStringW(command, buf, 254, 0)
    return err, buf.value

def is_playing():
    if IS_WINDOWS:
        err, mode = _mci("status voice mode")
        return err == 0 and mode == "playing"
    return player is not None and player.poll() is None

def play_voice(path):
    global player
    if not os.path.isfile(path):
        print(f"Voice file not found: {path}")
        return False
    if is_playing():
        return False
    if IS_WINDOWS:
        _mci("close voice")
        err, _ = _mci(f'open "{path}" type mpegvideo alias voice')
        if err != 0:
            print(f"Windows could not open: {path}")
            return False
        err, _ = _mci("play voice")
        return err == 0
    try:                                                    # Linux / Raspberry Pi
        player = subprocess.Popen(["mpg123", "-q", path])
        return True
    except FileNotFoundError:                               # FIX: used to crash on Linux (fell into the Windows branch)
        print("mpg123 is not installed:  sudo apt install mpg123")
        return False

def stop_voice():
    if IS_WINDOWS:
        _mci("stop voice")
    elif player is not None and player.poll() is None:
        player.terminate()


# ---------------------------------------------------------------- geometry
def ground_distance(y_bottom, frame_h):
    fy = (frame_h / 2) / math.tan(math.radians(VFOV_DEG) / 2)       # focal length in pixels
    angle = math.radians(PITCH_DEG) + math.atan((y_bottom - frame_h / 2) / fy)
    if angle <= 0:
        return None                                                  # at or above the horizon
    return CAM_HEIGHT / math.tan(angle)                              # metres along the ground

def damage_size(x1, y1, x2, y2, h):
    """Rough size in metres: the larger of the box width and its length along the road."""
    d_near = ground_distance(y2, h)
    if not d_near:
        return 0
    fy = (h / 2) / math.tan(math.radians(VFOV_DEG) / 2)
    width = (x2 - x1) * math.hypot(d_near, CAM_HEIGHT) / fy
    d_far = ground_distance(y1, h)
    length = min(d_far - d_near, 3) if d_far else 0
    return max(width, length)

def severity_of(size_m):
    return 2 if size_m >= SEV_LARGE else 1 if size_m >= SEV_MEDIUM else 0

def warn_distance():
    """about WARN_SECONDS of driving, between MIN_WARN and MAX_WARN metres"""
    return min(MAX_WARN, max(MIN_WARN, gps.speed * WARN_SECONDS))


# ---------------------------------------------------------------- drawing
def draw_detections(frame, dets):
    for x1, y1, x2, y2, cls, conf in dets:
        col = BOX_COLORS.get(cls, (255, 255, 255))
        p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
        cv2.rectangle(frame, p1, p2, col, 2)
        text = f"{CLASS_NAMES.get(cls, '?')} {conf:.0%}"
        (tw, th), _ = cv2.getTextSize(text, FONT, 0.5, 1)
        top = max(p1[1] - th - 8, 0)
        cv2.rectangle(frame, (p1[0], top), (p1[0] + tw + 8, top + th + 8), col, -1)
        cv2.putText(frame, text, (p1[0] + 4, top + th + 2), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

def draw_distance_lines(frame, targets):
    """targets = list of (x_center, y_bottom, distance_m)"""
    if not targets:
        return
    h, w = frame.shape[:2]
    start = (w // 2, h - 1)
    base, tip = 28, 2
    overlay = frame.copy()
    for cx, cy, _ in targets:
        end = (int(cx), int(cy))
        poly = np.array([[start[0] - base, start[1]], [start[0] + base, start[1]],
                         [end[0] + tip, end[1]], [end[0] - tip, end[1]]], np.int32)
        cv2.fillConvexPoly(overlay, poly, (0, 255, 0))
    cv2.addWeighted(overlay, 0.35, frame, 0.65, 0, dst=frame)
    for cx, cy, dist in targets:
        end = (int(cx), int(cy))
        cv2.line(frame, start, end, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.circle(frame, end, 5, (0, 0, 255), -1, cv2.LINE_AA)
        text = f"{dist:.1f} m"
        mx, my = (start[0] + end[0]) // 2, (start[1] + end[1]) // 2
        (tw, th), _ = cv2.getTextSize(text, FONT, 0.6, 2)
        cv2.rectangle(frame, (mx + 8, my - th - 6), (mx + tw + 16, my + 6), (0, 0, 0), -1)
        cv2.putText(frame, text, (mx + 12, my), FONT, 0.6, (255, 255, 255), 2, cv2.LINE_AA)


# ---------------------------------------------------------------- damage logic
def confirm_and_store(cls, lat, lon, sev, frame_no):
    """Save a damage after CONFIRM_HITS sightings. Returns the id if a NEW damage was saved."""
    now = time.time()
    candidates[:] = [c for c in candidates if now - c[4] < CONFIRM_TIMEOUT]
    for c in candidates:
        if c[0] == cls and haversine(lat, lon, c[1], c[2]) < CONFIRM_RADIUS:
            c[1], c[2], c[5], c[4] = lat, lon, sev, now      # newest sighting is the closest one
            if c[6] != frame_no:                             # FIX: two boxes in ONE frame counted as 2 hits
                c[3] += 1
                c[6] = frame_no
            if c[3] >= CONFIRM_HITS:
                candidates.remove(c)
                did, is_new = store.add(cls, lat, lon, sev)
                return did if is_new else None
            return None
    candidates.append([cls, lat, lon, 1, now, sev, frame_no])
    return None

def save_photo(frame, did):
    name = f"{did}.jpg"
    cv2.imwrite(os.path.join(PHOTO_DIR, name), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    store.set_photo(did, name)

def remove_photos(ids):
    for i in ids:                                            # FIX: photos of repaired damages were left behind
        try:
            os.remove(os.path.join(PHOTO_DIR, f"{i}.jpg"))
        except OSError:
            pass

def current_warning(pos):
    if not pos or pos[2] is None:
        return None
    hit = store.nearest_ahead(pos[0], pos[1], pos[2], warn_distance())
    if not hit:
        return None
    dist, cls, did = hit
    return dict(id=did, distance=round(dist), type=CLASS_NAMES[cls],
                severity=SEV_NAMES[store.info.get(did, 0)])

def gps_voice():
    global last_gps_check
    if muted or time.time() - last_gps_check < 1:
        return
    last_gps_check = time.time()
    pos = gps.position()
    if not pos or pos[2] is None:
        return
    wd = warn_distance()
    hit = store.nearest_ahead(pos[0], pos[1], pos[2], wd)
    if not hit:
        announced.clear()
        return
    dist, cls, did = hit
    stage = "near" if dist <= wd * 0.4 else "far"
    if (did, stage) in announced or (did, "near") in announced:
        return
    if play_voice(VOICE_AHEAD[cls]):
        announced.add((did, stage))

def track_repairs():
    global last_track
    if time.time() - last_track < 1:
        return
    last_track = time.time()
    pos = gps.position()
    if not pos or gps.speed < 2:                             # no GPS, or the car is stopped
        return
    # FIX: at 70 km/h the car crosses a fixed 10 m circle in under a second and the check was skipped
    check = max(10, gps.speed * 1.5)
    deleted = store.track_passes(pos[0], pos[1], CLASSES[selected_option],
                                 check_m=check, leave_m=check + 15, limit=REPAIR_PASSES)
    if deleted:
        remove_photos(deleted)
        print("Removed (probably repaired):", deleted)


# ---------------------------------------------------------------- detection worker
# FIX: ONE background thread reads the camera and runs YOLO. Before, every browser tab started its own
#      loop on the same camera: double CPU load, double voice alerts, and a race on `candidates`.
latest = {"jpeg": None, "id": 0}
frame_cond = threading.Condition()

def detection_loop():
    global last_voice, current_fps
    frame_no, t0, dets, targets, ran = 0, time.time(), [], [], False
    while True:
        ok, frame = camera.read()
        if not ok:
            print("Camera read failed, reconnecting...")
            time.sleep(1)
            camera.open()
            continue
        frame_no += 1
        h = frame.shape[0]
        new_ids = []

        if not ran or frame_no % DETECT_EVERY == 0:
            ran = True
            res = model(frame, classes=CLASSES[selected_option], imgsz=IMGSZ, verbose=False)[0]
            pos = gps.position()
            dets, targets = [], []
            for box in res.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                cls, conf = int(box.cls[0]), float(box.conf[0])
                dets.append((x1, y1, x2, y2, cls, conf))
                d = ground_distance(y2, h)
                if d:
                    targets.append(((x1 + x2) / 2, y2, d))
                    if pos and conf >= MIN_CONF:
                        lat, lon, course = pos
                        if course is not None:
                            lat, lon = project(lat, lon, course, d)
                        sev = severity_of(damage_size(x1, y1, x2, y2, h))
                        did = confirm_and_store(cls, lat, lon, sev, frame_no)
                        if did:
                            new_ids.append(did)

            # FIX: voice used the model's 0.25 default threshold; now it needs the same MIN_CONF as saving
            alert = [c for *_, c, p in dets if p >= MIN_CONF and c in VOICE]
            if alert and not muted and time.time() - last_voice > VOICE_COOLDOWN:
                if play_voice(VOICE[min(alert)]):
                    last_voice = time.time()

        draw_detections(frame, dets)
        draw_distance_lines(frame, targets)
        for did in new_ids:
            save_photo(frame, did)
        gps_voice()
        track_repairs()

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            continue
        with frame_cond:
            latest["jpeg"] = buf.tobytes()
            latest["id"] += 1
            frame_cond.notify_all()

        if frame_no % 30 == 0:
            current_fps = 30 / (time.time() - t0)
            t0 = time.time()

def generate_frames():
    last = 0
    while True:
        with frame_cond:
            frame_cond.wait_for(lambda: latest["id"] != last, timeout=5)
            if latest["id"] == last:
                continue
            jpeg, last = latest["jpeg"], latest["id"]
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html", selected=selected_option, muted=muted, sim=USE_SIM)

@app.route("/map")
def map_page():
    local = os.path.isfile(os.path.join(BASE_DIR, "static", "vendor", "leaflet", "leaflet.js"))
    return render_template("map.html", leaflet_local=local)

@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/mute", methods=["POST"])
def mute():
    global muted
    muted = bool((request.get_json(silent=True) or {}).get("muted"))   # FIX: bad body no longer gives a 500
    if muted:
        stop_voice()
    return jsonify(status="ok", muted=muted)

@app.route("/select", methods=["POST"])
def select():
    global selected_option
    value = (request.get_json(silent=True) or {}).get("value")
    if value not in CLASSES:                                           # FIX: unknown value used to crash the detector
        return jsonify(status="error", message="unknown option"), 400
    selected_option = value
    return jsonify(status="ok")

@app.route("/api/status")
def api_status():
    pos = gps.position()
    return jsonify(gps=bool(pos), speed=round(gps.speed * 3.6) if pos else 0, muted=muted,
                   option=selected_option, fps=round(current_fps, 1), sim=USE_SIM,
                   warning=current_warning(pos))

@app.route("/api/damages")
def api_damages():
    return jsonify(store.all())

@app.route("/export.csv")
def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "type", "severity", "latitude", "longitude",
                "first_seen_utc", "last_seen_utc", "photo", "google_maps"])
    for d in store.all():
        w.writerow([d["id"], CLASS_NAMES[d["cls"]], SEV_NAMES[d["severity"]],
                    f'{d["lat"]:.6f}', f'{d["lon"]:.6f}', d["seen"], d["last_seen"],
                    d["photo"] or "",
                    f'https://www.google.com/maps?q={d["lat"]:.6f},{d["lon"]:.6f}'])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=road_damages.csv"})

@app.route("/export.geojson")
def export_geojson():
    features = [{"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
                 "properties": {"id": d["id"], "type": CLASS_NAMES[d["cls"]],
                                "severity": SEV_NAMES[d["severity"]],
                                "first_seen_utc": d["seen"], "photo": d["photo"]}}
                for d in store.all()]
    return Response(json.dumps({"type": "FeatureCollection", "features": features}),
                    mimetype="application/geo+json",
                    headers={"Content-Disposition": "attachment; filename=road_damages.geojson"})


if __name__ == "__main__":
    threading.Thread(target=detection_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
import math
import sqlite3
import threading
import time

GPS_PORT = "/dev/serial0"   # Pi UART. USB GPS: "/dev/ttyUSB0" or "/dev/ttyACM0". Windows: "COM5"
GPS_BAUD = 9600
R = 6371000                 # earth radius, metres

# ---------- geo helpers ----------
def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def project(lat, lon, brg, dist):
    """point `dist` metres from (lat, lon) in direction `brg` degrees"""
    d, b = dist / R, math.radians(brg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


# ---------- GPS reader (background thread) ----------
class GPS:
    def __init__(self, port=GPS_PORT, baud=GPS_BAUD):
        self.lat = self.lon = None
        self.course = None          # heading in degrees, only updated while moving
        self.speed = 0.0            # m/s
        self.fix_time = 0
        threading.Thread(target=self._run, args=(port, baud), daemon=True).start()

    def _run(self, port, baud):
        try:
            import serial, pynmea2
        except ImportError as e:    # FIX: a missing package used to kill this thread silently
            print("GPS disabled, missing package:", e, "-> pip install pyserial pynmea2")
            return
        while True:
            try:
                with serial.Serial(port, baud, timeout=1) as ser:
                    while True:
                        line = ser.readline().decode("ascii", errors="ignore").strip()
                        if not line.startswith("$") or line[3:6] != "RMC":
                            continue
                        try:
                            msg = pynmea2.parse(line)
                            if msg.status != "A":          # A = valid fix
                                continue
                            lat, lon = msg.latitude, msg.longitude
                            speed = (msg.spd_over_grnd or 0) * 0.514444   # knots -> m/s
                            course = msg.true_course
                        except (pynmea2.ParseError, ValueError, AttributeError):
                            continue
                        # FIX: write speed/course first and the fix time last, so position() never mixes old and new values
                        self.speed = speed
                        if course is not None and speed > 1.5:
                            self.course = float(course)
                        self.lat, self.lon = lat, lon
                        self.fix_time = time.time()
            except (serial.SerialException, OSError) as e:
                print("GPS error:", e)
                time.sleep(3)

    def position(self):
        """(lat, lon, course) or None when there is no fresh fix"""
        if self.lat is None or time.time() - self.fix_time > 3:
            return None
        lat, lon = self.lat, self.lon
        if self.course is not None and self.speed > 1.5:
            lat, lon = project(lat, lon, self.course, self.speed * (time.time() - self.fix_time))
        return lat, lon, self.course


# ---------- damage database ----------
class DamageStore:
    def __init__(self, path="damages.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS damages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cls INTEGER, lat REAL, lon REAL, seen TEXT)""")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(damages)")}
        for name, typ in [("severity", "INTEGER DEFAULT 0"), ("photo", "TEXT"),
                          ("last_seen", "TEXT"), ("passes", "INTEGER DEFAULT 0")]:
            if name not in have:                       # add new columns to an older database
                self.db.execute(f"ALTER TABLE damages ADD COLUMN {name} {typ}")
        self.db.commit()
        rows = self.db.execute("SELECT id, cls, lat, lon, severity FROM damages").fetchall()
        self.cache = [(i, c, la, lo) for i, c, la, lo, _ in rows]
        self.info = {i: (s or 0) for i, _, _, _, s in rows}     # id -> severity (0, 1, 2)
        self.session = set()       # seen by the camera during the current drive-by: no GPS warning for these
        self.seen_now = set()      # seen by the camera since the last drive-by check
        self.near = set()          # damages the car is passing right now

    def add(self, cls, lat, lon, severity=0, merge_m=10):
        """Returns (id, is_new). Same type within merge_m metres counts as the same damage."""
        with self.lock:
            for i, c, la, lo in self.cache:
                if c == cls and haversine(lat, lon, la, lo) < merge_m:
                    self.session.add(i)
                    self.seen_now.add(i)
                    if severity > self.info.get(i, 0):          # it got bigger
                        self.info[i] = severity
                        self.db.execute("UPDATE damages SET severity=? WHERE id=?", (severity, i))
                        self.db.commit()
                    return i, False
            cur = self.db.execute(
                "INSERT INTO damages (cls, lat, lon, seen, last_seen, severity) "
                "VALUES (?,?,?,datetime('now'),datetime('now'),?)", (cls, lat, lon, severity))
            self.db.commit()
            i = cur.lastrowid
            self.cache.append((i, cls, lat, lon))
            self.info[i] = severity
            self.session.add(i)
            self.seen_now.add(i)
            return i, True

    def set_photo(self, i, name):
        with self.lock:
            self.db.execute("UPDATE damages SET photo=? WHERE id=?", (name, i))
            self.db.commit()

    def all(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, cls, lat, lon, seen, last_seen, severity, photo FROM damages").fetchall()
        return [dict(id=r[0], cls=r[1], lat=r[2], lon=r[3], seen=r[4],
                     last_seen=r[5], severity=r[6] or 0, photo=r[7]) for r in rows]

    def track_passes(self, lat, lon, allowed, check_m=10, leave_m=25, limit=3):
        """Call about once a second while driving. A damage you drove past `limit` times
        in a row without the camera seeing it is deleted (probably repaired)."""
        deleted = []
        with self.lock:
            for i, c, la, lo in list(self.cache):
                d = haversine(lat, lon, la, lo)
                if d <= check_m:
                    self.near.add(i)
                elif d > leave_m and i in self.near:           # the drive-by is finished
                    self.near.discard(i)
                    self.session.discard(i)                    # FIX: warn again on the next pass of the same road
                    if i in self.seen_now:                     # camera saw it: still there
                        self.seen_now.discard(i)
                        self.db.execute("UPDATE damages SET passes=0, last_seen=datetime('now') "
                                        "WHERE id=?", (i,))
                    elif c in allowed:                         # camera was looking for it, saw nothing
                        n = (self.db.execute("SELECT passes FROM damages WHERE id=?",
                                             (i,)).fetchone()[0] or 0) + 1
                        if n >= limit:
                            self.db.execute("DELETE FROM damages WHERE id=?", (i,))
                            self.cache = [x for x in self.cache if x[0] != i]
                            self.info.pop(i, None)
                            self.seen_now.discard(i)
                            deleted.append(i)
                        else:
                            self.db.execute("UPDATE damages SET passes=? WHERE id=?", (n, i))
                    self.db.commit()
        return deleted

    def nearest_ahead(self, lat, lon, course, max_m=150, cone=45):
        """Closest stored damage in front of the vehicle. Large ones warn 50 % earlier."""
        if course is None:                 # FIX: heading unknown (car not moving yet) -> "ahead" is meaningless,
            return None                    #      it used to warn about damages behind the car too
        with self.lock:
            items = list(self.cache)
            seen = set(self.session)
            info = dict(self.info)
        best = None
        for i, cls, la, lo in items:
            if i in seen:
                continue
            d = haversine(lat, lon, la, lo)
            if d > max_m * (1.5 if info.get(i) == 2 else 1):
                continue
            diff = abs((bearing(lat, lon, la, lo) - course + 180) % 360 - 180)
            if diff > cone:
                continue
            if best is None or d < best[0]:
                best = (d, cls, i)
        return best

class FakeGPS:
    """Simulated car for testing on Windows: drives in a straight line, then restarts."""
    def __init__(self, lat=7.0, lon=80.0, heading=90, speed=8, length=600):
        self.lat0, self.lon0 = lat, lon
        self.heading = heading      # degrees, 90 = east
        self.speed = speed          # m/s (8 m/s is about 29 km/h)
        self.length = length        # route length in metres, then it jumps back to the start
        self.t0 = time.time()

    def position(self):
        dist = ((time.time() - self.t0) * self.speed) % self.length
        lat, lon = project(self.lat0, self.lon0, self.heading, dist)
        return lat, lon, self.heading

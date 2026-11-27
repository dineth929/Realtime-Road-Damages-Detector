const feed = $("#videofeed"), offline = $("#offline");

// ---- live stream: show a message and reconnect if it drops ----
feed.addEventListener("load", () => (offline.hidden = true));
feed.addEventListener("error", () => {
  offline.hidden = false;
  setTimeout(() => (feed.src = "/video_feed?t=" + Date.now()), 3000);
});

// ---- detection filter ----
const filter = $("#filter");
filter.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-value]");
  if (!b) return;
  filter.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  fetch("/select", { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify({ value: b.dataset.value }) }).catch(() => {});
});

// ---- voice switch ----
const sw = $("#muteSwitch");
let muted = sw.getAttribute("aria-checked") === "false";
function renderMute() {
  sw.setAttribute("aria-checked", String(!muted));
  $("#voiceText").textContent = muted ? "Off" : "On";
}
sw.addEventListener("click", () => {
  muted = !muted;
  renderMute();
  fetch("/mute", { method: "POST", headers: { "Content-Type": "application/json" },
                   body: JSON.stringify({ muted }) }).catch(() => {});
});

// ---- status (GPS, speed, FPS, warning) every second ----
const gpsChip = $("#gpsChip"), alertBox = $("#alert");
function setGps(ok, text) {
  gpsChip.classList.toggle("ok", ok);
  gpsChip.classList.toggle("bad", !ok);
  $("#gpsText").textContent = text;
}
async function pollStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    setGps(d.gps, d.gps ? "GPS fix" : "GPS: no fix");
    $("#speed").textContent = d.gps ? d.speed : "–";
    $("#fps").textContent = d.fps.toFixed(1);
    muted = d.muted; renderMute();
    const w = d.warning;
    if (w) {
      alertBox.textContent = `${w.severity} ${w.type.toLowerCase()} ahead in ${w.distance} m`;
      alertBox.classList.toggle("large", w.severity === "Large");
    }
    alertBox.classList.toggle("show", !!w);
  } catch {
    setGps(false, "Server offline");
  }
}
setInterval(pollStatus, 1000);
pollStatus();

// ---- statistics + recent detections every 5 seconds ----
async function loadDamages() {
  try {
    const list = await (await fetch("/api/damages")).json();
    $("#stTotal").textContent = list.length;
    [0, 1, 2].forEach((c) => ($("#st" + c).textContent = list.filter((d) => d.cls === c).length));
    const recent = list.sort((a, b) => b.id - a.id).slice(0, 6);
    $("#recent").replaceChildren(...recent.map(damageRow));
    $("#recentEmpty").hidden = recent.length > 0;
  } catch { /* try again next time */ }
}
setInterval(loadDamages, 5000);
loadDamages();

const COLORS = ["#e53935", "#fb8c00", "#1e88e5"];
const RADIUS = [6, 9, 13];
const hasMap = typeof L !== "undefined";   // false when Leaflet could not load (no internet on the Pi)

let map, layer, firstFit = true, all = [], lastKey = "";
const markers = new Map();
const filters = { types: new Set([0, 1, 2]), sev: "all" };

if (hasMap) {
  map = L.map("map").setView([0, 0], 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);
  layer = L.layerGroup().addTo(map);
} else {
  $("#map").append(el("p", "map-msg",
    "The map could not be loaded (no internet connection). The list on the left still works."));
}

function popupFor(d) {
  const box = el("div", "popup");
  box.append(el("b", "", `${SEV[d.severity]} ${TYPES[d.cls].toLowerCase()}`), el("br"),
             el("span", "", "First seen " + fmtTime(d.seen)));
  if (d.photo) { const img = el("img"); img.src = "/static/photos/" + encodeURIComponent(d.photo); box.append(img); }
  return box;
}

function focusOn(d) {
  if (!hasMap) return;
  map.setView([d.lat, d.lon], Math.max(map.getZoom(), 17));
  const m = markers.get(d.id);
  if (m) m.openPopup();
}

function render() {
  const list = all.filter((d) => filters.types.has(d.cls) && (filters.sev === "all" || d.severity === +filters.sev))
                   .sort((a, b) => b.id - a.id);
  $("#count").textContent = `${list.length} of ${all.length}`;
  $("#rows").replaceChildren(...list.map((d) => {
    const li = damageRow(d);
    li.addEventListener("click", () => focusOn(d));
    return li;
  }));
  $("#empty").hidden = list.length > 0;
  if (!hasMap) return;

  layer.clearLayers();
  markers.clear();
  const pts = [];
  list.forEach((d) => {
    const m = L.circleMarker([d.lat, d.lon], { radius: RADIUS[d.severity], color: COLORS[d.cls],
      fillColor: COLORS[d.cls], fillOpacity: 0.7 }).addTo(layer).bindPopup(popupFor(d));
    markers.set(d.id, m);
    pts.push([d.lat, d.lon]);
  });
  if (firstFit && pts.length) {              // zoom to the damages only the first time
    map.fitBounds(pts, { padding: [40, 40], maxZoom: 18 });
    firstFit = false;
  }
}

async function load() {
  try {
    all = await (await fetch("/api/damages")).json();
    const key = all.map((d) => d.id + ":" + d.severity).join(",");
    if (key !== lastKey) { lastKey = key; render(); }   // nothing changed -> don't redraw
  } catch { $("#count").textContent = "Could not load data"; }
}

$("#typeChips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-cls]");
  if (!b) return;
  const c = +b.dataset.cls;
  filters.types.has(c) ? filters.types.delete(c) : filters.types.add(c);
  b.setAttribute("aria-pressed", filters.types.has(c));
  render();
});
$("#sevFilter").addEventListener("change", (e) => { filters.sev = e.target.value; render(); });

load();
setInterval(load, 10000);

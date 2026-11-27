const TYPES = ["Pothole", "Crack", "Manhole"];
const SEV = ["Small", "Medium", "Large"];
const $ = (s, r = document) => r.querySelector(s);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// the database stores UTC "YYYY-MM-DD HH:MM:SS"; show it in the viewer's local time
function fmtTime(s) {
  if (!s) return "";
  const d = new Date(s.replace(" ", "T") + "Z");
  return isNaN(d) ? s : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function damageRow(d) {
  const li = el("li", "row t" + d.cls);
  const thumb = d.photo ? el("img", "thumb") : el("span", "thumb");
  if (d.photo) { thumb.src = "/static/photos/" + encodeURIComponent(d.photo); thumb.alt = ""; thumb.loading = "lazy"; }
  const body = el("div", "row-body");
  const line = el("div", "line");
  line.append(el("strong", "", TYPES[d.cls] || "Damage"), el("span", "tag sev" + d.severity, SEV[d.severity]));
  body.append(line, el("small", "", fmtTime(d.last_seen || d.seen)));
  li.append(thumb, body);
  return li;
}

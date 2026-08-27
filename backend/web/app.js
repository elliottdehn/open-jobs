// JobScream local-first client: résumé -> /embed -> tree descent over the manifest -> Maybe/No on
// groups (downloads only those) -> live logistic regression over the downloaded pool -> export weights.
const API = location.origin;
const $ = (s) => document.querySelector(s);
const state = { vec: null, manifest: null, cent: null, cards: [], decided: new Map(), pool: [], labels: new Map(), w: null, b: 0, shown: 0, dl: { done: 0, total: 0 } };

function show(step) { document.querySelectorAll(".step").forEach((e) => e.classList.remove("active")); $(step).classList.add("active"); }
function norm(v) { let s = 0; for (const x of v) s += x * x; s = Math.sqrt(s) || 1; return Float32Array.from(v, (x) => x / s); }
// exact float32 vector from base64 (little-endian)
function vec(j) { const s = atob(j.v); const buf = new ArrayBuffer(s.length); const u = new Uint8Array(buf); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return new Float32Array(buf); }
function dot(a, b) { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; }

// ---------- manifest ----------
async function loadManifest() {
  if (state.manifest) return;
  $("#st1").textContent = "loading manifest…";
  const [mr, cr] = await Promise.all([fetch(`${API}/data/manifest.json`), fetch(`${API}/data/centroids.bin`)]);
  if (!mr.ok || !cr.ok) throw new Error(mr.status === 404 ? "the job index hasn't been published yet — check back shortly" : `couldn't load the job index (HTTP ${mr.status})`);
  const [m, c] = await Promise.all([mr.json(), cr.arrayBuffer()]);
  state.manifest = m;
  // float16 -> float32
  const u16 = new Uint16Array(c), f = new Float32Array(u16.length);
  for (let i = 0; i < u16.length; i++) f[i] = f16(u16[i]);
  state.cent = f;
  $("#corpus").textContent = `${m.jobs.toLocaleString()} jobs · ${m.leaves.toLocaleString()} groups · ${m.recipe}`;
}
function f16(h) { const s = (h & 0x8000) ? -1 : 1, e = (h >> 10) & 0x1f, m = h & 0x3ff; if (e === 0) return s * m * 2 ** -24; if (e === 31) return m ? NaN : s * Infinity; return s * (1 + m / 1024) * 2 ** (e - 15); }
function centroid(i) { const d = state.manifest.dims; return state.cent.subarray(i * d, (i + 1) * d); }

// ball-bound best-first descent; returns nodes in lower-bound order at an adaptive level
function nearestNodes(q, want) {
  const T = state.manifest.tree;
  const lb = (n) => Math.max(0, 1 - dot(q, centroid(n.id)) - n.radius);
  const pq = [[lb(T[0]), 0]];
  const out = [];
  while (pq.length && out.length < want * 3) {
    pq.sort((a, b) => a[0] - b[0]);
    const [, id] = pq.shift(); const n = T[id];
    // present leaves, or internal nodes that are already tight enough to be one card
    if (!n.children.length || n.radius <= 0.45) out.push(n); else for (const c of n.children) pq.push([lb(T[c]), c]);
  }
  return out.sort((a, b) => (1 - dot(q, centroid(a.id))) - (1 - dot(q, centroid(b.id)))).slice(0, want);
}
function leavesUnder(n) { const T = state.manifest.tree; const out = []; const st = [n]; while (st.length) { const x = st.pop(); if (!x.children.length) out.push(x); else for (const c of x.children) st.push(T[c]); } return out; }

// ---------- step 1 ----------
$("#go").onclick = async () => {
  const text = $("#resume").value.trim();
  if (text.length < 20) { $("#st1").textContent = "paste a bit more text"; return; }
  $("#go").disabled = true;
  try {
    await loadManifest();
    $("#st1").textContent = "embedding…";
    const r = await fetch(`${API}/embed`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text, title: $("#title").value, location: $("#loc").value }) });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error ? `${e.error}${e.retryAfterSeconds ? ` (retry in ${e.retryAfterSeconds}s)` : ""}` : `HTTP ${r.status}`); }
    const { vector, recipe } = await r.json();
    if (recipe !== state.manifest.recipe) $("#st1").textContent = `warning: embedding recipe ${recipe} ≠ manifest ${state.manifest.recipe}`;
    state.vec = norm(vector);
    renderCards(24); show("#s2");
  } catch (e) { $("#st1").textContent = String(e.message || e); }
  $("#go").disabled = false;
};

// ---------- step 2 ----------
function renderCards(want) {
  state.cards = nearestNodes(state.vec, want);
  const el = $("#cards"); el.innerHTML = "";
  for (const n of state.cards) {
    const sim = dot(state.vec, centroid(n.id));
    const d = document.createElement("div"); d.className = "card " + (state.decided.get(n.id) || ""); d.dataset.id = n.id;
    d.innerHTML = `<div class="lbl">${esc(n.label || n.medoid)}</div><div class="meta">${n.size.toLocaleString()} jobs${n.distinct_titles ? ` (${n.distinct_titles} distinct titles)` : ""} · similarity ${sim.toFixed(2)} · spread ${n.radius.toFixed(2)}</div>
      <ul>${n.exemplars.map((e) => `<li>${esc(e.title)} <span>· ${esc(e.company)}${e.location ? " · " + esc(e.location) : ""}</span></li>`).join("")}</ul>
      <div class="btns"><button class="btn-yes">Maybe</button><button class="btn-no">No</button></div>`;
    d.querySelector(".btn-yes").onclick = () => decide(n, "maybe", d);
    d.querySelector(".btn-no").onclick = () => decide(n, "no", d);
    el.appendChild(d);
  }
}
$("#more").onclick = () => renderCards(state.cards.length + 24);
function decide(n, v, el) {
  state.decided.set(n.id, v); el.className = "card " + v;
  if (v === "maybe") for (const leaf of leavesUnder(n)) fetchGroup(leaf);
  $("#next2").disabled = ![...state.decided.values()].includes("maybe");
}
const groups = new Map();
async function fetchGroup(leaf) {
  if (groups.has(leaf.id)) return;
  groups.set(leaf.id, null); state.dl.total++; updDl();
  const gr = await fetch(`${API}/data/groups/${leaf.id}.json`);
  if (!gr.ok) { groups.delete(leaf.id); state.dl.total--; updDl(); return; }
  const g = await gr.json();
  for (const j of g.jobs) {
    j.v = vec(j); j.leaf = leaf.id; j.sim = dot(state.vec, j.v); state.pool.push(j);
  }
  groups.set(leaf.id, g); state.dl.done++; updDl();
}
function updDl() { $("#dl").textContent = `${state.dl.done}/${state.dl.total} groups downloaded · ${state.pool.length.toLocaleString()} jobs local`; $("#dlbar").style.width = state.dl.total ? `${(100 * state.dl.done) / state.dl.total}%` : "0"; }
$("#next2").onclick = () => { seedNegatives(); refit(); renderJobs(true); show("#s3"); };

// ---------- step 3: labeling + logistic regression ----------
function seedNegatives() {
  // a few jobs from each "No" group as hard negatives (fetched lazily, small)
  for (const [id, v] of state.decided) if (v === "no") {
    const n = state.manifest.tree[id];
    for (const leaf of leavesUnder(n).slice(0, 2)) fetch(`${API}/data/groups/${leaf.id}.json`).then((r) => r.json()).then((g) => {
      for (const j of g.jobs.slice(0, 5)) { j.v = vec(j); j.neg = true; j.sim = dot(state.vec, j.v); state.pool.push(j); state.labels.set(key(j), 0); }
      refit(); renderJobs(false);
    });
  }
}
const key = (j) => `${j.ats}/${j.slug}#${j.id}`;
function refit() {
  const D = state.manifest.dims; const items = state.pool.filter((j) => state.labels.has(key(j)));
  const pos = items.filter((j) => state.labels.get(key(j)) === 1).length, neg = items.length - pos;
  if (pos === 0 || neg === 0) { state.w = null; $("#st3").textContent = `label at least one yes and one no (have ${pos} yes / ${neg} no) — ranked by résumé similarity meanwhile`; return; }
  // logistic regression on top of the résumé similarity: init w = résumé vector, few epochs of SGD with L2
  const w = Float32Array.from(state.vec); let b = 0; const lr = 0.5, l2 = 0.01;
  for (let ep = 0; ep < 30; ep++) for (const j of items) {
    const y = state.labels.get(key(j)), p = 1 / (1 + Math.exp(-(dot(w, j.v) + b))), g = p - y;
    for (let i = 0; i < D; i++) w[i] -= lr * (g * j.v[i] + l2 * (w[i] - state.vec[i]));
    b -= lr * g;
  }
  state.w = w; state.b = b;
  $("#st3").textContent = `model: ${pos} yes / ${neg} no · ${state.pool.length.toLocaleString()} jobs ranked locally`;
}
function score(j) { return state.w ? 1 / (1 + Math.exp(-(dot(state.w, j.v) + state.b))) : j.sim; }
let ranked = [];
function renderJobs(reset) {
  ranked = state.pool.filter((j) => !j.neg).sort((a, b) => score(b) - score(a));
  if (reset) state.shown = 0;
  const el = $("#jobs"); el.innerHTML = "";
  const N = Math.min(ranked.length, state.shown + 40);
  for (let i = 0; i < N; i++) el.appendChild(jobEl(ranked[i], i));
  state.shown = N; cur = Math.min(cur, N - 1); hi();
}
$("#morejobs").onclick = () => renderJobs(false);
function jobEl(j, i) {
  const d = document.createElement("div"); const l = state.labels.get(key(j)); d.className = "job " + (l === 1 ? "pos" : l === 0 ? "neg" : ""); d.dataset.i = i;
  d.innerHTML = `<div class="t"><b>${esc(j.title)}</b><div>${esc(j.company)}${j.location ? " · " + esc(j.location) : ""} · <a href="${esc(j.url)}" target="_blank" rel="noopener">open ↗</a></div><div class="jd" style="display:none;margin-top:8px;color:#c9c9d0;font-size:13px;white-space:pre-wrap">${esc(j.jd || "(no description)")}</div></div><div class="s">${score(j).toFixed(2)}</div>`;
  d.onclick = () => { cur = i; hi(); const x = d.querySelector(".jd"); x.style.display = x.style.display === "none" ? "block" : "none"; };
  return d;
}
let cur = 0;
function hi() { document.querySelectorAll(".job").forEach((e, i) => (e.style.outline = i === cur ? "2px solid var(--acc)" : "")); const e = document.querySelector(`.job[data-i="${cur}"]`); e && e.scrollIntoView({ block: "nearest" }); }
document.addEventListener("keydown", (ev) => {
  if (!$("#s3").classList.contains("active") || ev.target.tagName === "TEXTAREA" || ev.target.tagName === "INPUT") return;
  const j = ranked[cur]; if (!j) return;
  if (ev.key === "j" || ev.key === "J") { state.labels.set(key(j), 1); refit(); renderJobs(false); cur++; hi(); }
  else if (ev.key === "k" || ev.key === "K") { state.labels.set(key(j), 0); refit(); renderJobs(false); cur++; hi(); }
  else if (ev.key === " ") { ev.preventDefault(); cur++; hi(); }
  else if (ev.key === "ArrowUp") { cur = Math.max(0, cur - 1); hi(); } else if (ev.key === "ArrowDown") { cur++; hi(); }
});
$("#export").onclick = () => {
  const m = { recipe: state.manifest.recipe, dims: state.manifest.dims, w: state.w ? Array.from(state.w, (x) => +x.toFixed(5)) : Array.from(state.vec, (x) => +x.toFixed(5)), b: +state.b.toFixed(5),
    groups_maybe: [...state.decided].filter(([, v]) => v === "maybe").map(([id]) => id), labels: [...state.labels].map(([k, v]) => [k, v]), exported_at: Date.now() };
  const el = $("#exp"); el.style.display = "block"; el.textContent = JSON.stringify(m); navigator.clipboard?.writeText(JSON.stringify(m));
  $("#st3").textContent += " · search JSON copied to clipboard";
};
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

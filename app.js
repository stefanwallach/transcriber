"use strict";
/* Transcriber Beta: page logic. Talks to the local Python server (app.py). */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const desktop = () => window.pywebview && window.pywebview.api;
const newId32 = () => crypto.randomUUID().replace(/-/g, "");
const newId = () => Math.random().toString(36).slice(2, 12);
const LANGS = { en: "English", multi: "Mixed languages", sw: "Swahili", fr: "French", es: "Spanish", pt: "Portuguese", de: "German", it: "Italian", nl: "Dutch", hi: "Hindi", ar: "Arabic", zh: "Chinese" };
const MEDIA_EXT = /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|wma|amr|aiff?|mp4|m4v|mov|mkv|webm|avi|wmv|mpe?g|3gp|ts|mts)$/i;
const ICON = {
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  all: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  flag: '<svg viewBox="0 0 24 24"><path d="M6 3h12v18l-6-4-6 4z"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
};

function clock(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60, p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(x)}` : `${m}:${p(x)}`;
}
function parseClock(t) {
  const parts = String(t).trim().split(":").map(Number);
  if (!parts.length || parts.some((n) => isNaN(n))) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}
function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return "Today, " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
}
const money = (usd) => usd == null ? "" : usd < 0.01 ? "under $0.01" : "$" + usd.toFixed(2);
async function req(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "Something went wrong.");
  return j;
}
function toast(msg, action, fn, ms = 4500) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<span>${esc(msg)}</span>` + (action ? `<button>${esc(action)}</button>` : "");
  if (action) el.querySelector("button").onclick = () => { fn(); el.remove(); };
  $("toasts").append(el);
  setTimeout(() => el.remove(), ms);
}
const isTyping = (el) => el && (el.matches?.("input, textarea, select") || el.closest?.("[contenteditable]"));

/* ======================================================= preferences */
const DEFAULTS = { language: "en", model: "nova-3", diarize: false, summary: false, filler: false, profanity: false,
  redact: false, keepMedia: true, keyterms: "", folder: "", stamps: false, speakersOut: true, writeupsOut: true,
  follow: true, unsure: true, rate: "1", autoUpdate: true, folders: [], geminiModel: "", writeEnglish: false };
let prefs = { ...DEFAULTS };
let status = {};
const OPT_IDS = ["language", "model", "keyterms", "diarize", "summary", "filler", "profanity", "redact", "keepMedia", "folder"];
function setPref(k, v) { prefs[k] = v; req("PUT", "/api/prefs", { [k]: v }).catch(() => {}); }
function bindOptions() {
  for (const k of OPT_IDS) {
    const el = $("o-" + k);
    if (el.type === "checkbox") { el.checked = !!prefs[k]; el.onchange = () => setPref(k, el.checked); }
    else { el.onchange = () => setPref(k, el.value); }
  }
  renderFolderOption();
  for (const k of ["language", "model", "keyterms"]) $("o-" + k).value = prefs[k];
  $("follow").checked = prefs.follow;
  $("follow").onchange = () => setPref("follow", $("follow").checked);
  $("rate").value = prefs.rate;
}
function renderFolderOption() {
  const sel = $("o-folder");
  sel.innerHTML = `<option value="">No folder</option>` + folderNames().map((f) => `<option>${esc(f)}</option>`).join("");
  sel.value = folderNames().includes(prefs.folder) ? prefs.folder : "";
}

/* ======================================================= menus and dialogs */
let menuEl = null;
function closeMenu() { menuEl?.remove(); menuEl = null; }
function openMenu(anchor, items, extraHtml, alignRight) {
  closeMenu();
  const m = document.createElement("div");
  m.className = "menu"; m.setAttribute("role", "menu");
  for (const it of items) {
    if (it.sep) { m.insertAdjacentHTML("beforeend", `<div class="sep"></div>`); continue; }
    if (it.heading) { m.insertAdjacentHTML("beforeend", `<div class="mh">${esc(it.heading)}</div>`); continue; }
    const b = document.createElement("button");
    b.className = "mi" + (it.danger ? " danger" : ""); b.setAttribute("role", "menuitem");
    b.innerHTML = `<span>${esc(it.label)}</span>` + (it.hint ? `<small>${esc(it.hint)}</small>` : it.check ? `<small>✓</small>` : "");
    b.onclick = () => { closeMenu(); it.fn(); };
    m.append(b);
  }
  if (extraHtml) m.insertAdjacentHTML("beforeend", extraHtml);
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  let left = alignRight ? r.right - m.offsetWidth : r.left;
  left = Math.max(10, Math.min(left, innerWidth - m.offsetWidth - 10));
  const top = r.bottom + 6 + m.offsetHeight > innerHeight ? Math.max(10, r.top - m.offsetHeight - 6) : r.bottom + 6;
  m.style.left = left + "px"; m.style.top = top + "px";
  menuEl = m;
  m.querySelector("button")?.focus();
  return m;
}
document.addEventListener("mousedown", (e) => { if (menuEl && !menuEl.contains(e.target)) closeMenu(); });

let dialogEl = null;
function dialog(html, setup) {
  closeDialog();
  const s = document.createElement("div");
  s.className = "scrim";
  s.innerHTML = `<div class="dialog" role="dialog" aria-modal="true">${html}</div>`;
  s.addEventListener("mousedown", (e) => { if (e.target === s) closeDialog(); });
  s.querySelectorAll("[data-close]").forEach((b) => b.onclick = closeDialog);
  document.body.append(s);
  dialogEl = s;
  setup?.(s);
  (s.querySelector("input:not([type=checkbox]), .btn.danger, .btn.primary") || s.querySelector("button"))?.focus();
}
function closeDialog() { dialogEl?.remove(); dialogEl = null; }
function askText(title, label, value, okLabel) {
  return new Promise((resolve) => {
    dialog(`<h2>${esc(title)}</h2><div class="field"><label for="askIn">${esc(label)}</label><input type="text" id="askIn" value="${esc(value || "")}"></div>
      <div class="end"><button class="btn" data-close>Cancel</button><button class="btn primary" id="askOk">${esc(okLabel)}</button></div>`, (d) => {
      const inp = d.querySelector("#askIn");
      const ok = () => { const v = inp.value.trim(); if (!v) return inp.focus(); closeDialog(); resolve(v); };
      d.querySelector("#askOk").onclick = ok;
      inp.onkeydown = (e) => { if (e.key === "Enter") ok(); };
      d.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => resolve(null)));
      setTimeout(() => inp.select(), 0);
    });
  });
}
function confirmBox(title, text, okLabel) {
  return new Promise((resolve) => {
    dialog(`<h2>${esc(title)}</h2><p class="hint" style="font-size:14px">${esc(text)}</p>
      <div class="end"><button class="btn" data-close>Cancel</button><button class="btn danger" id="cOk">${esc(okLabel)}</button></div>`, (d) => {
      d.querySelector("#cOk").onclick = () => { closeDialog(); resolve(true); };
      d.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => resolve(false)));
    });
  });
}

/* ======================================================= views */
let hasKey = true;
function show(view) {
  $("viewNew").hidden = view !== "new";
  $("viewDoc").hidden = view !== "doc";
  $("viewRec").hidden = view !== "rec";
}
async function showNew() {
  if (rec) return toast("Stop or discard the recording first.");
  await flushSave();
  pauseMedia();
  doc = null;
  show("new");
  renderLibrary();
}
$("newBtn").onclick = showNew;

/* ======================================================= keys, credit, updates */
$("keySave").onclick = async () => {
  const key = $("keyInput").value.trim();
  if (!key) return toast("Paste your API key first.");
  try { await req("POST", "/api/key", { key, kind: "deepgram" }); $("keyInput").value = ""; hasKey = true; $("keyCard").hidden = true; toast("Key saved"); pump(); loadCredit(); }
  catch (e) { toast(e.message); }
};
async function loadCredit() {
  const el = $("credit");
  try {
    const a = await req("GET", "/api/account");
    if (a.error || a.balance == null) { el.textContent = ""; return; }
    el.textContent = "Credit $" + a.balance.toFixed(2);
    el.classList.toggle("low", a.balance < 2);
    el.title = "Deepgram credit left on your account";
  } catch { el.textContent = ""; }
}
let updateInfo = null;
async function checkUpdates(manual) {
  try {
    const u = await req("GET", "/api/update/check");
    if (u.error) { if (manual) toast(u.error); return u; }
    updateInfo = u;
    renderUpdate();
    if (manual && !u.available) toast(`You have the latest version (${u.current}).`);
    return u;
  } catch (e) { if (manual) toast("Couldn't check for updates."); }
}
function renderUpdate() {
  const box = $("updateBox");
  const u = updateInfo;
  if (!u || !u.available) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<b>Version ${esc(u.latest)} is available</b><span class="hint">You have ${esc(u.current)}.</span>
    <div class="row">${u.canInstall ? `<button class="btn sm primary" id="updGo">Update now</button>` : `<span class="hint">Install it from the installer.</span>`}
    <button class="link-btn" id="updNotes">What's new</button></div>`;
  $("updNotes").onclick = () => dialog(`<h2>Version ${esc(u.latest)}</h2><p style="white-space:pre-wrap">${esc(u.notes || "No notes for this version.")}</p>
    <div class="end"><button class="btn" data-close>Close</button></div>`);
  $("updGo")?.addEventListener("click", installUpdate);
}
async function installUpdate() {
  await flushSave();
  if (jobs.some((j) => ["uploading", "processing", "downloading", "transcribing"].includes(j.status))) {
    if (!(await confirmBox("Update now?", "Transcriptions in progress will stop. You can start them again after the update.", "Update"))) return;
  }
  try { await req("POST", "/api/update/install"); } catch (e) { return toast(e.message); }
  const box = $("updateBox");
  const poll = setInterval(async () => {
    const s = await req("GET", "/api/update/status").catch(() => null);
    if (!s) return; // the app is closing to install
    if (s.phase === "error") { clearInterval(poll); toast(s.error); renderUpdate(); return; }
    box.innerHTML = s.phase === "installing" ? `<b>Installing the update…</b><span class="hint">Transcriber will reopen by itself.</span>`
      : `<b>Downloading the update… ${Math.round((s.pct || 0) * 100)}%</b><div class="bar"><i style="width:${(s.pct || 0) * 100}%"></i></div>`;
  }, 500);
}

/* ======================================================= adding work */
const jobs = [];
const MAX_PARALLEL = 2;
const drop = $("drop");
drop.onclick = () => $("picker").click();
drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("picker").click(); } };
$("picker").onchange = (e) => { addFiles(e.target.files); e.target.value = ""; };
let dragDepth = 0;
document.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types?.includes("Files")) { dragDepth++; $("dropOverlay").hidden = false; } });
document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("dropOverlay").hidden = true; } });
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("dropOverlay").hidden = true; if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files); });

function needKey() {
  if (hasKey) return false;
  showNew(); $("keyInput").focus(); toast("Add your Deepgram API key first.");
  return true;
}
function currentOpts() {
  const o = {}; for (const k of OPT_IDS) o[k] = prefs[k];
  return o;
}
function addFiles(list, overrides) {
  if (needKey()) return;
  let added = 0;
  for (const file of list) {
    if (!(file.type.startsWith("audio/") || file.type.startsWith("video/") || MEDIA_EXT.test(file.name))) { toast(`${file.name} isn't an audio or video file.`); continue; }
    if (file.size > 2 * 1024 ** 3) { toast(`${file.name} is over 2 GB.`); continue; }
    const job = { kind: "file", id: newId32(), name: file.name, file, opts: { ...currentOpts(), ...overrides }, status: "waiting", pct: 0 };
    jobs.push(job); added++;
    probeDuration(file).then((d) => { job.estimate = estimateCost(d); renderJobs(); });
  }
  if (added) { renderJobs(); pump(); }
}
$("linkForm").onsubmit = (e) => {
  e.preventDefault();
  if (needKey()) return;
  const url = $("linkIn").value.trim();
  if (!/^https?:\/\//i.test(url)) return toast("Paste a full link that starts with https://");
  let name = "Linked recording";
  try { name = decodeURIComponent(new URL(url).pathname.split("/").pop()) || name; } catch { /* keep default */ }
  jobs.push({ kind: "url", id: newId32(), name, url, opts: currentOpts(), status: "waiting", pct: 0 });
  $("linkIn").value = "";
  renderJobs(); pump();
};

function probeDuration(file) {
  return new Promise((res) => {
    const el = document.createElement(file.type.startsWith("video") ? "video" : "audio");
    const u = URL.createObjectURL(file);
    const done = (v) => { URL.revokeObjectURL(u); res(v); };
    el.preload = "metadata";
    el.onloadedmetadata = () => done(isFinite(el.duration) ? el.duration : null);
    el.onerror = () => done(null);
    setTimeout(() => done(null), 5000);
    el.src = u;
  });
}
function costRate() {
  let usd = 0, sec = 0;
  for (const m of allMeta) if (m.cost != null && m.duration) { usd += m.cost; sec += m.duration; }
  return sec > 60 ? usd / sec : null;
}
function estimateCost(seconds) { const r = costRate(); return seconds && r ? seconds * r : null; }

const running = () => jobs.filter((j) => ["uploading", "processing", "downloading", "transcribing", "starting"].includes(j.status)).length;
function pump() {
  while (running() < MAX_PARALLEL) {
    const next = jobs.find((j) => j.status === "waiting");
    if (!next) break;
    next.kind === "url" ? runLink(next) : runFile(next);
  }
}
function jobDone(job, data) {
  jobs.splice(jobs.indexOf(job), 1);
  loadLibrary(); setTimeout(loadCredit, 6000);
  if (!doc && !rec && !$("viewNew").hidden && !jobs.length) openDoc(data.id, data);
  else toast(`"${data.title}" is ready`, "Open", () => openDoc(data.id));
  renderJobs(); pump();
}
function jobFailed(job, msg) { job.status = "error"; job.err = msg; renderJobs(); pump(); }

function runFile(job) {
  const o = job.opts;
  job.status = "uploading"; job.pct = 0; job.err = "";
  const qs = new URLSearchParams({ id: job.id, name: job.name, language: o.language, model: o.model,
    diarize: +o.diarize, summary: +o.summary, filler: +o.filler, profanity: +o.profanity, redact: +o.redact,
    keepMedia: +o.keepMedia, folder: o.folder || "", keyterms: o.keyterms || "" });
  const xhr = new XMLHttpRequest();
  job.xhr = xhr;
  xhr.open("POST", "/api/transcribe?" + qs);
  xhr.setRequestHeader("Content-Type", job.file.type || "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    job.pct = e.loaded / e.total;
    if (job.pct >= 1 && job.status === "uploading") { job.status = "processing"; job.t0 = Date.now(); }
    renderJobs();
  };
  xhr.upload.onload = () => { if (job.status === "uploading") { job.status = "processing"; job.t0 = Date.now(); renderJobs(); } };
  xhr.onload = () => {
    let data; try { data = JSON.parse(xhr.responseText); } catch { data = { error: "Transcriber got an unreadable reply." }; }
    if (xhr.status === 200) jobDone(job, data);
    else if (data.cancelled || job.status === "cancelled") { const i = jobs.indexOf(job); if (i >= 0) jobs.splice(i, 1); renderJobs(); pump(); }
    else jobFailed(job, data.error || "Transcription failed.");
  };
  xhr.onerror = () => { if (job.status !== "cancelled") jobFailed(job, "Lost connection while uploading."); };
  xhr.send(job.file);
  renderJobs();
}
async function runLink(job) {
  job.status = "starting"; job.err = ""; job.pct = 0;
  try { await req("POST", "/api/transcribe-url", { id: job.id, url: job.url, opts: job.opts }); }
  catch (e) { return jobFailed(job, e.message); }
  const poll = async () => {
    if (job.status === "cancelled") return;
    let s;
    try { s = await req("GET", "/api/jobs/" + job.id); } catch { return jobFailed(job, "Lost track of this transcription."); }
    if (s.phase === "done") return jobDone(job, s.doc);
    if (s.phase === "error") return jobFailed(job, s.error);
    if (s.phase === "cancelled") return;
    job.status = { downloading: "downloading", uploading: "uploading", transcribing: "processing" }[s.phase] || "starting";
    job.pct = s.pct || 0;
    if (job.status === "processing" && !job.t0) job.t0 = Date.now() - (s.elapsed || 0) * 1000;
    renderJobs();
    setTimeout(poll, 700);
  };
  poll();
}
async function cancelJob(job) {
  if (["uploading", "processing", "downloading", "starting"].includes(job.status)) {
    job.status = "cancelled";
    await fetch("/api/cancel/" + job.id, { method: "POST" }).catch(() => {});
    job.xhr?.abort();
  }
  const i = jobs.indexOf(job); if (i >= 0) jobs.splice(i, 1);
  renderJobs(); pump();
}
function renderJobs() {
  $("jobsBox").hidden = !jobs.length;
  $("jobs").innerHTML = "";
  for (const job of jobs) {
    const el = document.createElement("div");
    el.className = "job";
    let state = "Waiting to start", bar = "";
    const est = job.estimate != null ? `, about ${money(job.estimate)}` : "";
    if (job.status === "starting") { state = "Opening the link"; bar = `<div class="bar indet"><i></i></div>`; }
    if (job.status === "downloading") { state = job.pct ? `Downloading ${Math.round(job.pct * 100)}%` : "Downloading"; bar = job.pct ? `<div class="bar"><i style="width:${job.pct * 100}%"></i></div>` : `<div class="bar indet"><i></i></div>`; }
    if (job.status === "uploading") { state = `Uploading ${Math.round(job.pct * 100)}%${est}`; bar = `<div class="bar"><i style="width:${job.pct * 100}%"></i></div>`; }
    if (job.status === "processing") { state = `Transcribing, ${clock((Date.now() - (job.t0 || Date.now())) / 1000)}${est}`; bar = `<div class="bar indet"><i></i></div>`; }
    if (job.status === "waiting" && est) state += est;
    if (job.status === "error") state = job.err;
    el.innerHTML = `<div class="job-top"><div class="job-name" title="${esc(job.name)}">${esc(job.name)}</div>
      <button class="icon-btn" data-a="x" title="${job.status === "error" ? "Dismiss" : "Cancel"}">${ICON.x}</button></div>
      <div class="job-state ${job.status === "error" ? "err" : ""}">${esc(state)}</div>${bar}
      ${job.status === "error" ? `<div class="job-actions"><button class="link-btn" data-a="retry">Try again</button></div>` : ""}`;
    el.querySelector('[data-a="x"]').onclick = () => cancelJob(job);
    const retry = el.querySelector('[data-a="retry"]');
    if (retry) retry.onclick = () => { job.status = "waiting"; job.id = newId32(); job.t0 = 0; renderJobs(); pump(); };
    $("jobs").append(el);
  }
}
setInterval(() => { if (jobs.some((j) => j.status === "processing")) renderJobs(); }, 1000);

/* ======================================================= library and folders */
let allMeta = [], library = [], searchQ = "", folderView = null; // null = all
let selecting = false; const selected = new Set();
async function loadLibrary() {
  try { allMeta = await req("GET", "/api/library"); } catch { allMeta = []; }
  if (searchQ) { try { library = await req("GET", "/api/library?q=" + encodeURIComponent(searchQ)); } catch { library = []; } }
  else library = allMeta;
  renderFolders(); renderLibrary(); renderFolderOption();
}
let searchTimer;
$("search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { searchQ = $("search").value.trim(); loadLibrary(); }, 200); };
function folderNames() {
  const set = new Set(prefs.folders || []);
  for (const m of allMeta) if (m.folder) set.add(m.folder);
  return [...set].sort((a, b) => a.localeCompare(b));
}
function renderFolders() {
  const box = $("folders");
  const names = folderNames();
  const count = (f) => allMeta.filter((m) => (m.folder || "") === f).length;
  box.innerHTML = "";
  const mk = (label, icon, n, on, fn, extra) => {
    const b = document.createElement("button");
    b.className = "folder" + (on ? " on" : "") + (extra || "");
    b.innerHTML = `${icon}<span>${esc(label)}</span>${n != null ? `<span class="c">${n}</span>` : ""}`;
    b.onclick = fn;
    box.append(b);
    return b;
  };
  mk("All transcripts", ICON.all, allMeta.length, folderView === null, () => { folderView = null; renderFolders(); renderLibrary(); });
  for (const f of names) {
    const b = mk(f, ICON.folder, count(f), folderView === f, () => { folderView = f; renderFolders(); renderLibrary(); });
    b.oncontextmenu = (e) => { e.preventDefault(); folderMenu(b, f); };
    b.title = "Right-click to rename or delete";
  }
  mk("New folder", '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>', null, false, newFolder, " add");
}
async function newFolder(thenMove) {
  const name = await askText("New folder", "Folder name", "", "Create folder");
  if (!name) return null;
  if (!folderNames().includes(name)) setPref("folders", [...(prefs.folders || []), name]);
  renderFolders(); renderFolderOption();
  if (!thenMove) { folderView = name; renderFolders(); renderLibrary(); }
  return name;
}
function folderMenu(anchor, f) {
  openMenu(anchor, [
    { label: "Rename folder", fn: async () => {
      const name = await askText("Rename folder", "Folder name", f, "Rename");
      if (!name || name === f) return;
      await req("POST", "/api/folders/rename", { from: f, to: name }).catch((e) => toast(e.message));
      setPref("folders", [...(prefs.folders || []).filter((x) => x !== f), name]);
      if (folderView === f) folderView = name;
      if (doc && doc.folder === f) doc.folder = name;
      loadLibrary();
    } },
    { label: "Delete folder", danger: true, fn: async () => {
      if (!(await confirmBox(`Delete the folder "${f}"?`, "The transcripts in it stay in All transcripts.", "Delete folder"))) return;
      await req("POST", "/api/folders/rename", { from: f, to: "" }).catch((e) => toast(e.message));
      setPref("folders", (prefs.folders || []).filter((x) => x !== f));
      if (folderView === f) folderView = null;
      if (doc && doc.folder === f) doc.folder = "";
      loadLibrary();
    } },
  ]);
}
function highlightSnippet(s, q) {
  const i = s.toLowerCase().indexOf(q.toLowerCase());
  return i < 0 ? esc(s) : esc(s.slice(0, i)) + "<b>" + esc(s.slice(i, i + q.length)) + "</b>" + esc(s.slice(i + q.length));
}
function visibleItems() { return library.filter((m) => folderView === null || (m.folder || "") === folderView); }
function renderLibrary() {
  const box = $("lib");
  box.innerHTML = "";
  $("libHead").textContent = searchQ ? "Search results" : folderView === null ? "All transcripts" : folderView;
  $("selectBtn").textContent = selecting ? "Cancel" : "Select";
  const items = visibleItems();
  if (!items.length) {
    box.innerHTML = `<div class="empty-side">${searchQ ? "No transcripts match that search." : folderView ? "This folder is empty. Move transcripts here from their More menu." : "Finished transcripts appear here."}</div>`;
  }
  for (const m of items) {
    const b = document.createElement("button");
    b.className = "item" + (!selecting && doc && doc.id === m.id ? " on" : "");
    const sub = [when(m.created), m.duration ? clock(m.duration) : null].filter(Boolean).join(", ");
    b.innerHTML = (selecting ? `<input type="checkbox" tabindex="-1" ${selected.has(m.id) ? "checked" : ""}>` : "") +
      `<div><div class="item-title">${esc(m.title)}</div><div class="item-sub">${esc(sub)}</div>` +
      (m.snippet ? `<div class="item-snip">${highlightSnippet(m.snippet, searchQ)}</div>` : "") + `</div>`;
    b.onclick = () => {
      if (selecting) { selected.has(m.id) ? selected.delete(m.id) : selected.add(m.id); renderLibrary(); }
      else openDoc(m.id);
    };
    box.append(b);
  }
  $("selbar").hidden = !selecting;
  $("selCount").textContent = `${selected.size} selected`;
  for (const id of ["selExport", "selMove", "selDelete"]) $(id).disabled = !selected.size;
}
$("selectBtn").onclick = () => { selecting = !selecting; selected.clear(); renderLibrary(); };
$("selDone").onclick = () => { selecting = false; selected.clear(); renderLibrary(); };
$("selDelete").onclick = async () => {
  const n = selected.size;
  if (!(await confirmBox(`Delete ${n} ${n === 1 ? "transcript" : "transcripts"}?`, "They and their saved recordings will be removed from this computer. This can't be undone.", "Delete"))) return;
  if (doc && selected.has(doc.id)) { clearTimeout(saveTimer); dirty = false; pauseMedia(); media.removeAttribute("src"); doc = null; show("new"); }
  await req("POST", "/api/bulk/delete", { ids: [...selected] }).catch((e) => toast(e.message));
  toast(`Deleted ${n} ${n === 1 ? "transcript" : "transcripts"}`);
  selected.clear(); selecting = false; loadLibrary();
};
$("selMove").onclick = () => moveMenu($("selMove"), async (folder) => {
  await req("POST", "/api/bulk/move", { ids: [...selected], folder }).catch((e) => toast(e.message));
  if (doc && selected.has(doc.id)) doc.folder = folder;
  toast(folder ? `Moved to ${folder}` : "Removed from folder");
  selected.clear(); selecting = false; loadLibrary();
});
$("selExport").onclick = () => {
  openMenu($("selExport"), FORMATS.map(([f, label, ext]) => ({ label, hint: ext, fn: () => exportMany([...selected], f) })),
    exportChecks(false));
  bindExportChecks();
};
function moveMenu(anchor, fn) {
  const items = folderNames().map((f) => ({ label: f, fn: () => fn(f) }));
  items.push({ sep: true }, { label: "New folder…", fn: async () => { const f = await newFolder(true); if (f) fn(f); } });
  items.push({ label: "No folder", fn: () => fn("") });
  openMenu(anchor, items);
}

/* ======================================================= transcript */
let doc = null;
const media = $("media");
const segsEl = $("segs");
let tab = "transcript";

async function openDoc(id, preloaded) {
  if (rec) return toast("Stop or discard the recording first.");
  await flushSave();
  pauseMedia();
  let d = preloaded;
  if (!d) { try { d = await req("GET", "/api/library/" + id); } catch (e) { toast(e.message); loadLibrary(); return; } }
  d.bookmarks ||= []; d.writeups ||= []; d.speakers ||= {};
  doc = d;
  history.length = 0; future.length = 0; lastKind = null; updateUndo();
  show("doc");
  $("title").value = doc.title;
  renderMeta();
  $("summary").hidden = !doc.summary;
  $("summaryText").textContent = doc.summary || "";
  renderSpeakers();
  renderSegments();
  $("saveState").textContent = "";
  closeFind();
  setTab("transcript");
  activeIdx = -1; activeWord = null; videoHidden = false; fixingDuration = false;
  $("videoBox").hidden = true; $("showVideo").hidden = true;
  if (doc.media) {
    $("player").hidden = false; $("noPlayer").hidden = true;
    media.src = "/media/" + doc.id;
    media.playbackRate = +prefs.rate;
  } else {
    media.removeAttribute("src"); media.load();
    $("player").hidden = true; $("noPlayer").hidden = false;
    $("noMediaMsg").textContent = "The recording wasn't kept for this transcript, so playback isn't available.";
  }
  renderMarks();
  $("docScroll").scrollTop = 0;
  renderLibrary();
}
function renderMeta() {
  const words = doc.segments.reduce((n, s) => n + (s.text.trim() ? s.text.trim().split(/\s+/).length : 0), 0);
  const bits = [when(doc.created)];
  if (doc.duration) bits.push(clock(doc.duration) + " long");
  bits.push(words.toLocaleString() + " words");
  if (doc.language) bits.push(LANGS[doc.language] || doc.language.toUpperCase());
  if (doc.folder) bits.push("In " + doc.folder);
  if (doc.cost != null) bits.push("Cost " + money(doc.cost));
  $("meta").innerHTML = bits.map((b) => `<span>${esc(b)}</span>`).join("");
  $("nWriteups").textContent = doc.writeups.length ? doc.writeups.length : "";
  $("nBookmarks").textContent = doc.bookmarks.length ? doc.bookmarks.length : "";
}
function setTab(t) {
  tab = t;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $("paneTranscript").hidden = t !== "transcript";
  $("paneWriteups").hidden = t !== "writeups";
  $("paneBookmarks").hidden = t !== "bookmarks";
  if (t !== "transcript") closeFind();
  if (t === "writeups") renderWriteups();
  if (t === "bookmarks") renderBookmarks();
}
document.querySelectorAll(".tab").forEach((b) => b.onclick = () => setTab(b.dataset.tab));

function speakerLabel(sp) { return sp == null ? null : (doc.speakers[String(sp)] || `Speaker ${+sp + 1}`); }
function renderSpeakers() {
  const keys = Object.keys(doc.speakers || {});
  const box = $("speakers");
  box.hidden = !keys.length;
  box.innerHTML = keys.length ? `<span class="lbl">Speakers</span>` : "";
  for (const k of keys) {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.textContent = doc.speakers[k]; chip.title = "Rename";
    chip.onclick = () => {
      chip.innerHTML = `<input value="${esc(doc.speakers[k])}" aria-label="Speaker name">`;
      const inp = chip.querySelector("input"); inp.focus(); inp.select();
      let finished = false;
      const done = (commit) => {
        if (finished) return; finished = true;
        const v = inp.value.trim();
        if (commit && v && v !== doc.speakers[k]) { pushHistory("struct"); doc.speakers[k] = v; scheduleSave(); }
        renderSpeakers(); refreshSpeakerButtons();
      };
      inp.onkeydown = (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); e.stopPropagation(); };
      inp.onblur = () => done(true);
      chip.onclick = null;
    };
    box.append(chip);
  }
}
function wordsHtml(seg) {
  return seg.words.map((w) => `<span class="w${w.c < 0.6 ? " low" : ""}" data-s="${w.s}" data-e="${w.e}">${esc(w.w)}</span>`).join(" ");
}
function segNode(seg) {
  const el = document.createElement("div");
  el.className = "seg"; el.dataset.id = seg.id;
  el.innerHTML = `<div class="gutter"><button class="ts" title="Play from here">${clock(seg.start)}</button></div>
    <div><button class="spk"></button><div class="txt" contenteditable="plaintext-only" spellcheck="true"></div></div>`;
  const txt = el.querySelector(".txt");
  if (seg.words && seg.words.length && !seg.edited) txt.innerHTML = wordsHtml(seg);
  else txt.textContent = seg.text;
  return el;
}
function renderSegments() {
  segsEl.className = prefs.unsure ? "show-unsure" : "";
  segsEl.innerHTML = "";
  if (!doc.segments.length) { segsEl.innerHTML = `<div class="doc-empty">No speech was found in this recording.</div>`; return; }
  const frag = document.createDocumentFragment();
  for (const s of doc.segments) frag.append(segNode(s));
  segsEl.append(frag);
  refreshSpeakerButtons(); refreshFlags();
}
function refreshSpeakerButtons() {
  let last;
  const nodes = segsEl.querySelectorAll(".seg");
  doc.segments.forEach((s, i) => {
    const b = nodes[i]?.querySelector(".spk"); if (!b) return;
    const name = speakerLabel(s.speaker);
    b.textContent = name || "Add speaker";
    b.className = "spk" + (name == null ? " none" : name === last ? " same" : "");
    last = name;
  });
}
const segIndex = (id) => doc.segments.findIndex((s) => s.id === id);
const nodeOf = (id) => segsEl.querySelector(`.seg[data-id="${id}"]`);
const joinWords = (seg) => (seg.words || []).map((w) => w.w).join(" ");

// ---- typing
segsEl.addEventListener("beforeinput", (e) => {
  if (!e.target.closest?.(".txt")) return;
  if (e.inputType === "historyUndo" || e.inputType === "historyRedo") { e.preventDefault(); e.inputType === "historyUndo" ? undo() : redo(); return; }
  pushHistory("type");
});
segsEl.addEventListener("input", (e) => {
  const txt = e.target.closest(".txt"); if (!txt) return;
  const seg = doc.segments[segIndex(txt.closest(".seg").dataset.id)];
  seg.text = txt.textContent.replace(/\s+/g, " ").trim();
  if (!seg.edited && seg.text !== joinWords(seg)) seg.edited = true;
  scheduleSave();
});
segsEl.addEventListener("paste", (e) => {
  if (!e.target.closest(".txt")) return;
  e.preventDefault();
  document.execCommand("insertText", false, e.clipboardData.getData("text/plain").replace(/\s+/g, " "));
});
segsEl.addEventListener("keydown", (e) => {
  const txt = e.target.closest(".txt"); if (!txt) return;
  const id = txt.closest(".seg").dataset.id;
  if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) { e.preventDefault(); splitAt(id, caretOffset(txt)); }
  else if (e.key === "Enter") e.preventDefault();
  else if (e.key === "Backspace" && caretOffset(txt) === 0 && getSelection().isCollapsed && segIndex(id) > 0) { e.preventDefault(); mergeUp(id); }
});
segsEl.addEventListener("click", (e) => {
  const ts = e.target.closest(".ts");
  if (ts) { const s = doc.segments[segIndex(ts.closest(".seg").dataset.id)]; seek(s.start, true); return; }
  const flag = e.target.closest(".bm-flag");
  if (flag) { setTab("bookmarks"); return; }
  const spk = e.target.closest(".spk");
  if (spk) { speakerMenu(spk, spk.closest(".seg").dataset.id); return; }
  const w = e.target.closest(".w");
  if (w && e.altKey) { e.preventDefault(); seek(+w.dataset.s, true); }
});

function caretOffset(el) {
  const sel = getSelection(); if (!sel.rangeCount) return 0;
  const r = sel.getRangeAt(0), pre = document.createRange();
  pre.selectNodeContents(el); pre.setEnd(r.startContainer, r.startOffset);
  return pre.toString().length;
}
function selectText(el, start, end = start) {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let pos = 0, node, setStart = false;
  const r = document.createRange();
  while ((node = walker.nextNode())) {
    const len = node.textContent.length;
    if (!setStart && start <= pos + len) { r.setStart(node, start - pos); setStart = true; }
    if (setStart && end <= pos + len) { r.setEnd(node, end - pos); break; }
    pos += len;
  }
  if (!setStart) { r.selectNodeContents(el); r.collapse(false); }
  el.focus();
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
}

function splitAt(id, off) {
  const i = segIndex(id), seg = doc.segments[i];
  const text = nodeOf(id).querySelector(".txt").textContent;
  let a, b;
  if (seg.words && seg.words.length && !seg.edited) {
    let pos = 0, k = seg.words.length;
    for (let j = 0; j < seg.words.length; j++) { if (pos >= off) { k = j; break; } pos += seg.words[j].w.length + 1; }
    if (k <= 0 || k >= seg.words.length) return;
    const wa = seg.words.slice(0, k), wb = seg.words.slice(k);
    a = { ...seg, words: wa, text: wa.map((w) => w.w).join(" "), end: wa[wa.length - 1].e };
    b = { ...seg, id: newId(), words: wb, text: wb.map((w) => w.w).join(" "), start: wb[0].s };
  } else {
    const ta = text.slice(0, off).trim(), tb = text.slice(off).trim();
    if (!ta || !tb) return;
    const t = seg.start + (seg.end - seg.start) * (off / Math.max(text.length, 1));
    const ws = seg.words || [];
    a = { ...seg, text: ta, end: t, words: ws.filter((w) => w.s < t), edited: true };
    b = { ...seg, id: newId(), text: tb, start: t, words: ws.filter((w) => w.s >= t), edited: true };
  }
  pushHistory("struct");
  doc.segments.splice(i, 1, a, b);
  const old = nodeOf(id), na = segNode(a), nb = segNode(b);
  old.replaceWith(na); na.after(nb);
  afterStructure();
  selectText(nb.querySelector(".txt"), 0);
}
function mergeUp(id) {
  const i = segIndex(id), seg = doc.segments[i], prev = doc.segments[i - 1];
  pushHistory("struct");
  const joinAt = prev.text.length + (prev.text && seg.text ? 1 : 0);
  const clean = !prev.edited && !seg.edited && prev.words?.length && seg.words?.length;
  const merged = { ...prev, text: [prev.text, seg.text].filter(Boolean).join(" "), end: seg.end,
    words: [...(prev.words || []), ...(seg.words || [])], edited: !clean };
  doc.segments.splice(i - 1, 2, merged);
  const n = segNode(merged);
  nodeOf(prev.id).replaceWith(n); nodeOf(id).remove();
  afterStructure();
  selectText(n.querySelector(".txt"), joinAt);
}
function afterStructure() { refreshSpeakerButtons(); refreshFlags(); renderMeta(); scheduleSave(); resetHighlight(); }

// ---- undo / redo (covers typing, split, join, replace and speakers)
const history = [], future = [];
let lastKind = null, lastAt = 0;
const snapshot = () => ({ segments: doc.segments.map((s) => ({ ...s })), speakers: { ...doc.speakers } });
function pushHistory(kind) {
  const now = Date.now();
  if (kind === "type" && lastKind === "type" && now - lastAt < 1200) { lastAt = now; return; }
  history.push(snapshot());
  if (history.length > 80) history.shift();
  future.length = 0; lastKind = kind; lastAt = now;
  updateUndo();
}
function restore(snap) {
  const focused = document.activeElement?.closest?.(".seg")?.dataset.id;
  const off = focused ? caretOffset(document.activeElement) : 0;
  doc.segments = snap.segments.map((s) => ({ ...s }));
  doc.speakers = { ...snap.speakers };
  renderSpeakers(); renderSegments(); renderMeta(); resetHighlight(); scheduleSave();
  const n = focused && nodeOf(focused);
  if (n) { const t = n.querySelector(".txt"); selectText(t, Math.min(off, t.textContent.length)); }
}
function undo() { if (!doc || !history.length) return; future.push(snapshot()); restore(history.pop()); lastKind = null; updateUndo(); }
function redo() { if (!doc || !future.length) return; history.push(snapshot()); restore(future.pop()); lastKind = null; updateUndo(); }
function updateUndo() { $("undoBtn").disabled = !history.length; $("redoBtn").disabled = !future.length; }
$("undoBtn").onclick = undo;
$("redoBtn").onclick = redo;

// ---- speakers
function speakerMenu(anchor, segId) {
  const seg = doc.segments[segIndex(segId)];
  const keys = Object.keys(doc.speakers);
  const items = keys.map((k) => ({ label: doc.speakers[k], check: String(seg.speaker) === k, fn: () => setSpeaker(seg, +k) }));
  if (keys.length) items.push({ sep: true });
  items.push({ label: "New speaker", fn: () => {
    pushHistory("struct");
    const n = keys.length ? Math.max(...keys.map(Number)) + 1 : 0;
    doc.speakers[String(n)] = `Speaker ${n + 1}`; seg.speaker = n;
    renderSpeakers(); refreshSpeakerButtons(); scheduleSave();
  } });
  if (seg.speaker != null) items.push({ label: "No speaker", fn: () => setSpeaker(seg, null) });
  if (keys.length && seg.speaker != null) items.push({ label: "Use this speaker for all paragraphs below", fn: () => {
    pushHistory("struct");
    const i = segIndex(segId); for (let j = i + 1; j < doc.segments.length; j++) doc.segments[j].speaker = seg.speaker;
    refreshSpeakerButtons(); scheduleSave();
  } });
  openMenu(anchor, items);
}
function setSpeaker(seg, sp) { pushHistory("struct"); seg.speaker = sp; refreshSpeakerButtons(); scheduleSave(); }

/* ======================================================= saving */
let saveTimer = null, dirty = false, saving = null, dirtyTold = false;
$("title").oninput = () => {
  doc.title = $("title").value;
  const m = allMeta.find((x) => x.id === doc.id); if (m) { m.title = doc.title || "Untitled"; renderLibrary(); }
  scheduleSave();
};
$("title").onkeydown = (e) => { if (e.key === "Enter") e.target.blur(); };
function scheduleSave() {
  if (!doc) return;
  dirty = true; $("saveState").className = "save-state"; $("saveState").textContent = "Editing";
  if (!dirtyTold) { dirtyTold = true; fetch("/api/dirty", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: doc.id }) }).catch(() => {}); }
  clearTimeout(saveTimer); saveTimer = setTimeout(flushSave, 350);
}
async function flushSave() {
  clearTimeout(saveTimer);
  if (saving) await saving;
  if (!dirty || !doc) return;
  dirty = false; dirtyTold = false;
  const d = doc;
  $("saveState").textContent = "Saving";
  saving = req("PUT", "/api/library/" + d.id, { title: d.title, speakers: d.speakers, segments: d.segments,
      bookmarks: d.bookmarks, writeups: d.writeups, folder: d.folder || "" })
    .then(() => { if (doc === d) { $("saveState").textContent = "Saved"; renderMeta(); } })
    .catch(() => { dirty = true; if (doc === d) { $("saveState").className = "save-state err"; $("saveState").textContent = "Couldn't save"; } })
    .finally(() => { saving = null; });
  await saving;
}
window.addEventListener("pagehide", () => {
  if (!dirty || !doc) return;
  const d = doc;
  navigator.sendBeacon("/api/library/" + d.id + "/save", new Blob([JSON.stringify({ title: d.title, speakers: d.speakers,
    segments: d.segments, bookmarks: d.bookmarks, writeups: d.writeups, folder: d.folder || "" })], { type: "application/json" }));
});

/* ======================================================= find and replace */
let matches = [], mi = -1;
function openFind(replace) {
  if (tab !== "transcript") setTab("transcript");
  $("findbar").hidden = false;
  const sel = getSelection().toString().trim();
  if (sel && sel.length < 80) $("findIn").value = sel;
  (replace ? $("replIn") : $("findIn")).focus(); $("findIn").select();
  computeMatches();
}
function closeFind() { $("findbar").hidden = true; matches = []; mi = -1; }
$("findBtn").onclick = () => $("findbar").hidden ? openFind() : closeFind();
$("findClose").onclick = closeFind;
function computeMatches() {
  const q = $("findIn").value, cs = $("matchCase").checked;
  matches = [];
  if (q && doc) doc.segments.forEach((s, si) => {
    const hay = cs ? s.text : s.text.toLowerCase(), needle = cs ? q : q.toLowerCase();
    let i = hay.indexOf(needle);
    while (i >= 0) { matches.push({ si, off: i }); i = hay.indexOf(needle, i + needle.length); }
  });
  mi = matches.length ? Math.min(Math.max(mi, 0), matches.length - 1) : -1;
  $("findCount").textContent = q ? (matches.length ? `${mi + 1} of ${matches.length}` : "No results") : "";
}
function gotoMatch(dir) { computeMatches(); if (!matches.length) return; mi = (mi + dir + matches.length) % matches.length; showMatch(); }
function showMatch() {
  const m = matches[mi]; if (!m) return;
  $("findCount").textContent = `${mi + 1} of ${matches.length}`;
  const seg = doc.segments[m.si];
  const txt = nodeOf(seg.id).querySelector(".txt");
  const dom = txt.textContent, q = $("findIn").value, cs = $("matchCase").checked;
  let count = 0;
  for (let k = 0; k < mi; k++) if (matches[k].si === m.si) count++;
  const h = cs ? dom : dom.toLowerCase(), n = cs ? q : q.toLowerCase();
  let idx = h.indexOf(n); for (let c = 0; c < count && idx >= 0; c++) idx = h.indexOf(n, idx + n.length);
  if (idx < 0) return;
  txt.closest(".seg").scrollIntoView({ block: "center" });
  selectText(txt, idx, idx + q.length);
  $("findIn").focus({ preventScroll: true });
}
$("findIn").oninput = () => { mi = 0; computeMatches(); if (matches.length) showMatch(); };
$("matchCase").onchange = () => { mi = 0; computeMatches(); };
$("findIn").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); gotoMatch(e.shiftKey ? -1 : 1); } };
$("replIn").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); replaceOne(); } };
$("findNext").onclick = () => gotoMatch(1);
$("findPrev").onclick = () => gotoMatch(-1);
function replaceInSeg(seg, off, len, rep) {
  seg.text = (seg.text.slice(0, off) + rep + seg.text.slice(off + len)).replace(/\s+/g, " ").trim();
  seg.edited = true;
  nodeOf(seg.id).replaceWith(segNode(seg));
}
function replaceOne() {
  computeMatches(); if (!matches.length) return;
  pushHistory("struct");
  const m = matches[Math.max(mi, 0)];
  replaceInSeg(doc.segments[m.si], m.off, $("findIn").value.length, $("replIn").value);
  afterStructure();
  computeMatches(); if (matches.length) { mi = Math.min(mi, matches.length - 1); showMatch(); }
}
function replaceAll() {
  const q = $("findIn").value; if (!q) return;
  computeMatches();
  const n = matches.length; if (!n) return;
  pushHistory("struct");
  for (let k = matches.length - 1; k >= 0; k--) { const m = matches[k]; replaceInSeg(doc.segments[m.si], m.off, q.length, $("replIn").value); }
  afterStructure(); computeMatches();
  toast(`Replaced ${n} ${n === 1 ? "match" : "matches"}`, "Undo", undo, 7000);
}
$("replOne").onclick = replaceOne;
$("replAll").onclick = replaceAll;

/* ======================================================= bookmarks */
function addBookmark() {
  if (!doc) return;
  let t = 0;
  if (doc.media && !isNaN(media.currentTime) && (media.currentTime > 0 || !media.paused)) t = media.currentTime;
  else {
    const segId = document.activeElement?.closest?.(".seg")?.dataset.id;
    if (segId) t = doc.segments[segIndex(segId)].start;
  }
  doc.bookmarks.push({ id: newId(), t: Math.round(t * 100) / 100, note: "" });
  doc.bookmarks.sort((a, b) => a.t - b.t);
  refreshFlags(); renderMarks(); renderMeta(); scheduleSave();
  toast(`Bookmark added at ${clock(t)}`, "Add a note", () => { setTab("bookmarks"); focusBookmarkAt(t); });
  if (tab === "bookmarks") renderBookmarks();
}
function focusBookmarkAt(t) {
  const b = doc.bookmarks.find((x) => x.t === Math.round(t * 100) / 100);
  if (b) $("paneBookmarks").querySelector(`[data-bm="${b.id}"] input`)?.focus();
}
$("markBtn").onclick = addBookmark;
function textAt(t) {
  const s = [...doc.segments].reverse().find((x) => x.start <= t + 0.01) || doc.segments[0];
  if (!s) return "";
  if (s.words?.length && !s.edited) {
    const i = Math.max(0, s.words.findIndex((w) => w.e > t));
    return s.words.slice(i, i + 24).map((w) => w.w).join(" ");
  }
  return s.text.slice(0, 160);
}
function renderBookmarks() {
  const pane = $("paneBookmarks");
  if (!doc.bookmarks.length) {
    pane.innerHTML = `<div class="empty-pane"><h3>No bookmarks yet</h3><p>Press <kbd>Ctrl+B</kbd> or the bookmark button while listening to mark a moment. Bookmarks show as amber ticks on the timeline.</p></div>`;
    return;
  }
  pane.innerHTML = `<div class="pane-head"><p>Moments you've marked. Click a time to play from there, or export the part between two bookmarks as a clip.</p></div>`;
  doc.bookmarks.forEach((b, i) => {
    const el = document.createElement("div");
    el.className = "bm"; el.dataset.bm = b.id;
    el.innerHTML = `<button class="ts" title="Play from here">${clock(b.t)}</button>
      <input type="text" placeholder="Add a note" value="${esc(b.note)}" aria-label="Bookmark note">
      <div class="bm-actions">${i < doc.bookmarks.length - 1 ? `<button class="icon-btn" data-a="clip" title="Export from here to the next bookmark">${ICON.copy}</button>` : ""}
      <button class="icon-btn" data-a="del" title="Delete bookmark">${ICON.trash}</button></div>
      <div class="ctx">${esc(textAt(b.t))}</div>`;
    el.querySelector(".ts").onclick = () => seek(b.t, true);
    const inp = el.querySelector("input");
    inp.oninput = () => { b.note = inp.value; scheduleSave(); };
    inp.onkeydown = (e) => { if (e.key === "Enter") inp.blur(); };
    el.querySelector('[data-a="del"]').onclick = () => {
      doc.bookmarks = doc.bookmarks.filter((x) => x !== b);
      refreshFlags(); renderMarks(); renderMeta(); renderBookmarks(); scheduleSave();
    };
    el.querySelector('[data-a="clip"]')?.addEventListener("click", () => clipDialog(b.t, doc.bookmarks[i + 1].t));
    pane.append(el);
  });
}
function refreshFlags() {
  if (!doc) return;
  segsEl.querySelectorAll(".bm-flag").forEach((f) => f.remove());
  const segs = doc.segments;
  for (const b of doc.bookmarks) {
    let idx = -1;
    for (let i = 0; i < segs.length; i++) { if (segs[i].start <= b.t + 0.01) idx = i; else break; }
    const n = idx >= 0 && nodeOf(segs[idx].id);
    if (n && !n.querySelector(".bm-flag")) n.querySelector(".gutter").insertAdjacentHTML("beforeend", `<button class="bm-flag" title="Bookmarked">${ICON.flag}</button>`);
  }
}
function renderMarks() {
  const box = $("marks");
  const dur = media.duration && isFinite(media.duration) ? media.duration : doc?.duration;
  box.innerHTML = !doc || !dur ? "" : doc.bookmarks.map((b) => `<i style="left:${Math.min(100, b.t / dur * 100)}%"></i>`).join("");
}

/* ======================================================= write-ups (Gemini) */
const KINDS = [["summary", "Summary and key points"], ["chapters", "Chapters"], ["sermon", "Sermon notes"],
  ["minutes", "Meeting minutes"], ["actions", "Action items and decisions"]];
let writing = null;
function renderWriteups() {
  const pane = $("paneWriteups");
  if (!status.hasGemini) {
    pane.innerHTML = `<div class="empty-pane"><h3>Write summaries, chapters and notes</h3>
      <p>Write-ups use Google Gemini with your own API key. You can get a free key at aistudio.google.com under "Get API key".</p>
      <button class="btn primary" id="wuKey">Add Gemini key</button></div><div id="wuList" style="margin-top:18px"></div>`;
    $("wuKey").onclick = () => openSettings("gemini");
    if (!doc.writeups.length) return;
  } else {
    pane.innerHTML = `<div class="pane-head"><p>Written by Gemini from this transcript. Check names and quotes before you share them.</p>
      <button class="btn primary" id="wuNew">New write-up</button></div><div id="wuList"></div>`;
    $("wuNew").onclick = () => writeupMenu($("wuNew"));
  }
  const list = $("wuList");
  if (writing && writing.docId === doc.id) {
    list.insertAdjacentHTML("beforeend", `<div class="wu pending"><div class="wu-head"><h3>${esc(writing.title)}</h3></div>
      <div class="wu-body">Writing… This takes a few seconds for short recordings and up to a minute for long ones.<div class="bar indet"><i></i></div></div></div>`);
  }
  if (!doc.writeups.length && !(writing && writing.docId === doc.id)) {
    list.innerHTML = `<div class="empty-pane"><h3>No write-ups yet</h3><p>Choose New write-up to make a summary, chapters with times, sermon notes, meeting minutes or your own request.</p></div>`;
  }
  for (const w of doc.writeups) {
    const el = document.createElement("div");
    el.className = "wu";
    el.innerHTML = `<div class="wu-head"><h3>${esc(w.title)}</h3><small>${esc(when(w.created))}</small>
      <button class="icon-btn" data-a="copy" title="Copy">${ICON.copy}</button>
      <button class="icon-btn" data-a="edit" title="Edit">${ICON.edit}</button>
      <button class="icon-btn" data-a="del" title="Delete">${ICON.trash}</button></div>
      <div class="wu-body">${renderMd(w.text)}</div>`;
    el.querySelector('[data-a="copy"]').onclick = async () => { try { await navigator.clipboard.writeText(w.text.replace(/\*\*/g, "")); toast("Copied"); } catch { toast("Couldn't copy."); } };
    el.querySelector('[data-a="del"]').onclick = async () => {
      if (!(await confirmBox(`Delete "${w.title}"?`, "This write-up will be removed. The transcript stays.", "Delete"))) return;
      doc.writeups = doc.writeups.filter((x) => x !== w); renderMeta(); renderWriteups(); scheduleSave();
    };
    el.querySelector('[data-a="edit"]').onclick = () => {
      const body = el.querySelector(".wu-body");
      body.innerHTML = `<textarea aria-label="Edit write-up">${esc(w.text)}</textarea><div class="row" style="display:flex;gap:8px;margin-top:8px"><button class="btn sm primary">Save</button><button class="btn sm">Cancel</button></div>`;
      const [save, cancel] = body.querySelectorAll("button");
      save.onclick = () => { w.text = body.querySelector("textarea").value; scheduleSave(); renderWriteups(); };
      cancel.onclick = renderWriteups;
      body.querySelector("textarea").focus();
    };
    el.querySelector(".wu-body").addEventListener("click", (e) => { const b = e.target.closest(".tsl"); if (b) seek(+b.dataset.t, true); });
    list.append(el);
  }
}
function writeupMenu(anchor) {
  const items = KINDS.map(([k, label]) => ({ label, fn: () => makeWriteup(k, label) }));
  items.push({ label: "Your own request…", fn: customWriteup });
  openMenu(anchor, items, `<div class="sep"></div><label><input type="checkbox" id="wuEn" ${prefs.writeEnglish ? "checked" : ""}> Write in English</label>`, true);
  menuEl.querySelector("#wuEn").onchange = (e) => setPref("writeEnglish", e.target.checked);
}
function customWriteup() {
  dialog(`<h2>Your own request</h2><div class="field"><label for="cwIn">What should Gemini write from this transcript?</label>
    <textarea id="cwIn" placeholder="For example: A 150-word newsletter paragraph about this sermon"></textarea></div>
    <div class="end"><button class="btn" data-close>Cancel</button><button class="btn primary" id="cwGo">Write</button></div>`, (d) => {
    d.querySelector("#cwGo").onclick = () => { const v = d.querySelector("#cwIn").value.trim(); if (!v) return; closeDialog(); makeWriteup("custom", v.split("\n")[0].slice(0, 60), v); };
  });
}
async function makeWriteup(kind, title, custom) {
  if (writing) return toast("A write-up is already being written. Wait for it to finish.");
  await flushSave();
  const d = doc;
  writing = { docId: d.id, title };
  if (doc === d) { setTab("writeups"); }
  try {
    const wu = await req("POST", "/api/writeup", { id: d.id, kind, custom: custom || "", english: prefs.writeEnglish });
    if (doc === d) { doc.writeups.unshift(wu); renderMeta(); }
    else toast(`"${title}" is ready in "${d.title}"`);
  } catch (e) { toast(e.message, null, null, 7000); }
  writing = null;
  if (doc === d && tab === "writeups") renderWriteups();
}
function renderMd(text) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, (m, t) => `<button class="tsl" data-t="${parseClock(t)}" title="Play from here">${t}</button>`);
  let html = "", list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { close(); continue; }
    if ((m = line.match(/^\s*#{1,6}\s+(.*)/))) { close(); html += `<h4>${inline(m[1])}</h4>`; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; continue; }
    close(); html += `<p>${inline(line)}</p>`;
  }
  close();
  return html;
}

/* ======================================================= export */
const FORMATS = [["docx", "Word document", ".docx"], ["pdf", "PDF", ".pdf"], ["txt", "Plain text", ".txt"],
  ["srt", "Subtitles", ".srt"], ["vtt", "Web subtitles", ".vtt"], ["md", "Markdown", ".md"], ["json", "Data", ".json"]];
function exportChecks(withWriteups) {
  return `<div class="sep"></div>
    <label><input type="checkbox" data-p="stamps" ${prefs.stamps ? "checked" : ""}> Include timestamps</label>
    <label><input type="checkbox" data-p="speakersOut" ${prefs.speakersOut ? "checked" : ""}> Include speaker names</label>
    ${withWriteups ? `<label><input type="checkbox" data-p="writeupsOut" ${prefs.writeupsOut ? "checked" : ""}> Include write-ups</label>` : ""}`;
}
function bindExportChecks() { menuEl.querySelectorAll("[data-p]").forEach((c) => c.onchange = () => setPref(c.dataset.p, c.checked)); }
function exportOpts(extra) {
  return { stamps: prefs.stamps ? 1 : 0, speakers: prefs.speakersOut ? 1 : 0, writeups: prefs.writeupsOut ? 1 : 0, ...extra };
}
function selectionRange() {
  const sel = getSelection();
  if (!sel.rangeCount || sel.isCollapsed || !segsEl.contains(sel.anchorNode)) return null;
  const r = sel.getRangeAt(0);
  const words = [...segsEl.querySelectorAll(".w")].filter((w) => r.intersectsNode(w));
  if (words.length) return { start: +words[0].dataset.s, end: +words[words.length - 1].dataset.e };
  const segs = [...segsEl.querySelectorAll(".seg")].filter((n) => r.intersectsNode(n)).map((n) => doc.segments[segIndex(n.dataset.id)]);
  return segs.length ? { start: segs[0].start, end: segs[segs.length - 1].end } : null;
}
$("exportBtn").onclick = () => {
  const range = tab === "transcript" ? selectionRange() : null;
  const items = [];
  if (range) items.push({ label: "Export the selected text…", hint: `${clock(range.start)}–${clock(range.end)}`, fn: () => clipDialog(range.start, range.end) }, { sep: true });
  items.push(...FORMATS.map(([f, label, ext]) => ({ label, hint: ext, fn: () => exportAs(f) })));
  items.push({ sep: true }, { label: "Export a clip…", fn: () => clipDialog() });
  openMenu($("exportBtn"), items, exportChecks(doc.writeups.length > 0), true);
  bindExportChecks();
};
async function exportAs(fmt, extra) {
  await flushSave();
  const opts = exportOpts(extra);
  const api = desktop();
  if (api?.export) {
    const res = await api.export(doc.id, fmt, opts);
    if (res?.error) toast(res.error);
    else if (res?.saved) toast("Exported " + res.path.split(/[\\/]/).pop(), "Show in folder", () => api.reveal(res.path), 6000);
    return;
  }
  const qs = new URLSearchParams({ fmt, ...Object.fromEntries(Object.entries(opts).filter(([, v]) => v != null)) });
  download(`/api/export/${doc.id}?${qs}`);
}
async function exportMany(ids, fmt) {
  const opts = exportOpts();
  const api = desktop();
  if (api?.export_many) {
    const res = await api.export_many(ids, fmt, opts);
    if (res?.error) toast(res.error);
    else if (res?.saved) toast("Exported " + ids.length + " transcripts", "Show in folder", () => api.reveal(res.path), 6000);
    return;
  }
  download(`/api/export-many?${new URLSearchParams({ fmt, ids: ids.join(","), ...opts })}`);
}
function download(href) { const a = document.createElement("a"); a.href = href; document.body.append(a); a.click(); a.remove(); }
function clipDialog(start, end) {
  const s = start ?? (doc.media ? media.currentTime : 0), e = end ?? Math.min((s || 0) + 60, doc.duration || (s || 0) + 60);
  dialog(`<h2>Export a clip</h2><p class="hint" style="font-size:14px">Exports only what was said between two times. Audio isn't cut, only the text and subtitles.</p>
    <div class="grid2"><div class="field"><label for="clA">From</label><input type="text" id="clA" value="${clock(s)}"></div>
    <div class="field"><label for="clB">To</label><input type="text" id="clB" value="${clock(e)}"></div></div>
    <div class="field"><label for="clF">Format</label><select id="clF">${FORMATS.map(([f, l, x]) => `<option value="${f}">${l} (${x})</option>`).join("")}</select></div>
    <label class="switch"><input type="checkbox" id="clR" checked><span><b>Start times at 0:00</b><small>For subtitles that go with a cut video clip</small></span></label>
    <div class="end"><button class="btn" data-close>Cancel</button><button class="btn primary" id="clGo">Export clip</button></div>`, (d) => {
    d.querySelector("#clF").value = prefs.clipFormat || "srt";
    d.querySelector("#clGo").onclick = () => {
      const a = parseClock(d.querySelector("#clA").value), b = parseClock(d.querySelector("#clB").value);
      if (a == null || b == null || b <= a) return toast("Enter a start and end time, like 1:20 and 2:45.");
      const fmt = d.querySelector("#clF").value;
      setPref("clipFormat", fmt);
      closeDialog();
      exportAs(fmt, { start: a, end: b, rebase: d.querySelector("#clR").checked ? 1 : 0 });
    };
  });
}
$("copyBtn").onclick = async () => {
  const text = doc.segments.map((s) => s.text).filter(Boolean).join("\n\n");
  try { await navigator.clipboard.writeText(text); toast("Copied the whole transcript"); } catch { toast("Couldn't copy. Select the text and press Ctrl+C."); }
};
$("moreBtn").onclick = () => {
  const items = [
    { label: "Move to folder…", hint: doc.folder || "", fn: () => moveMenu($("moreBtn"), (f) => { doc.folder = f; scheduleSave(); flushSave().then(loadLibrary); toast(f ? `Moved to ${f}` : "Removed from folder"); }) },
    { label: prefs.unsure ? "Stop marking unsure words" : "Mark unsure words", fn: () => { setPref("unsure", !prefs.unsure); segsEl.classList.toggle("show-unsure", prefs.unsure); } },
  ];
  if (desktop()?.open_library) items.push({ label: "Open library folder", fn: () => desktop().open_library() });
  items.push({ sep: true }, { label: "Delete transcript", danger: true, fn: deleteDoc });
  openMenu($("moreBtn"), items, null, true);
};
async function deleteDoc() {
  const title = doc.title, id = doc.id;
  if (!(await confirmBox(`Delete "${title}"?`, "The transcript and its saved recording will be removed from this computer. This can't be undone.", "Delete"))) return;
  clearTimeout(saveTimer); dirty = false;
  pauseMedia(); media.removeAttribute("src"); media.load();
  try { await req("DELETE", "/api/library/" + id); toast(`Deleted "${title}"`); } catch (e) { toast(e.message); }
  doc = null; show("new"); loadLibrary();
}

/* ======================================================= settings */
$("settingsBtn").onclick = () => openSettings();
async function openSettings(focus) {
  status = await req("GET", "/api/status").catch(() => status);
  const models = status.geminiModels || [];
  dialog(`<h2>Settings</h2>
    <section><h3>Deepgram API key</h3>
      <p class="hint">${status.hasKey ? "A key is saved on this computer. Paste a new one to replace it." : "No key saved yet."} Transcription is billed to this Deepgram account.</p>
      <div class="row"><input type="password" id="sKey" placeholder="Paste Deepgram key" style="flex:1"><button class="btn primary" id="sKeySave">Save key</button></div></section>
    <section><h3>Gemini API key, for write-ups</h3>
      <p class="hint">${status.hasGemini ? "A key is saved on this computer." : "Get a free key at aistudio.google.com under \"Get API key\"."}</p>
      <div class="row"><input type="password" id="gKey" placeholder="Paste Gemini key" style="flex:1"><button class="btn primary" id="gKeySave">Save key</button>
      ${status.hasGemini ? `<button class="btn" id="gKeyDel">Remove</button>` : ""}</div>
      <div class="field" style="margin-top:10px"><label for="gModel">Gemini model</label><select id="gModel">
        ${models.map((m, i) => `<option value="${m}">${m}${i === 0 ? " (fastest, cheapest)" : " (more thorough)"}</option>`).join("")}</select></div></section>
    <section><h3>Updates</h3>
      <label class="switch"><input type="checkbox" id="sAuto" ${prefs.autoUpdate ? "checked" : ""}><span><b>Check for updates when Transcriber opens</b></span></label>
      <div class="row"><button class="btn" id="sCheck">Check now</button><span class="hint">You have version ${esc(status.version || "")}.</span></div></section>
    <section><h3>Library</h3><p class="hint" style="word-break:break-all">${esc(status.library || "")}</p>
      ${desktop()?.open_library ? `<button class="btn" id="sOpen">Open folder</button>` : ""}</section>
    <section><h3>Keyboard shortcuts</h3><div class="keys">
      <kbd>Ctrl+Space</kbd><span>Play or pause</span>
      <kbd>Alt+←</kbd><span>Back 5 seconds</span>
      <kbd>Alt+→</kbd><span>Forward 5 seconds</span>
      <kbd>Alt+click a word</kbd><span>Play from that word</span>
      <kbd>Ctrl+B</kbd><span>Add a bookmark</span>
      <kbd>Enter</kbd><span>Split the paragraph at the cursor</span>
      <kbd>Backspace</kbd><span>At the start of a paragraph, join it to the one above</span>
      <kbd>Ctrl+Z</kbd><span>Undo</span>
      <kbd>Ctrl+Y</kbd><span>Redo</span>
      <kbd>Ctrl+F</kbd><span>Find and replace</span>
      <kbd>Ctrl+N</kbd><span>New transcription</span></div></section>
    <div class="end"><button class="btn" data-close>Done</button></div>`, (d) => {
      const saveKey = async (kind, inputId) => {
        const key = d.querySelector(inputId).value.trim();
        if (!key) return toast("Paste the key first.");
        try {
          await req("POST", "/api/key", { key, kind });
          toast("Key saved");
          if (kind === "deepgram") { hasKey = true; $("keyCard").hidden = true; pump(); loadCredit(); }
          status = await req("GET", "/api/status");
          if (doc && tab === "writeups") renderWriteups();
          openSettings();
        } catch (e) { toast(e.message); }
      };
      d.querySelector("#sKeySave").onclick = () => saveKey("deepgram", "#sKey");
      d.querySelector("#gKeySave").onclick = () => saveKey("gemini", "#gKey");
      d.querySelector("#gKeyDel")?.addEventListener("click", async () => { await req("DELETE", "/api/key?kind=gemini"); status.hasGemini = false; toast("Gemini key removed"); openSettings(); if (doc && tab === "writeups") renderWriteups(); });
      const gm = d.querySelector("#gModel"); gm.value = prefs.geminiModel || models[0] || ""; gm.onchange = () => setPref("geminiModel", gm.value);
      d.querySelector("#sAuto").onchange = (e) => setPref("autoUpdate", e.target.checked);
      d.querySelector("#sCheck").onclick = () => checkUpdates(true);
      d.querySelector("#sOpen")?.addEventListener("click", () => desktop().open_library());
      if (focus === "gemini") d.querySelector("#gKey").focus();
    });
}

/* ======================================================= live recording */
let rec = null;
$("recStart").onclick = startRecording;
async function listMics(selectedId) {
  try {
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    $("micSelect").innerHTML = devs.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "Microphone " + (i + 1))}</option>`).join("");
    if (selectedId) $("micSelect").value = selectedId;
  } catch { /* no device list */ }
}
async function startRecording() {
  if (needKey()) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return toast("Recording isn't available on this computer.");
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: prefs.micId ? { deviceId: { ideal: prefs.micId } } : true }); }
  catch (e) { return toast(e.name === "NotAllowedError" ? "Transcriber wasn't allowed to use the microphone. Allow it and try again." : "Couldn't open the microphone: " + e.message, null, null, 7000); }
  await flushSave(); pauseMedia(); doc = null; renderLibrary();
  const o = currentOpts();
  rec = { id: newId32(), stream, chunks: [], words: [], interim: "", opts: o, t0: 0, wsFailed: false, requestId: null,
    title: "Recording, " + new Date().toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) };
  show("rec");
  $("recNote").hidden = true; $("recRedo").checked = !!prefs.recRedo; $("recLabel").textContent = "Connecting…";
  $("recText").innerHTML = `<span class="interim">Start speaking…</span>`;
  $("recRedo").onchange = (e) => setPref("recRedo", e.target.checked);
  await listMics(stream.getAudioTracks()[0]?.getSettings().deviceId);
  $("micSelect").onchange = () => { setPref("micId", $("micSelect").value); toast("The new microphone is used for your next recording."); };
  // level meter
  try {
    const ac = new AudioContext(), an = ac.createAnalyser();
    ac.createMediaStreamSource(stream).connect(an); an.fftSize = 512;
    const buf = new Uint8Array(an.fftSize);
    rec.ac = ac;
    const tick = () => {
      if (!rec) return;
      an.getByteTimeDomainData(buf);
      let peak = 0; for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      $("recLevel").style.width = Math.min(100, peak / 128 * 160) + "%";
      if (rec.t0) $("recTime").textContent = clock((Date.now() - rec.t0) / 1000);
      rec.raf = requestAnimationFrame(tick);
    };
    tick();
  } catch { /* meter is optional */ }
  // Deepgram live connection
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
  rec.mime = mime;
  const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  rec.recorder = recorder;
  recorder.ondataavailable = (e) => { if (e.data.size) { rec?.chunks.push(e.data); if (rec?.ws?.readyState === 1) rec.ws.send(e.data); } };
  const startRec = () => { if (recorder.state === "inactive") { recorder.start(250); rec.t0 = Date.now(); $("recLabel").textContent = "Recording"; } };
  try {
    const tok = await req("GET", "/api/live/token");
    const lang = o.language === "auto" ? (o.model.startsWith("nova-3") ? "multi" : "en") : o.language;
    const p = new URLSearchParams({ model: o.model, language: lang, smart_format: "true", punctuate: "true", interim_results: "true", endpointing: "300" });
    if (o.diarize) p.set("diarize", "true");
    if (o.filler) p.set("filler_words", "true");
    if (o.profanity) p.set("profanity_filter", "true");
    for (const t of (o.keyterms || "").split("\n").map((x) => x.trim()).filter(Boolean)) p.append(o.model.startsWith("nova-3") ? "keyterm" : "keywords", t);
    const ws = new WebSocket("wss://api.deepgram.com/v1/listen?" + p, [tok.kind, tok.token]);
    rec.ws = ws;
    ws.onopen = startRec;
    ws.onmessage = (m) => {
      if (!rec) return;
      let data; try { data = JSON.parse(m.data); } catch { return; }
      if (data.type === "Metadata" && data.request_id) rec.requestId = data.request_id;
      if (data.type !== "Results") return;
      const alt = data.channel?.alternatives?.[0]; if (!alt) return;
      if (data.is_final) {
        for (const w of alt.words || []) rec.words.push({ w: w.punctuated_word || w.word, s: w.start, e: w.end, c: w.confidence, sp: w.speaker });
        rec.interim = "";
      } else rec.interim = alt.transcript || "";
      renderLive();
    };
    ws.onclose = (e) => {
      if (!rec || rec.stopping) return;
      rec.wsFailed = true; startRec();
      $("recNote").hidden = false;
      $("recNote").textContent = e.code === 1008 || e.code === 4001 ? "Deepgram didn't accept the key for live transcription. The recording continues and will be transcribed when you stop."
        : "The live connection to Deepgram dropped. The recording continues and will be transcribed when you stop.";
    };
    setTimeout(() => { if (rec && !rec.t0) { rec.wsFailed = true; startRec(); } }, 6000);
  } catch { rec.wsFailed = true; startRec(); }
}
function renderLive() {
  const finals = rec.words.map((w) => w.w).join(" ");
  $("recText").innerHTML = esc(finals) + (rec.interim ? ` <span class="interim">${esc(rec.interim)}</span>` : "") || `<span class="interim">Start speaking…</span>`;
  const sc = $("recScroll");
  if (sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160) sc.scrollTop = sc.scrollHeight;
}
function endRecording() {
  return new Promise((resolve) => {
    const r = rec; r.stopping = true;
    cancelAnimationFrame(r.raf); r.ac?.close().catch(() => {});
    const finish = () => {
      r.stream.getTracks().forEach((t) => t.stop());
      if (r.ws && r.ws.readyState === 1) {
        try { r.ws.send(JSON.stringify({ type: "CloseStream" })); } catch { /* closed */ }
        const old = r.ws.onclose;
        const t = setTimeout(() => resolve(), 4000);
        r.ws.onclose = () => { clearTimeout(t); resolve(); };
        void old;
      } else resolve();
    };
    if (r.recorder.state !== "inactive") { r.recorder.onstop = finish; r.recorder.stop(); } else finish();
  });
}
$("recStop").onclick = async () => {
  if (!rec) return;
  $("recLabel").textContent = "Saving…"; $("recStop").disabled = true; $("recDiscard").disabled = true;
  await endRecording();
  const r = rec; rec = null;
  $("recStop").disabled = false; $("recDiscard").disabled = false;
  const duration = r.t0 ? (Date.now() - r.t0) / 1000 : 0;
  const blob = new Blob(r.chunks, { type: (r.mime || "audio/webm").split(";")[0] });
  const ext = (r.mime || "").includes("ogg") ? ".ogg" : ".webm";
  if (!blob.size) { toast("Nothing was recorded."); show("new"); return; }
  if ($("recRedo").checked || r.wsFailed || !r.words.length) {
    const file = new File([blob], r.title.replace(/[\\/:*?"<>|]+/g, ".") + ext, { type: blob.type });
    show("new");
    addFiles([file], { keepMedia: true });
    toast(r.words.length || $("recRedo").checked ? "Transcribing the full recording…" : "Transcribing the recording…");
    return;
  }
  try {
    const d = await req("POST", "/api/live/save", { id: r.id, title: r.title, opts: r.opts, words: r.words, duration,
      language: r.opts.language === "auto" ? "multi" : r.opts.language, requestId: r.requestId });
    if (r.opts.keepMedia !== false) {
      await fetch(`/api/library/${d.id}/media?ext=${ext}`, { method: "PUT", body: blob }).catch(() => {});
      d.media = "media" + ext;
    }
    await loadLibrary(); setTimeout(loadCredit, 6000);
    openDoc(d.id, d);
  } catch (e) {
    toast("Couldn't save the live transcript (" + e.message + "). Transcribing the recording instead.");
    show("new"); addFiles([new File([blob], r.title + ext, { type: blob.type })], { keepMedia: true });
  }
};
$("recDiscard").onclick = async () => {
  if (!rec) return;
  if (rec.t0 && Date.now() - rec.t0 > 10000 && !(await confirmBox("Discard this recording?", "The recording and its words will be thrown away.", "Discard"))) return;
  await endRecording();
  rec = null; show("new");
};

/* ======================================================= playback */
let raf = 0, activeIdx = -1, activeWord = null, activeWords = null, videoHidden = false, fixingDuration = false;
const playPath = "M8 5v14l11-7z", pausePath = "M7 5h4v14H7zM13 5h4v14h-4z";
function pauseMedia() { if (!media.paused) media.pause(); }
function togglePlay() { if (!doc?.media) return; media.paused ? media.play().catch(() => {}) : media.pause(); }
function seek(t, play) { if (!doc?.media) return; media.currentTime = Math.max(0, t); highlight(); if (play) media.play().catch(() => {}); }
$("playBtn").onclick = togglePlay;
$("back5").onclick = () => seek(media.currentTime - 5);
$("fwd5").onclick = () => seek(media.currentTime + 5);
$("rate").onchange = () => { media.playbackRate = +$("rate").value; setPref("rate", $("rate").value); };
$("scrub").oninput = () => { const d = mediaDuration(); if (d) seek(d * $("scrub").value / 1000); };
$("hideVideo").onclick = () => { videoHidden = true; $("videoBox").hidden = true; $("showVideo").hidden = false; };
$("showVideo").onclick = () => { videoHidden = false; $("videoBox").hidden = false; $("showVideo").hidden = true; };
const mediaDuration = () => (isFinite(media.duration) && media.duration) || doc?.duration || 0;
media.addEventListener("loadedmetadata", () => {
  if (!isFinite(media.duration)) { fixingDuration = true; media.currentTime = 1e7; } // recorded WebM: find the real length
  $("tDur").textContent = clock(mediaDuration());
  const isVideo = media.videoWidth > 0;
  $("videoBox").hidden = !isVideo || videoHidden; $("showVideo").hidden = !isVideo || !videoHidden;
  renderMarks();
});
media.addEventListener("durationchange", () => {
  if (fixingDuration && isFinite(media.duration)) { fixingDuration = false; media.currentTime = 0; }
  $("tDur").textContent = clock(mediaDuration()); renderMarks();
});
media.addEventListener("error", () => {
  if (!doc?.media) return;
  $("player").hidden = true; $("noPlayer").hidden = false;
  $("noMediaMsg").textContent = "This recording's format can't be played here. The transcript and exports still work.";
});
media.addEventListener("play", () => { $("playIcon").innerHTML = `<path d="${pausePath}"/>`; cancelAnimationFrame(raf); loop(); });
media.addEventListener("pause", () => { $("playIcon").innerHTML = `<path d="${playPath}"/>`; cancelAnimationFrame(raf); highlight(); });
media.addEventListener("seeked", highlight);
function loop() { highlight(); if (!media.paused) raf = requestAnimationFrame(loop); }
function resetHighlight() { activeIdx = -1; activeWord = null; activeWords = null; highlight(); }
function highlight() {
  if (!doc || fixingDuration) return;
  const t = media.currentTime || 0, d = mediaDuration();
  $("tNow").textContent = clock(t);
  if (d && document.activeElement !== $("scrub")) $("scrub").value = Math.round(t / d * 1000);
  const segs = doc.segments;
  let lo = 0, hi = segs.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (segs[mid].start <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  if (idx !== activeIdx) {
    segsEl.querySelector(".seg.active")?.classList.remove("active");
    activeIdx = idx; activeWords = null;
    const n = idx >= 0 ? nodeOf(segs[idx].id) : null;
    if (n) {
      n.classList.add("active");
      activeWords = [...n.querySelectorAll(".w")];
      const editing = document.activeElement?.closest?.(".txt, .findbar");
      if (prefs.follow && !media.paused && !editing && tab === "transcript") n.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  let now = null;
  if (activeWords) for (const w of activeWords) { if (+w.dataset.s <= t && t < +w.dataset.e + 0.12) { now = w; break; } }
  if (now !== activeWord) { activeWord?.classList.remove("now"); now?.classList.add("now"); activeWord = now; }
}

/* ======================================================= shortcuts */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (menuEl) return closeMenu(); if (dialogEl) return closeDialog(); if (!$("findbar").hidden) return closeFind(); }
  if (dialogEl) return;
  const inDoc = doc && !$("viewDoc").hidden;
  const k = e.key.toLowerCase();
  if (e.ctrlKey && e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.altKey && e.key === "ArrowLeft" && inDoc) { e.preventDefault(); seek(media.currentTime - 5); }
  else if (e.altKey && e.key === "ArrowRight" && inDoc) { e.preventDefault(); seek(media.currentTime + 5); }
  else if (e.ctrlKey && (k === "f" || k === "h") && inDoc) { e.preventDefault(); openFind(k === "h"); }
  else if (e.ctrlKey && k === "b" && inDoc) { e.preventDefault(); addBookmark(); }
  else if (e.ctrlKey && k === "n") { e.preventDefault(); showNew(); }
  else if (e.ctrlKey && k === "s") { e.preventDefault(); flushSave(); }
  else if (inDoc && e.ctrlKey && (k === "z" || k === "y")) {
    const el = document.activeElement;
    if (isTyping(el) && !el.closest?.(".txt")) return; // title, find box etc. keep their own undo
    e.preventDefault();
    if (k === "y" || e.shiftKey) redo(); else undo();
  }
});

/* ======================================================= start */
(async function init() {
  try {
    const [st, p] = await Promise.all([req("GET", "/api/status"), req("GET", "/api/prefs")]);
    status = st; hasKey = st.hasKey; $("keyCard").hidden = st.hasKey;
    prefs = { ...DEFAULTS, ...p };
  } catch { /* defaults */ }
  bindOptions();
  await loadLibrary();
  if (hasKey) loadCredit();
  if (prefs.autoUpdate) setTimeout(() => checkUpdates(false), 1500);
})();

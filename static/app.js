"use strict";
// Receipts phone app. Photos are kept in IndexedDB until the server has them, so a photo taken
// away from home is sent the next time the phone can reach the server.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const money = (v) => (v == null || v === "" ? "" : "$" + Number(v).toFixed(2));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
const sessionStorageGet = (k) => { try { return sessionStorage.getItem(k); } catch { return null; } };
const sessionStorageSet = (k, v) => { try { sessionStorage.setItem(k, v); } catch {} };
let config = null;
let current = null;      // document open in the editor
let listShow = "check";

async function api(path, opts = {}) {
  const r = await fetch("api/" + path, { credentials: "same-origin", redirect: "manual", ...opts });
  // Authentik sign-in ran out: the proxy redirects to the login page, which fetch can't follow
  if (r.type === "opaqueredirect") { if (navigator.onLine) location.reload(); throw new Error("signed out"); }
  if (r.status === 401 && path !== "login") { showLock(); throw new Error("locked"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const d = (await r.json()).detail; msg = typeof d === "string" ? d : msg; } catch {}
    const e = new Error(msg); e.status = r.status; throw e;
  }
  return r.json();
}
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ---------- offline queue ----------
const queue = {
  db: null,
  open() {
    if (this.db) return Promise.resolve(this.db);
    return new Promise((res, rej) => {
      let req;
      try { req = indexedDB.open("receipts", 1); } catch (e) { return rej(e); }
      req.onupgradeneeded = () => req.result.createObjectStore("out", { keyPath: "id" });
      req.onsuccess = () => res((this.db = req.result));
      req.onerror = () => rej(req.error);
    });
  },
  async tx(mode, fn) {
    const db = await this.open();
    return new Promise((res, rej) => {
      const t = db.transaction("out", mode);
      const out = fn(t.objectStore("out"));
      t.oncomplete = () => res(out && out.result);
      t.onerror = () => rej(t.error);
    });
  },
  add(item) { return this.tx("readwrite", (s) => s.put(item)); },
  remove(id) { return this.tx("readwrite", (s) => s.delete(id)); },
  all() { return this.tx("readonly", (s) => s.getAll()); },
};

let sending = false;
async function flush() {
  if (sending) return;
  sending = true;
  try {
    let items;
    try { items = await queue.all(); } catch { items = []; }
    for (const it of items) {
      // photos are stored as bytes: iPhone Safari can hand back an empty File from IndexedDB
      const blob = it.data ? new Blob([it.data], { type: it.type || "image/jpeg" }) : it.blob;
      const fallbackName = (it.type || "").includes("pdf") ? "bill.pdf" : "photo.jpg";
      if (!blob || !blob.size) { await queue.remove(it.id); status("One photo was lost on the phone before it could be sent. Please take it again."); continue; }
      const fd = new FormData();
      fd.append("file", blob, it.name || fallbackName);
      fd.append("owner", it.owner || "");
      fd.append("client_id", it.id);
      try {
        await api("docs", { method: "POST", body: fd });
        await queue.remove(it.id);
      } catch (e) {
        if (e.message === "locked" || e.message === "signed out") break;
        if (e.status && e.status >= 400 && e.status < 500) { await queue.remove(it.id); status("Not sent: " + e.message + ". Please take it again."); continue; }
        break;   // server unreachable or busy: try again later
      }
    }
    const left = (await queue.all().catch(() => [])).length;
    if (!left && /lost on the phone|Not sent/.test($("#send-status").textContent)) { refreshBadge(); return; }
    status(left ? `${left} photo${left > 1 ? "s" : ""} waiting to send. They go when the server is reachable.` : "");
    refreshBadge();
  } finally { sending = false; }
}
function status(t) { $("#send-status").textContent = t; }

async function takePhotos(files) {
  const owner = store.get("owner") || "";
  for (const f of files) {
    const id = (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random()).toString();
    try {
      const data = await f.arrayBuffer();
      await queue.add({ id, data, type: f.type, name: f.name, owner, at: Date.now() });
    } catch {
      // no IndexedDB (private window): send directly
      const fd = new FormData();
      fd.append("file", f, f.name); fd.append("owner", owner); fd.append("client_id", id);
      try { await api("docs", { method: "POST", body: fd }); } catch (e) { status("Could not send: " + e.message); return; }
    }
  }
  status(files.length > 1 ? `Sending ${files.length} photos…` : "Sending…");
  await flush();
  if (!$("#send-status").textContent) status(files.length > 1 ? `${files.length} photos sent. They are being read.` : "Sent. It is being read; check it under To check.");
}

// ---------- views ----------
function show(view) {
  $$(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
  $$(".tabs button").forEach((b) => b.toggleAttribute("aria-current", b.dataset.view === view || (view === "doc" && b.dataset.view === "check")));
  $$(".tabs button[aria-current]").forEach((b) => b.setAttribute("aria-current", "page"));
  window.scrollTo(0, 0);
  if (view === "check") loadList();
  if (view === "spend") loadSpending();
}

function renderWho() {
  const who = $("#who");
  who.innerHTML = "";
  if (!config.people.length) return;
  if (config.me && !sessionStorageGet("picked")) store.set("owner", config.me);
  if (!config.people.includes(store.get("owner"))) store.set("owner", config.people[0]);
  for (const p of config.people) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = p; b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(store.get("owner") === p));
    b.onclick = () => { store.set("owner", p); sessionStorageSet("picked", "1"); renderWho(); };
    who.append(b);
  }
}

const STATE = {
  queued: "Waiting to be read", uploading: "Sending to Paperless", reading: "Being read",
  review: "To check", failed: "Failed", duplicate: "Duplicate", done: "Filed", cancel: "Deleting",
};

async function refreshBadge() {
  try {
    const { waiting } = await api("docs?show=check&limit=1");
    const b = $("#badge");
    b.hidden = !waiting; b.textContent = waiting;
  } catch {}
}

let listTimer = null;
async function loadList() {
  clearTimeout(listTimer);
  let data;
  try { data = await api("docs?show=" + listShow); } catch { return; }
  const ul = $("#doc-list");
  ul.innerHTML = "";
  for (const d of data.docs) {
    const li = document.createElement("li");
    li.className = "doc"; li.tabIndex = 0;
    const kind = config.kinds.find((k) => k.key === d.kind);
    const busy = ["queued", "uploading", "reading", "cancel"].includes(d.status);
    li.innerHTML = (d.manual ? `<div class="noimg">No photo</div>` : `<img alt="" loading="lazy">`) + `<div class="doc-main"><div class="t"></div><div class="s"></div></div><div class="amt"></div>`;
    if (!d.manual) $("img", li).src = `api/docs/${d.id}/photo`;
    const what = d.manual ? "No photo" : d.pdf ? "PDF" : "Photo";
    $(".t", li).textContent = d.issuer || d.title || (busy ? `New ${what === "PDF" ? "PDF" : "photo"}` : kind ? kind.label : "Unread");
    const pill = document.createElement("span");
    pill.className = "pill" + (["failed", "duplicate"].includes(d.status) || d.error ? " warn" : "");
    pill.textContent = d.status === "review" ? (kind ? kind.label : "To check") : STATE[d.status] || d.status;
    const type = document.createElement("span");
    type.className = "pill type"; type.textContent = what;
    const when = document.createElement("span");
    when.className = "when"; when.textContent = [d.date, d.owner].filter(Boolean).join(" · ");
    $(".s", li).append(type, pill, when);
    $(".amt", li).textContent = money(d.total);
    const open = () => openDoc(d.id);
    li.onclick = open; li.onkeydown = (e) => e.key === "Enter" && open();
    ul.append(li);
  }
  const empty = $("#doc-empty");
  empty.hidden = data.docs.length > 0;
  empty.textContent = listShow === "check" ? "Nothing to check. New photos show up here once they are read." : "Nothing filed yet.";
  $("#badge").hidden = !data.waiting; $("#badge").textContent = data.waiting;
  if (data.docs.some((d) => ["queued", "uploading", "reading", "cancel"].includes(d.status)) && !$("#view-check").hidden) {
    listTimer = setTimeout(loadList, 5000);
  }
}

// ---------- the editor ----------
function fillSelect(sel, values, labels) {
  sel.innerHTML = "";
  values.forEach((v, i) => { const o = document.createElement("option"); o.value = v; o.textContent = labels ? labels[i] : v || "—"; sel.append(o); });
}

function itemRow(it = {}) {
  const li = $("#item-row").content.firstElementChild.cloneNode(true);
  fillSelect($(".i-cat", li), ["", ...config.categories]);
  $(".i-name", li).value = it.name || "";
  $(".i-price", li).value = it.price ?? "";
  $(".i-cat", li).value = config.categories.includes(it.category) ? it.category : "";
  $(".i-del", li).onclick = () => { li.remove(); itemsSum(); };
  $(".i-price", li).oninput = itemsSum;
  return li;
}

function itemsSum() {
  const prices = $$(".i-price").map((i) => parseFloat(i.value) || 0);
  const sum = prices.reduce((a, b) => a + b, 0);
  const total = parseFloat($("#doc-form").total.value);
  const p = $("#items-sum");
  if (!prices.length) { p.textContent = ""; p.className = "items-sum"; return; }
  const off = !isNaN(total) && Math.abs(sum - total) > 0.01;
  p.textContent = off
    ? `Items add up to ${money(sum)}, total is ${money(total)}: ${money(Math.abs(total - sum))} ${sum > total ? "over (discount line?)" : "missing"}.`
    : `Items add up to ${money(sum)}.`;
  p.className = "items-sum" + (off ? " off" : "");
}

function kindChanged() {
  const k = config.kinds.find((x) => x.key === $("#f-kind").value) || { fields: [], spending: false };
  $$(".spend-only").forEach((el) => (el.hidden = !k.spending));
  $(".field-due").hidden = !k.fields.includes("Due date");
  $(".field-vital").hidden = !k.fields.includes("Expiry date") && !k.fields.includes("Issue date");
  $(".field-number").hidden = !k.fields.includes("Document number");
}

let docTimer = null;
function openManual() {
  clearTimeout(docTimer);
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  current = { id: null, manual: true, status: "new", info: { kind: "receipt", date: today, payment_method: "Card", items: [] } };
  show("doc");
  fillDoc(current);
  $("#doc-state").textContent = "New entry, no photo" + (store.get("owner") ? " · " + store.get("owner") : "");
  $("#save").textContent = "Save";
  setTimeout(() => $("#doc-form").issuer.focus(), 50);
}

async function openDoc(id) {
  clearTimeout(docTimer);
  let d;
  try { d = await api("docs/" + id); } catch (e) { return alert(e.message); }
  current = d;
  show("doc");
  fillDoc(d);
}

function fillDoc(d) {
  const id = d.id;
  const fig = $("#doc-photo");
  fig.innerHTML = ""; fig.classList.remove("zoom");
  fig.hidden = !!d.manual;
  if (d.manual) return fillForm(d);
  const img = new Image();
  img.alt = "Photo of the document";
  img.src = `api/docs/${id}/photo`;
  img.onclick = () => {
    fig.classList.toggle("zoom");
    if (fig.classList.contains("zoom")) img.src = `api/docs/${id}/photo?size=full`;
  };
  fig.append(img);
  fillForm(d);
}

function fillForm(d) {
  const id = d.id;
  const busy = ["queued", "uploading", "reading", "cancel"].includes(d.status);
  $("#doc-state").textContent = STATE[d.status] || d.status;
  $("#doc-error").hidden = !d.error; $("#doc-error").textContent = d.error || "";
  $("#doc-cancel").hidden = !busy || !d.id;
  const f = $("#doc-form");
  const info = d.info || {};
  f.hidden = busy;
  if (busy) { docTimer = setTimeout(() => openDoc(id), 4000); return; }

  for (const n of ["title", "issuer", "location", "date", "due_date", "issue_date", "expiry_date", "document_number"]) f[n].value = info[n] || "";
  if (!info.date && d.date) f.date.value = d.date;
  f.total.value = info.total ?? ""; f.gst.value = info.gst ?? "";
  f.kind.value = info.kind || "receipt";
  f.payment_method.value = config.methods.includes(info.payment_method) ? info.payment_method : "Unknown";
  f.category.value = config.categories.includes(info.category) ? info.category : "";
  const ul = $("#items"); ul.innerHTML = "";
  (info.items || []).forEach((it) => ul.append(itemRow(it)));
  kindChanged(); itemsSum();
  $("#save").textContent = d.status === "done" ? "Save changes" : "Looks right, file it";
  $("#reread").hidden = d.status === "duplicate" || !!d.manual;
  $("#delete").hidden = !d.id;
  $("#paperless-link").textContent = [d.paperless_id ? `Paperless document ${d.paperless_id}${d.pdf ? " (PDF, first page shown)" : ""}` : "", d.wallos || ""].filter(Boolean).join(" · ");
}

async function saveDoc(e) {
  e.preventDefault();
  const f = e.target;
  const body = Object.fromEntries(["kind", "title", "issuer", "location", "date", "due_date", "issue_date", "expiry_date", "document_number", "payment_method", "category", "total", "gst"].map((n) => [n, f[n].value]));
  body.items = $$("#items .item").map((li) => ({ name: $(".i-name", li).value, price: $(".i-price", li).value, qty: 1, category: $(".i-cat", li).value }));
  const btn = $("#save"); btn.disabled = true; btn.textContent = "Filing…";
  try {
    const isNew = current.manual && !current.id;
    if (isNew) body.owner = store.get("owner") || "";
    const saved = await api(isNew ? "manual" : "docs/" + current.id, json(isNew ? "POST" : "PUT", body));
    if (isNew) { show("capture"); status(`Saved: ${saved.issuer || "expense"} ${money(saved.total)}`); } else show("check");
    if (saved.wallos) setTimeout(() => alert(saved.wallos), 50);
  } catch (err) {
    alert("Not saved: " + err.message);
  } finally { btn.disabled = false; }
}

// ---------- spending ----------
function bars(ul, rows, fmt) {
  ul.innerHTML = "";
  const max = Math.max(...rows.map((r) => r.amount), 0.01);
  for (const r of rows) {
    const li = document.createElement("li");
    li.style.setProperty("--w", ((r.amount / max) * 100).toFixed(1) + "%");
    const a = document.createElement("span"); a.textContent = fmt ? fmt(r) : r.name;
    const b = document.createElement("span"); b.className = "v"; b.textContent = money(r.amount);
    li.append(a, b); ul.append(li);
  }
  if (!rows.length) { const li = document.createElement("li"); li.textContent = "Nothing yet"; ul.append(li); }
}

let spendPeriod = store.get("period") || "month";

async function loadSpending(key) {
  let s;
  try { s = await api(`spending?period=${spendPeriod}` + (key ? "&key=" + encodeURIComponent(key) : "")); } catch { return; }
  $$("#period-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.period === s.period)));
  fillSelect($("#month"), s.periods.map((p) => p.key), s.periods.map((p) => p.label));
  $("#month").value = s.key;
  $("#spend-total").textContent = money(s.total);
  $("#spend-count").textContent = `${s.label}: ${s.count} receipt${s.count === 1 ? "" : "s"} and bills`;
  $("#trend-head").textContent = `Last 8 ${s.period === "week" ? "weeks" : s.period === "quarter" ? "quarters" : "months"}`;
  const trend = $("#trend"); trend.innerHTML = "";
  const max = Math.max(...s.trend.map((t) => t.amount), 0.01);
  const tip = (t) => { $("#trend-tip").textContent = `${t.label}: ${money(t.amount)}`; };
  for (const t of s.trend) {
    const b = document.createElement("button");
    b.type = "button"; b.setAttribute("role", "listitem");
    b.setAttribute("aria-label", `${t.label}: ${money(t.amount)}`);
    b.setAttribute("aria-current", String(t.key === s.key));
    b.innerHTML = `<span class="col"></span><span class="lab"></span>`;
    $(".col", b).style.height = t.amount ? Math.max(2, (t.amount / max) * 100) + "%" : "2px";
    $(".lab", b).textContent = t.short;
    b.onmouseenter = () => tip(t); b.onfocus = () => tip(t);
    b.onclick = () => loadSpending(t.key);
    trend.append(b);
  }
  tip(s.trend.find((t) => t.key === s.key) || s.trend[s.trend.length - 1]);
  bars($("#spend-cats"), s.categories);
  bars($("#spend-shops"), s.shops, (r) => `${r.name} (${r.count})`);
  const many = s.people.length > 1;
  $("#people-head").hidden = !many; $("#spend-people").hidden = !many;
  if (many) bars($("#spend-people"), s.people);
}

let qTimer = null;
function searchItems() {
  clearTimeout(qTimer);
  qTimer = setTimeout(async () => {
    const q = $("#item-q").value.trim();
    const out = $("#item-rows"); out.innerHTML = ""; $("#item-total").textContent = "";
    if (q.length < 2) return;
    let r;
    try { r = await api("items?q=" + encodeURIComponent(q)); } catch { return; }
    $("#item-total").textContent = r.count ? `${money(r.total)} on ${r.count} purchase${r.count === 1 ? "" : "s"}` : "Not found on any receipt.";
    for (const it of r.items) {
      const li = document.createElement("li");
      li.innerHTML = `<span></span><span class="s"></span><span class="v"></span>`;
      li.children[0].textContent = it.name;
      li.children[1].textContent = [it.date, it.merchant].filter(Boolean).join(" · ");
      li.children[2].textContent = money(it.price);
      out.append(li);
    }
  }, 250);
}

// ---------- start ----------
function showLock() {
  $("#app").hidden = true; $("#lock").hidden = false;
  setTimeout(() => $("#passcode").focus(), 50);
}

async function start() {
  try { config = await api("config"); } catch { status("Cannot reach the server. Photos you take now are sent later."); }
  if (config && config.locked) return showLock();
  if (config && config.denied) {
    $("#app").hidden = true; $("#lock").hidden = false;
    $("#lock-form").innerHTML = `<h1>Receipts</h1><p class="error"></p>`;
    $("#lock-form .error").textContent = config.denied;
    return;
  }
  $("#lock").hidden = true; $("#app").hidden = false;
  if (!config) return;
  renderWho();
  fillSelect($("#f-kind"), config.kinds.map((k) => k.key), config.kinds.map((k) => k.label));
  fillSelect($("#f-method"), config.methods);
  fillSelect($("#f-cat"), ["", ...config.categories]);
  flush();
}

$("#lock-form").onsubmit = async (e) => {
  e.preventDefault();
  $("#lock-error").textContent = "";
  try {
    await api("login", json("POST", { passcode: $("#passcode").value }));
    $("#passcode").value = "";
    start();
  } catch (err) { $("#lock-error").textContent = err.message; }
};
$("#camera").onchange = (e) => { if (e.target.files.length) takePhotos([...e.target.files]); e.target.value = ""; };
$("#library").onchange = (e) => { if (e.target.files.length) takePhotos([...e.target.files]); e.target.value = ""; };
$$(".tabs button").forEach((b) => (b.onclick = () => show(b.dataset.view)));
$$(".seg button").forEach((b) => (b.onclick = () => {
  listShow = b.dataset.show;
  $$(".seg button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  loadList();
}));
$("#doc-back").onclick = () => { clearTimeout(docTimer); show("check"); };
$("#f-kind").onchange = kindChanged;
$("#doc-form").total.oninput = itemsSum;
$("#doc-form").onsubmit = saveDoc;
$("#add-item").onclick = () => { $("#items").append(itemRow()); itemsSum(); $("#items .item:last-child .i-name").focus(); };
$("#reread").onclick = async () => {
  try { await api(`docs/${current.id}/reread`, { method: "POST" }); openDoc(current.id); } catch (e) { alert(e.message); }
};
$("#doc-cancel").onclick = async () => {
  if (!confirm("Delete this upload? It is removed as soon as the server finishes its current step.")) return;
  clearTimeout(docTimer);
  try { await api("docs/" + current.id, { method: "DELETE" }); show("check"); } catch (e) { alert(e.message); }
};
$("#delete").onclick = async () => {
  if (!confirm(current.manual ? "Delete this entry?" : "Delete this photo here and move it to the Paperless trash?")) return;
  try { await api("docs/" + current.id, { method: "DELETE" }); show("check"); } catch (e) { alert(e.message); }
};
$("#month").onchange = (e) => loadSpending(e.target.value);
$$("#period-tabs button").forEach((b) => (b.onclick = () => { spendPeriod = b.dataset.period; store.set("period", spendPeriod); loadSpending(); }));
$("#manual").onclick = openManual;
$("#item-q").oninput = searchItems;
window.addEventListener("online", flush);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { flush(); if (!$("#view-check").hidden) loadList(); } });
setInterval(flush, 60000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
start();

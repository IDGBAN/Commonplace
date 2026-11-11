"use strict";

// Commonplace journal browser. Plain DOM, no dependencies.
// Journal text only ever reaches the page through textContent or text nodes.

const SVG_NS = "http://www.w3.org/2000/svg";
const page = document.getElementById("page");

// ---------------------------------------------------------------- DOM helpers

function h(tag, props, ...children) {
  const isSvg = tag.startsWith("svg:");
  const el = isSvg ? document.createElementNS(SVG_NS, tag.slice(4)) : document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2), value);
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  add(el, children);
  return el;
}

function add(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const svg = (tag, props, ...children) => h(`svg:${tag}`, props, ...children);

// replaceChildren() stringifies arrays, null and false; this flattens and skips them like h()
function fill(el, ...children) {
  el.replaceChildren();
  return add(el, children);
}

// ---------------------------------------------------------------- formatting

const numberFmt = new Intl.NumberFormat();
const fmtNum = (n) => numberFmt.format(n ?? 0);
const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;

function toDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d || 1));
}
const dateFmt = (opts) => new Intl.DateTimeFormat(undefined, { timeZone: "UTC", ...opts });
const fmtLong = dateFmt({ weekday: "long", day: "numeric", month: "long", year: "numeric" });
const fmtDayMonthYear = dateFmt({ day: "numeric", month: "long", year: "numeric" });
const fmtMedium = dateFmt({ day: "numeric", month: "short", year: "numeric" });
const fmtShort = dateFmt({ day: "numeric", month: "short" });
const fmtMonth = dateFmt({ month: "long", year: "numeric" });
const fmtMonthShort = dateFmt({ month: "short" });
const fmtMonthLong = dateFmt({ month: "long" });
const fmtToday = dateFmt({ weekday: "long", day: "numeric", month: "long" });
const fmtWeekday = dateFmt({ weekday: "long" });

const longDate = (iso) => (iso ? fmtLong.format(toDate(iso)) : "–");
const mediumDate = (iso) => (iso ? fmtMedium.format(toDate(iso)) : "–");

// not toISOString(), which is already tomorrow on a US evening
function localToday() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function stamp(isoTimestamp) {
  if (!isoTimestamp) return "unknown time";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return isoTimestamp;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const fmtMood = (m) => (m == null ? "–" : String(Math.round(m * 10) / 10));
const valueFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
// a yes/no number is stored as 1 or 0, so its average is the share of days with it
const fmtValue = (v, yesNo) => (v == null ? "–" : yesNo ? `${Math.round(v * 100)}%` : valueFmt.format(v));

function moodClass(mood, scale) {
  if (mood == null) return null;
  const t = Math.min(1, Math.max(0, (mood - 1) / Math.max(1, scale - 1)));
  return `m${Math.round(t * 8)}`;
}

function bytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function periodTitle(type, key) {
  if (type === "range") {
    const [start, end] = key.split("..");
    return start === end ? mediumDate(start) : `${mediumDate(start)} to ${mediumDate(end)}`;
  }
  if (type === "month") return fmtMonth.format(toDate(`${key}-01`));
  if (type === "week") {
    const [year, week] = key.split("-W");
    return `Week ${Number(week)}, ${year}`;
  }
  return key;
}

function periodRange(type, start, end) {
  if (!start || !end) return "";
  if (type === "week") return `${fmtShort.format(toDate(start))} – ${fmtShort.format(toDate(end))}`;
  return `${mediumDate(start)} – ${mediumDate(end)}`;
}

const KIND_LABELS = { week: "Week", month: "Month", year: "Year", range: "Span" };
const DIGEST_TABS = ["year", "month", "week", "range", "notes"];
const kindLabel = (kind) => kind.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());

// ---------------------------------------------------------------- links

const href = {
  overview: () => "#/",
  calendar: (year, by) => `${year ? `#/calendar/${year}` : "#/calendar"}${query({ by })}`,
  entries: (params) => `#/entries${query(params)}`,
  entry: (id, params) => `#/entry/${id}${query(params)}`,
  notes: (params) => `#/notes${query(params)}`,
  map: (params) => `#/map${query(params)}`,
  themes: () => "#/themes",
  theme: (id) => `#/theme/${id}`,
  note: (id, params) => `#/note/${id}${query(params)}`,
  tags: () => "#/tags",
  metrics: () => "#/metrics",
  metric: (key) => `#/metric/${encodeURIComponent(key)}`,
  tag: (name) => `#/tag/${encodeURIComponent(name)}`,
  digests: (type) => `#/digests/${type || "month"}`,
  digest: (type, key) => `#/digest/${type}/${encodeURIComponent(key)}`,
  compare: (first, second) => `#/compare/${encodeURIComponent(first)}${second ? `/${encodeURIComponent(second)}` : ""}`,
  review: (key) => `#/review/${encodeURIComponent(key)}`,
  files: (params) => `#/files${query(params)}`,
  search: (q) => `#/search${query({ q })}`,
  ask: (q, scope) => `#/ask${query({ q, ...scope })}`,
  history: (id) => `#/history/${id}`,
  historyList: () => "#/history",
};

function query(params) {
  const clean = Object.entries(params || {}).filter(([, v]) => v != null && v !== "");
  return clean.length ? `?${new URLSearchParams(clean)}` : "";
}

function replaceParams(path, params) {
  history.replaceState(null, "", `#${path}${query(params)}`);
}

// ---------------------------------------------------------------- data

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

const OFFLINE = "Commonplace isn't answering. Is `journal browse` still running?";

async function api(path, params) {
  let response;
  try {
    response = await fetch(`/api/${path}${query(params)}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError(OFFLINE, 0);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data.error || `Request failed (${response.status}).`, response.status);
  return data;
}

// Anything that does something is a POST with the token this page was given.
// `journal browse` makes a new token each time it starts, so a page left open
// across a restart asks for it once more before giving up.
let sessionToken = null;

async function send(path, body, { signal, accept = "application/json" } = {}) {
  for (let attempt = 0; ; attempt++) {
    if (!sessionToken || attempt) sessionToken = (await api("session")).token;
    let response;
    try {
      response = await fetch(`/api/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: accept, "X-Journal-Token": sessionToken },
        body: JSON.stringify(body || {}),
        signal,
      });
    } catch (err) {
      if (signal?.aborted) throw err;
      throw new ApiError(OFFLINE, 0);
    }
    if (response.status !== 401 || attempt) return response;
  }
}

async function post(path, body) {
  const response = await send(path, body);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data.error || `Request failed (${response.status}).`, response.status);
  return data;
}

let catalogCache = null;
let catalogAt = 0;

function getCatalog(fresh = false) {
  if (fresh || !catalogCache || Date.now() - catalogAt > 60_000) {
    catalogAt = Date.now();
    catalogCache = api("catalog").then(indexLinks).catch((err) => {
      catalogCache = null;
      throw err;
    });
  }
  return catalogCache;
}

// ---------------------------------------------------------------- shared pieces

// `eyebrow` shows only when it's a link: the way back to the list this page is from
function pageHead({ eyebrow, title, lede, actions, meta }) {
  return h("header", { class: "page-head" },
    eyebrow instanceof Node && h("p", { class: "crumb" }, eyebrow),
    h("div", { class: "head-row" },
      h("h1", { tabindex: "-1" }, title),
      actions && h("div", { class: "pager-nav" }, actions)),
    lede && h("p", { class: "lede" }, lede),
    meta && h("div", { class: "meta-line" }, meta));
}

function section(title, children, { note, intro, id } = {}) {
  return h("section", { class: "section", id },
    h("div", { class: "section-head" }, h("h2", {}, title), note && h("span", { class: "section-note" }, note)),
    intro && h("p", { class: "section-intro" }, intro),
    children);
}

function empty(...children) {
  return h("p", { class: "empty" }, ...children);
}

function swatch(mood, scale) {
  const cls = moodClass(mood, scale);
  return h("span", {
    class: cls ? `swatch ${cls}` : "swatch none",
    title: mood == null ? "No mood score" : `Mood ${fmtMood(mood)} of ${scale}`,
    "aria-hidden": "true",
  });
}

function moodChip(mood, scale) {
  return h("span", { class: "mood-chip" },
    swatch(mood, scale),
    mood == null ? h("span", { class: "none" }, "no mood") : `${fmtMood(mood)} / ${scale}`);
}

function delta(value, baseline) {
  if (value == null || baseline == null) return null;
  const d = Math.round((value - baseline) * 10) / 10;
  const cls = d >= 0.5 ? "delta up" : d <= -0.5 ? "delta down" : "delta";
  const text = d === 0 ? "same as usual" : `${d > 0 ? "+" : "−"}${Math.abs(d)} vs usual`;
  return h("span", { class: cls, title: `Your average is ${fmtMood(baseline)}` }, text);
}

const tagChip = (name, n) =>
  h("a", { class: "chip", href: href.tag(name) }, name, n != null && h("span", { class: "n" }, fmtNum(n)));

const noteChip = (note, cls = "chip") =>
  h("a", { class: cls, href: href.note(note.id), title: kindLabel(note.kind) }, note.title,
    h("span", { class: "n" }, kindLabel(note.kind)));

// `aside` replaces the tags under each line, like a number's value on its page
function entryLines(entries, scale, { emptyText, aside } = {}) {
  if (!entries.length) return empty(emptyText || "No entries.");
  // search results say which part of a many-headed entry matched, and open there
  return h("ol", { class: "lines" }, entries.map((e) => {
    const under = aside ? aside(e) : [e.section && `in “${e.section}”`, ...(e.tags || [])].filter(Boolean).join(" · ");
    return h("li", {},
      h("a", { class: "line", href: href.entry(e.id, e.section ? { at: e.section } : null) },
        h("span", { class: "line-date" }, mediumDate(e.date)),
        swatch(e.mood, scale),
        h("span", { class: e.summary ? "line-summary" : "line-summary none" }, e.summary || "No summary stored"),
        under ? h("span", { class: "line-tags" }, under) : null));
  }));
}

// The stored text, with the part a link's #heading or #^block points at marked.
// `focus` is what the API sends back for ?at=.
function rawText(text, focus, emptyText) {
  if (!text) return h("div", { class: "raw empty" }, emptyText);
  if (!focus || !focus.lines) {
    // shown the way Obsidian would, with the stored text a click away
    const body = h("div", { class: "raw md" }, md(text));
    const toggle = h("button", { class: "link-button", type: "button", "aria-pressed": "false" }, "Show it as stored");
    toggle.addEventListener("click", () => {
      const stored = toggle.getAttribute("aria-pressed") !== "true";
      toggle.setAttribute("aria-pressed", String(stored));
      toggle.textContent = stored ? "Show it formatted" : "Show it as stored";
      body.className = stored ? "raw" : "raw md";
      fill(body, stored ? text : md(text));
    });
    return h("div", {}, body, toggle);
  }
  const lines = text.split("\n");
  const [start, end] = focus.lines;
  return h("div", { class: "raw" },
    start > 0 ? `${lines.slice(0, start).join("\n")}\n` : "",
    h("span", { class: "focus", id: "focus", tabindex: "-1" }, lines.slice(start, end).join("\n")),
    end < lines.length ? `\n${lines.slice(end).join("\n")}` : "");
}

function focusNotice(focus, what) {
  if (!focus || focus.lines) return null;
  return h("div", { class: "notice info" }, h("span", { class: "notice-label" }, "Part not found"),
    h("span", {}, `There's no “${focus.label}” part in this ${what} any more, so all of it is shown.`));
}

// once render() has scrolled to the top and focused the heading
function showFocus(node) {
  setTimeout(() => {
    const marked = node.querySelector("#focus");
    if (!marked || !marked.isConnected) return;
    marked.scrollIntoView({ block: "center" });
    marked.focus({ preventScroll: true });
  }, 0);
}

const groupLink = (key, value) => href.entries({ group: `${key}=${value}` });

function sectionChip(label, link, part, title) {
  return h("a", { class: "chip pencil", href: link, title }, label, h("span", { class: "n" }, `› ${part}`));
}

function gloss(label, ...body) {
  return h("div", { class: "gloss" },
    h("div", { class: "gloss-label" }, label),
    h("div", { class: "gloss-body" }, ...body));
}

function button(label, link, { disabled, key, title } = {}) {
  return h("a", {
    class: "button", href: disabled ? null : link, "aria-disabled": disabled ? "true" : null, title,
  }, label, key && h("kbd", {}, key));
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function field(label, control, grow = false) {
  return h("label", { class: grow ? "field grow" : "field" }, h("span", {}, label), control);
}

// a short word that something happened, for a few seconds
const toasts = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
document.body.append(toasts);

function toast(message, kind = "") {
  const el = h("div", { class: `toast ${kind}`.trim() }, message);
  toasts.append(el);
  setTimeout(() => el.remove(), kind === "error" ? 9000 : 5000);
}

// Asks before something that can't be undone. Resolves true only on the action button.
function confirmAction({ title, body, action }) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "dialog", "aria-labelledby": "dialog-title" },
      h("h2", { id: "dialog-title" }, title),
      h("p", {}, body),
      h("div", { class: "dialog-actions" },
        h("button", { class: "button", type: "button", autofocus: true, onclick: () => dialog.close("no") }, "Cancel"),
        h("button", { class: "button danger", type: "button", onclick: () => dialog.close("yes") }, action)));
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(dialog.returnValue === "yes");
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

// ---------------------------------------------------------------- markdown

// What the models write is often Markdown. It's turned into elements here, one
// text node at a time, so nothing a model or an entry says is ever read as HTML.
// [[links]] and dates go to the day or note they name, when there is one.

const linkIndex = { days: new Map(), notes: new Map() };

function indexLinks(cat) {
  linkIndex.days = new Map(cat.entries.map((e) => [e.date, e.id]));
  linkIndex.notes = new Map();
  for (const n of cat.notes) {
    // a title wins over another note's alias, and an alias over a learned name
    for (const names of [[n.title], n.aliases, n.learned]) {
      for (const name of names) {
        const key = name.toLowerCase();
        if (!linkIndex.notes.has(key) || names.length === 1 && names[0] === n.title) linkIndex.notes.set(key, n.id);
      }
    }
  }
  return cat;
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function wikilink(target, label) {
  const [name, part] = target.split("#").map((x) => x.trim());
  const isDay = ISO_DAY.test(name);
  const id = isDay ? linkIndex.days.get(name) : linkIndex.notes.get(name.toLowerCase());
  const text = label || `${isDay ? mediumDate(name) : name}${part ? ` › ${part.replace(/^\^/, "")}` : ""}`;
  if (id == null) return h("span", { class: "cite none", title: isDay ? "No entry on that day" : "No note by that name" }, text);
  const at = part ? { at: part } : null;
  return h("a", { class: isDay && !part ? "cite day" : "cite", href: isDay ? href.entry(id, at) : href.note(id, at) }, text);
}

const MD_INLINE = new RegExp([
  "`([^`\\n]+)`",                                        // 1 code
  "\\[\\[([^\\]\\n|]+)(?:\\|([^\\]\\n]+))?\\]\\]",       // 2 3 [[target|label]]
  "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)",     // 4 5 [text](url)
  "\\*\\*(?=\\S)([^\\n]+?)(?<=\\S)\\*\\*",               // 6 **bold**
  "(?<![\\w*])\\*(?=[^\\s*])([^*\\n]+?)(?<=[^\\s*])\\*(?![\\w*])", // 7 *italic*
  "(?<![\\w_])__(?=\\S)([^\\n]+?)(?<=\\S)__(?![\\w_])",  // 8 __bold__
  "(?<![\\w_])_(?=[^\\s_])([^_\\n]+?)(?<=[^\\s_])_(?![\\w_])", // 9 _italic_
  "~~(?=\\S)([^\\n]+?)(?<=\\S)~~",                       // 10 ~~struck~~
  "(?<![\\w-])(\\d{4}-\\d{2}-\\d{2})(?![\\w-])",         // 11 a bare date
].join("|"), "g");

function mdInline(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(MD_INLINE)) {
    let node;
    if (m[1] != null) node = h("code", {}, m[1]);
    else if (m[2] != null) node = wikilink(m[2], m[3]);
    else if (m[4] != null) node = h("a", { href: m[5], target: "_blank", rel: "noopener noreferrer" }, mdInline(m[4]));
    else if (m[6] != null || m[8] != null) node = h("strong", {}, mdInline(m[6] ?? m[8]));
    else if (m[7] != null || m[9] != null) node = h("em", {}, mdInline(m[7] ?? m[9]));
    else if (m[10] != null) node = h("s", {}, mdInline(m[10]));
    else if (linkIndex.days.has(m[11])) node = h("a", { class: "cite day", href: href.entry(linkIndex.days.get(m[11])) }, m[11]);
    else continue;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(node);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const MD_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const MD_RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const MD_FENCE = /^\s{0,3}(```|~~~)/;
const MD_TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const mdCells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

// items at one indent become a list; deeper ones go inside the item above them
function mdList(items) {
  const base = items[0].indent;
  const list = h(items[0].ordered ? "ol" : "ul", {});
  let i = 0;
  while (i < items.length) {
    const li = h("li", {}, mdInline(items[i].text));
    let j = i + 1;
    while (j < items.length && items[j].indent > base) j++;
    if (j > i + 1) li.append(mdList(items.slice(i + 1, j)));
    list.append(li);
    i = j;
  }
  return list;
}

function md(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) out.push(h("p", {}, mdInline(para.join("\n"))));
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = MD_FENCE.exec(line);
    if (fence) {
      flush();
      const code = [];
      // a reply still being written may not have closed its fence yet
      for (i++; i < lines.length && !lines[i].trimStart().startsWith(fence[1]); i++) code.push(lines[i]);
      out.push(h("pre", {}, h("code", {}, code.join("\n"))));
    } else if (!line.trim()) {
      flush();
    } else if (MD_RULE.test(line)) {
      flush();
      out.push(h("hr", {}));
    } else if (/^\s{0,3}#{1,6}\s+\S/.test(line)) {
      flush();
      const [, marks, title] = /^\s*(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      // the page has its own h1 and h2, so a reply's headings start under them
      out.push(h(marks.length <= 2 ? "h3" : "h4", {}, mdInline(title)));
    } else if (/^\s{0,3}>/.test(line)) {
      flush();
      const quoted = [];
      for (; i < lines.length && /^\s{0,3}>/.test(lines[i]); i++) quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
      i--;
      out.push(h("blockquote", {}, md(quoted.join("\n"))));
    } else if (MD_ITEM.test(line)) {
      flush();
      const items = [];
      for (; i < lines.length; i++) {
        const item = MD_ITEM.exec(lines[i]);
        const next = item && { indent: item[1].replace(/\t/g, "    ").length, ordered: /\d/.test(item[2]), text: item[3] };
        // bullets after numbers, or the other way round, are a new list
        if (next && items.length && next.indent <= items[0].indent && next.ordered !== items[0].ordered) break;
        if (next) items.push(next);
        else if (lines[i].trim() && /^\s/.test(lines[i])) items[items.length - 1].text += `\n${lines[i].trim()}`;
        else if (!lines[i].trim() && MD_ITEM.test(lines[i + 1] || "")) continue;
        else break;
      }
      i--;
      out.push(mdList(items));
    } else if (line.includes("|") && MD_TABLE_RULE.test(lines[i + 1] || "")) {
      flush();
      const head = mdCells(line);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(mdCells(lines[i]));
      i--;
      out.push(h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, mdInline(c))))),
        h("tbody", {}, rows.map((r) => h("tr", {}, head.map((_, k) => h("td", {}, mdInline(r[k] || "")))))))));
    } else {
      para.push(line.trim());
    }
  }
  flush();
  return out;
}

// for one line in a list, where a heading or a bullet would only be clutter
const mdPlain = (text) => String(text || "")
  .replace(/^\s{0,3}(#{1,6}|[-*+]|\d{1,9}[.)]|>)\s+/gm, "")
  .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2").replace(/\[\[([^\]]+)\]\]/g, "$1")
  .replace(/\*\*|__|~~|`/g, "").replace(/\s*\n+\s*/g, " ");

// ---------------------------------------------------------------- charts

// every month from the first to the last, so gaps in writing show as gaps
function monthSpan(months) {
  const first = toDate(`${months[0].month}-01`);
  const last = toDate(`${months[months.length - 1].month}-01`);
  const keys = [];
  for (let d = first; d <= last; d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1))) {
    keys.push(d.toISOString().slice(0, 7));
  }
  return keys;
}

const lastDayOf = (month) => {
  const [yy, mm] = month.split("-").map(Number);
  return new Date(Date.UTC(yy, mm, 0)).toISOString().slice(0, 10);
};

function monthlyMoodChart(months, baseline, scale) {
  const scored = months.filter((m) => m.avg_mood != null);
  if (!scored.length) return empty("No entry has a mood score yet.");
  const byKey = new Map(months.map((m) => [m.month, m]));
  const keys = monthSpan(months);
  const slot = 14;
  const width = Math.max(keys.length * slot, 320);
  const height = 170;
  const top = 12;
  const bottom = 24;
  const plot = height - top - bottom;
  const y = (v) => top + plot - (Math.max(0, v - 1) / Math.max(1, scale - 1)) * plot;

  const chart = svg("svg", {
    class: "chart", viewBox: `0 0 ${width + 20} ${height}`, role: "img",
    "aria-label": `Average mood by month, ${keys.length} months`,
  });
  // stretch to the column, but not so far that a short history turns into slabs
  chart.style.maxWidth = `${(width + 20) * 1.4}px`;
  chart.style.minWidth = `${Math.min(width + 20, 640)}px`;
  for (const v of [1, Math.round(scale / 2), scale]) {
    chart.append(svg("line", { class: "grid", x1: 0, x2: width, y1: y(v), y2: y(v) }),
      svg("text", { class: "axis", x: width + 4, y: y(v) + 4 }, String(v)));
  }
  keys.forEach((key, i) => {
    const x = i * slot;
    const m = byKey.get(key);
    if (key.endsWith("-01") || i === 0) {
      chart.append(svg("text", { class: "axis", x: x + 1, y: height - 6 }, key.slice(0, 4)));
    }
    if (!m || m.avg_mood == null) return;
    const barTop = y(m.avg_mood);
    const [yy, mm] = key.split("-").map(Number);
    const lastDay = new Date(Date.UTC(yy, mm, 0)).toISOString().slice(0, 10);
    chart.append(svg("a", { href: href.entries({ from: `${key}-01`, to: lastDay }) },
      svg("title", {}, `${fmtMonth.format(toDate(`${key}-01`))}: mood ${fmtMood(m.avg_mood)}, ${plural(m.entry_count, "entry", "entries")}`),
      svg("rect", {
        class: `bar ${moodClass(m.avg_mood, scale)}`, x: x + 2, y: barTop,
        width: slot - 4, height: Math.max(2, top + plot - barTop), rx: 1.5,
      })));
  });
  if (baseline != null) {
    chart.append(
      svg("line", { class: "baseline", x1: 0, x2: width, y1: y(baseline), y2: y(baseline) }),
      svg("text", { class: "baseline-label", x: width, y: y(baseline) - 5, "text-anchor": "end" }, `your average ${fmtMood(baseline)}`));
  }
  return h("div", { class: "chart-wrap" }, chart);
}

// a number's average by month, laid out like monthlyMoodChart so the two line up
function metricChart(months, key, yesNo, tip) {
  if (!months.length) return empty(`No entry records ${key}.`);
  const byKey = new Map(months.map((m) => [m.month, m]));
  const keys = monthSpan(months);
  const values = months.map((m) => m.avg);
  // bars start at zero, so a month with half the value looks half as tall
  const lo = yesNo ? 0 : Math.min(0, ...values);
  const hi = yesNo ? 1 : Math.max(0, ...values);
  const slot = 14;
  const width = Math.max(keys.length * slot, 320);
  const height = 170;
  const top = 12;
  const bottom = 24;
  const plot = height - top - bottom;
  const y = (v) => top + plot - ((v - lo) / (hi - lo || 1)) * plot;

  const chart = svg("svg", {
    class: "chart", viewBox: `0 0 ${width + 20} ${height}`, role: "img",
    "aria-label": `${yesNo ? `Share of days with ${key}` : `Average ${key}`} by month, ${keys.length} months`,
  });
  chart.style.maxWidth = `${(width + 20) * 1.4}px`;
  chart.style.minWidth = `${Math.min(width + 20, 640)}px`;
  for (const v of [...new Set([lo, (lo + hi) / 2, hi])]) {
    chart.append(svg("line", { class: v === 0 && lo < 0 ? "zero" : "grid", x1: 0, x2: width, y1: y(v), y2: y(v) }),
      svg("text", { class: "axis", x: width + 4, y: y(v) + 4 }, fmtValue(v, yesNo)));
  }
  keys.forEach((month, i) => {
    const x = i * slot;
    const m = byKey.get(month);
    if (month.endsWith("-01") || i === 0) {
      chart.append(svg("text", { class: "axis", x: x + 1, y: height - 6 }, month.slice(0, 4)));
    }
    if (!m) return;
    const from = y(0);
    const to = y(m.avg);
    const what = yesNo ? `${key} on ${fmtValue(m.avg, true)} of days` : `${key} ${fmtValue(m.avg)}`;
    chart.append(svg("a", { href: href.entries({ from: `${month}-01`, to: lastDayOf(month) }) },
      svg("title", {}, tip ? tip(m) : `${fmtMonth.format(toDate(`${month}-01`))}: ${what}, ${plural(m.n, "entry", "entries")}`),
      svg("rect", {
        class: "bar metric", x: x + 2, y: Math.min(from, to),
        width: slot - 4, height: Math.max(2, Math.abs(to - from)), rx: 1.5,
      })));
  });
  return h("div", { class: "chart-wrap" }, chart);
}

function yearStrips(entries, scale) {
  const byYear = new Map();
  for (const e of entries) {
    const year = e.date.slice(0, 4);
    if (!byYear.has(year)) byYear.set(year, []);
    byYear.get(year).push(e);
  }
  const rows = [...byYear.keys()].sort().map((year) => {
    const list = byYear.get(year);
    const start = Date.UTC(Number(year), 0, 1);
    const days = (Date.UTC(Number(year) + 1, 0, 1) - start) / 86_400_000;
    const strip = svg("svg", {
      class: "strip", viewBox: `0 0 ${days} 30`, preserveAspectRatio: "none",
      role: "img", "aria-label": `${year}: ${plural(list.length, "entry", "entries")}`,
    });
    for (let month = 1; month < 12; month++) {
      const x = (Date.UTC(Number(year), month, 1) - start) / 86_400_000;
      strip.append(svg("line", { class: "month", x1: x, x2: x, y1: 0, y2: 30, "vector-effect": "non-scaling-stroke" }));
    }
    for (const e of list) {
      const x = (toDate(e.date).getTime() - start) / 86_400_000;
      const cls = moodClass(e.mood, scale);
      strip.append(svg("a", { href: href.entry(e.id) },
        svg("title", {}, `${longDate(e.date)} · mood ${fmtMood(e.mood)}${e.summary ? ` · ${e.summary}` : ""}`),
        svg("rect", { class: cls ? `day ${cls}` : "day none", x, y: 0, width: 1, height: 30 })));
    }
    return h("div", { class: "strip-row" },
      h("a", { class: "strip-year", href: href.calendar(year) }, year),
      strip,
      h("span", { class: "strip-n" }, plural(list.length, "entry", "entries")));
  });
  return h("div", { class: "strips" }, rows, moodLegend(scale));
}

// for colouring by amount: seven steps of one blue, from `lo` to `hi`
function amountLegend(lo, hi) {
  return h("div", { class: "strip-scale" },
    lo,
    h("span", { class: "legend", "aria-hidden": "true" }, [0, 1, 2, 3, 4, 5, 6].map((i) => h("span", { class: `q${i}` }))),
    hi);
}

const amountStep = (v, lo, hi) => `q${Math.round(Math.min(1, Math.max(0, (v - lo) / (hi - lo || 1))) * 6)}`;

function moodLegend(scale) {
  return h("div", { class: "strip-scale" },
    "low mood",
    h("span", { class: "legend", "aria-hidden": "true" }, [0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => h("span", { class: `m${i}` }))),
    `high mood (1–${scale})`);
}

// ---------------------------------------------------------------- views

async function overviewView() {
  const [o, cat] = await Promise.all([api("overview"), getCatalog(true)]);
  updateRail(o);
  document.title = "Commonplace";
  const c = o.counts;
  const w = o.writing;
  const scale = o.mood_scale;

  if (!c.entries && !c.notes) {
    return h("div", {},
      pageHead({ title: "Nothing here yet", lede: "Commonplace hasn't read your journal yet." }),
      empty("Reading your journal goes through each entry once and remembers it, so the first time is the long one."),
      upkeepButton("Read your journal", "index"));
  }

  const today = toDate(o.today);
  const input = h("input", {
    type: "text", placeholder: "Ask your journal anything", "aria-label": "Ask your journal",
    autocomplete: "off", maxlength: 2000,
  });
  const form = h("form", { class: "search-box ask-box", role: "search" },
    input, h("button", { class: "button primary", type: "submit" }, "Ask"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    location.hash = q ? href.ask(q) : "#/ask";
  });
  const lastMonth = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() - 1, 1));
  const prompts = [
    `How was ${fmtMonthLong.format(lastMonth)}?`,
    "What have I been writing about lately?",
    "Who did I see most this year?",
  ];

  const lede = h("p", { class: "lede" },
    h("strong", {}, plural(c.entries, "entry", "entries")),
    c.date_from ? [" since ", fmtMonth.format(toDate(c.date_from))] : null,
    ", ", plural(c.words, "word"), " in all, and ",
    h("strong", {}, plural(c.notes, "note")), " about the people, places and things you link to.");

  const PROBLEMS_SHOWN = 3;
  const problems = [
    ...o.errored_files.map((f) => h("div", { class: "notice" },
      h("span", { class: "notice-label" }, "Couldn't index"),
      h("span", {}, h("span", { class: "path" }, f.vault_path), " ", f.error))),
    ...o.future_entries.map((f) => h("div", { class: "notice info" },
      h("span", { class: "notice-label" }, "Dated in the future"),
      h("span", {}, futureLink(cat, f.date), " from ", h("span", { class: "path" }, f.vault_path),
        ". Usually a typo in the file name, and it throws off streaks."))),
  ];

  const facts = h("div", { class: "facts" },
    fact(fmtMood(o.baseline_mood), `average mood, from ${plural(o.scored_entries, "scored entry", "scored entries")}`, href.tags()),
    fact(fmtNum(w.longest_streak), `longest streak in days${w.longest_streak_end ? `, to ${mediumDate(w.longest_streak_end)}` : ""}`, href.calendar(w.longest_streak_end?.slice(0, 4))),
    fact(fmtNum(c.tags), "tags", href.tags()),
    fact(fmtNum(c.links), "links from entries to notes", href.notes()),
    fact(fmtNum(c.rollups), "digests written", href.digests()),
    fact(fmtNum(c.files), `files tracked${c.errored_files ? `, ${c.errored_files} with errors` : ""}`, href.files()));

  const s = o.setup;
  const setup = h("dl", { class: "setup" },
    h("dt", {}, "Journal folder"), h("dd", { class: "path" }, s.vault),
    h("dt", {}, "Note folders"), h("dd", {},
      s.notes_paths.length ? s.notes_paths.join(", ") : s.other_files ? null : "none set",
      s.other_files ? `${s.notes_paths.length ? ", and " : ""}every other Markdown file` : null),
    h("dt", {}, "Note kinds"), h("dd", {}, o.note_kinds.length
      ? h("span", { class: "chips" }, o.note_kinds.map((k) => h("a", { class: "chip", href: href.notes({ kind: k.kind }) }, kindLabel(k.kind), h("span", { class: "n" }, fmtNum(k.n)))))
      : "none"),
    h("dt", {}, "Cache file"), h("dd", {}, h("span", { class: "path" }, s.db_path), ` (${bytes(s.db_bytes)}, format ${s.schema_version})`),
    h("dt", {}, "Reads entries with"), h("dd", {}, s.fast_model),
    h("dt", {}, "Writes digests with"), h("dd", {}, s.precise_model),
    h("dt", {}, "Meaning vectors"), h("dd", {}, s.embed_model, s.embed_dim ? `, ${s.embed_dim} dimensions` : ""),
    h("dt", {}, "Reranking"), h("dd", {}, s.reranking || "off"));

  const latest = cat.entries.slice(-5).reverse();
  return h("div", {},
    h("header", { class: "page-head home-head" },
      h("h1", { tabindex: "-1" }, fmtToday.format(today)),
      lede,
      h("div", { class: "home-ask" },
        form,
        h("div", { class: "prompts" }, "Try", prompts.map((q) => h("a", { class: "chip", href: href.ask(q) }, q))))),
    problems.length ? section("Needs a look", [
      problems.slice(0, PROBLEMS_SHOWN),
      problems.length > PROBLEMS_SHOWN
        ? h("p", { class: "section-foot" }, h("a", { href: href.files({ show: "problems" }) }, `All ${plural(o.errored_files.length, "file")} that couldn't be indexed`))
        : null,
    ]) : null,
    h("div", { class: "home-grid" },
      section("On this day", entryLines(o.on_this_day, scale, {
        emptyText: `Nothing written on ${fmtShort.format(today)} in other years.`,
      }), { note: `${fmtShort.format(today)} in other years` }),
      section("Lately", entryLines(latest, scale), { note: h("a", { href: href.entries() }, "All entries") })),
    cat.entries.length ? section("Every day you wrote", yearStrips(cat.entries, scale), {
      note: "one mark for each entry, coloured by its mood",
    }) : null,
    h("section", { class: "section" }, facts),
    section("Mood by month", monthlyMoodChart(o.monthly_mood, o.baseline_mood, scale), {
      note: "pick a bar to see that month's entries",
    }),
    h("details", { class: "about" }, h("summary", {}, "How it's set up"), setup));
}

function futureLink(cat, iso) {
  const found = cat.entries.find((e) => e.date === iso);
  return found ? h("a", { href: href.entry(found.id) }, longDate(iso)) : longDate(iso);
}

function fact(value, label, link) {
  return h(link ? "a" : "div", { class: "fact", href: link },
    h("div", { class: "fact-value" }, value), h("div", { class: "fact-label" }, label));
}

// past this share of days, a heavy day doesn't stretch the colours for everything else
const WORDS_TOP_QUANTILE = 0.95;

// How the calendar colours a day other than by mood: words written, or a number
// you keep. Null when `by` names nothing there is.
async function calendarAmount(by, byDate, metrics) {
  if (by === "words") {
    const totals = new Map([...byDate].map(([iso, list]) => [iso, list.reduce((n, e) => n + (e.words || 0), 0)]));
    const sorted = [...totals.values()].sort((a, b) => a - b);
    const hi = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * WORDS_TOP_QUANTILE))] || 1;
    return {
      label: "words", value: (iso) => totals.get(iso) ?? null, lo: 0, hi,
      fmt: (v) => plural(v, "word"),
      legend: amountLegend("fewer words", `${fmtNum(hi)} or more`),
      lede: "Every day you wrote, coloured by how many words: lighter is fewer, darker is more.",
    };
  }
  const m = metrics.find((x) => x.key === by);
  if (!m) return null;
  const d = await api(`metrics/${encodeURIComponent(m.key)}`);
  const values = new Map(d.days.map((x) => [x.date, x.value]));
  const lo = d.yes_no ? 0 : d.lo;
  const hi = d.yes_no ? 1 : d.hi;
  return {
    label: m.key, value: (iso) => values.get(iso) ?? null, lo, hi,
    fmt: (v) => (d.yes_no ? (v >= 0.5 ? `${m.key}: yes` : `${m.key}: no`) : `${m.key} ${fmtValue(v)}`),
    legend: d.yes_no ? amountLegend("no", "yes") : amountLegend(fmtValue(lo), `${fmtValue(hi)} ${m.key}`),
    lede: `Every day you wrote, coloured by your ${m.key}: lighter is ${d.yes_no ? "no" : "less"}, darker is ${d.yes_no ? "yes" : "more"}. Dashed squares are days you wrote without recording it.`,
  };
}

async function calendarView([yearParam], params) {
  const [cat, metrics] = await Promise.all([getCatalog(), api("metrics").then((d) => d.metrics).catch(() => [])]);
  const scale = cat.mood_scale;
  document.title = "Calendar · Commonplace";
  const byDate = new Map();
  for (const e of cat.entries) {
    if (!byDate.has(e.date)) byDate.set(e.date, []);
    byDate.get(e.date).push(e);
  }
  const years = [...new Set(cat.entries.map((e) => e.date.slice(0, 4)))].sort();
  if (!years.length) {
    return h("div", {}, pageHead({ title: "Calendar" }), empty("No entries are indexed yet."));
  }
  const year = years.includes(yearParam) ? yearParam : years[years.length - 1];
  const inYear = cat.entries.filter((e) => e.date.startsWith(year));
  const todayIso = localToday();
  const wanted = params.get("by") || "";
  const amount = wanted ? await calendarAmount(wanted, byDate, metrics) : null;
  const by = amount ? wanted : "";

  const preview = h("aside", { class: "preview", "aria-live": "polite" });
  const showPreview = (iso) => {
    const list = byDate.get(iso) || [];
    const value = amount ? amount.value(iso) : null;
    fill(preview,
      h("div", { class: "preview-date" }, longDate(iso)),
      amount && list.length && h("div", { class: "meta-line" }, value == null ? `No ${amount.label} recorded` : amount.fmt(value)),
      list.length
        ? list.map((e) => h("div", {},
          h("div", { class: "meta-line" }, moodChip(e.mood, scale), plural(e.words, "word")),
          h("p", { class: "preview-summary" }, e.summary || "No summary stored")))
        : h("p", { class: "quiet" }, "Nothing written this day."));
  };
  const resetPreview = () => fill(preview,
    h("div", { class: "preview-date" }, year),
    h("p", { class: "preview-summary" }, `${plural(inYear.length, "entry", "entries")} this year.`),
    h("p", { class: "quiet" }, "Point at a day to read what the app took from it, or click to open it."));
  resetPreview();

  // weeks run Monday to Sunday; pad the first column back to Monday
  const jan1 = Date.UTC(Number(year), 0, 1);
  const lead = (new Date(jan1).getUTCDay() + 6) % 7;
  const days = (Date.UTC(Number(year) + 1, 0, 1) - jan1) / 86_400_000;
  const grid = h("div", { class: "cal-grid" });
  const months = h("div", { class: "cal-months", "aria-hidden": "true" });
  for (let i = 0; i < lead; i++) grid.append(h("span", { class: "cell pad" }));
  for (let i = 0; i < days; i++) {
    const d = new Date(jan1 + i * 86_400_000);
    const iso = d.toISOString().slice(0, 10);
    if (d.getUTCDate() === 1) {
      const col = Math.floor((lead + i) / 7) + 1;
      months.append(h("span", { "data-col": col }, fmtMonthShort.format(d)));
    }
    const list = byDate.get(iso);
    if (!list) {
      grid.append(h("span", { class: iso === todayIso ? "cell today" : "cell", title: longDate(iso) }));
      continue;
    }
    let cls;
    let label;
    if (amount) {
      const value = amount.value(iso);
      cls = value == null ? "cell has nomood" : `cell has ${amountStep(value, amount.lo, amount.hi)}`;
      label = `${longDate(iso)}, ${value == null ? `no ${amount.label} recorded` : amount.fmt(value)}`;
    } else {
      const mood = list.reduce((sum, e) => sum + (e.mood ?? 0), 0) / (list.filter((e) => e.mood != null).length || 1);
      const hasMood = list.some((e) => e.mood != null);
      cls = hasMood ? `cell has ${moodClass(mood, scale)}` : "cell has nomood";
      label = `${longDate(iso)}, mood ${hasMood ? fmtMood(mood) : "none"}`;
    }
    const cell = h("a", {
      class: iso === todayIso ? `${cls} today` : cls,
      href: href.entry(list[0].id),
      "aria-label": label,
      onmouseenter: () => showPreview(iso),
      onfocus: () => showPreview(iso),
    });
    grid.append(cell);
  }
  grid.addEventListener("mouseleave", resetPreview);
  // month labels sit over the week column holding the 1st
  for (const span of months.children) span.style.gridColumn = `${span.dataset.col} / span 4`;

  const cal = h("div", { class: "cal" },
    months,
    h("div", { class: "cal-days", "aria-hidden": "true" }, ["Mon", "", "Wed", "", "Fri", "", "Sun"].map((d) => h("span", {}, d))),
    grid);

  const choose = h("select", {},
    h("option", { value: "", selected: !by }, "Mood"),
    h("option", { value: "words", selected: by === "words" }, "Words written"),
    metrics.map((m) => h("option", { value: m.key, selected: by === m.key }, m.key)));
  choose.addEventListener("change", () => { location.hash = href.calendar(year, choose.value); });

  return h("div", {},
    pageHead({
      eyebrow: "Calendar", title: year,
      lede: amount ? amount.lede : "Every day you wrote, coloured by the mood the app gave that entry. Dashed squares are entries without a mood score.",
      meta: [h("a", { href: href.review(year) }, `${year} in review`), h("a", { href: href.compare(year) }, `${year} against ${Number(year) - 1}`)],
    }),
    h("nav", { class: "year-tabs", "aria-label": "Years" }, years.map((y) =>
      h("a", { class: y === year ? "chip active" : "chip", href: href.calendar(y, by), "aria-current": y === year ? "page" : null },
        y, h("span", { class: "n" }, fmtNum(cat.entries.filter((e) => e.date.startsWith(y)).length))))),
    h("div", { class: "colour-by" }, field("Colour by", choose),
      wanted && !amount ? h("span", { class: "quiet" }, `No entry records ${wanted}, so this is by mood.`) : null),
    h("div", { class: "cal-scroll" }, cal),
    amount ? amount.legend : moodLegend(scale),
    preview,
    section(`Entries in ${year}`, entryLines([...inYear].reverse(), scale), {
      note: h("a", { href: href.entries({ from: `${year}-01-01`, to: `${year}-12-31` }) }, "filter these"),
    }));
}

async function entriesView(_, params) {
  const cat = await getCatalog();
  const scale = cat.mood_scale;
  document.title = "Entries · Commonplace";
  const state = {
    q: params.get("q") || "", tag: params.get("tag") || "", note: params.get("note") || "",
    group: params.get("group") || "", from: params.get("from") || "", to: params.get("to") || "",
    order: params.get("order") || "newest", page: Number(params.get("page")) || 1,
  };
  const pageSize = 50;
  const results = h("div", {});
  const heading = h("h1", { tabindex: "-1" }, "Entries");

  const text = h("input", { type: "search", value: state.q, placeholder: "Words in the entry or its summary" });
  const tagSelect = h("select", {},
    h("option", { value: "" }, "Any tag"),
    cat.tags.map((t) => h("option", { value: t.name, selected: t.name === state.tag }, `${t.name} (${t.n})`)));
  const from = h("input", { type: "date", value: state.from });
  const to = h("input", { type: "date", value: state.to });
  const order = h("select", {},
    h("option", { value: "newest", selected: state.order === "newest" }, "Newest first"),
    h("option", { value: "oldest", selected: state.order === "oldest" }, "Oldest first"));
  const noteSlot = h("div", { class: "field" });
  const groupSlot = h("div", { class: "field" });
  const clear = h("button", { class: "button", type: "button" }, "Clear filters");
  const askSlot = h("div", {});

  let token = 0;
  async function load() {
    const mine = ++token;
    replaceParams("/entries", { ...state, order: state.order === "newest" ? "" : state.order, page: state.page > 1 ? state.page : "" });
    const data = await api("entries", {
      q: state.q, tag: state.tag, note: state.note, group: state.group, from: state.from, to: state.to,
      order: state.order, limit: pageSize, offset: (state.page - 1) * pageSize,
    }).catch((err) => ({ error: err }));
    if (mine !== token) return;
    if (data.error) {
      fill(results, h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't filter"), data.error.message));
      return;
    }
    fill(noteSlot, data.note
      ? [h("span", {}, "Linking to"), h("span", { class: "chip pencil" }, data.note.title,
        h("button", { type: "button", "aria-label": `Stop filtering by ${data.note.title}`, onclick: () => { state.note = ""; state.page = 1; load(); } }, "×"))]
      : []);
    fill(groupSlot, data.group
      ? [h("span", {}, "In the group"), h("span", { class: "chip pencil" }, `${data.group.key}: ${data.group.value}`,
        h("span", { class: "n" }, plural(data.group.notes, "note")),
        h("button", { type: "button", "aria-label": `Stop filtering by ${data.group.key} ${data.group.value}`, onclick: () => { state.group = ""; state.page = 1; load(); } }, "×"))]
      : []);
    const pages = Math.max(1, Math.ceil(data.total / pageSize));
    const filtered = state.q || state.tag || state.note || state.group || state.from || state.to;
    const scope = scopeParams(state);
    fill(askSlot, data.total && Object.keys(scope).length
      ? askHere(scope, "What happened on these days?", state.q
        ? "Answers read the entries the other filters keep. The words filter isn't used for asking."
        : "Answers only read the entries these filters keep.")
      : null);
    const go = (p) => () => { state.page = p; load(); window.scrollTo(0, 0); };
    fill(results,
      h("p", { class: "result-count" }, filtered
        ? `${plural(data.total, "entry", "entries")} match`
        : `${plural(data.total, "entry", "entries")} in the cache`,
        pages > 1 ? `, page ${state.page} of ${pages}` : ""),
      data.total ? h("p", { class: "section-note downloads" }, "Save them as ",
        [["csv", "a spreadsheet (CSV)"], ["markdown", "Markdown"], ["json", "JSON"]].map(([format, label], i) => [
          i ? ", " : "",
          h("a", { href: `/api/export${query({ format, tag: state.tag, note: state.note, group: state.group, from: state.from, to: state.to })}`, download: "" }, label),
        ]), ".", state.q ? " The words filter isn't applied to the file." : null) : null,
      entryLines(data.items, scale, { emptyText: "No entries match these filters." }),
      pages > 1 && h("div", { class: "pager-nav" },
        h("button", { class: "button", type: "button", disabled: state.page <= 1, onclick: go(state.page - 1) }, "Previous page"),
        h("button", { class: "button", type: "button", disabled: state.page >= pages, onclick: go(state.page + 1) }, "Next page")));
  }

  const update = (key, el) => () => { state[key] = el.value; state.page = 1; load(); };
  text.addEventListener("input", debounce(update("q", text), 250));
  tagSelect.addEventListener("change", update("tag", tagSelect));
  from.addEventListener("change", update("from", from));
  to.addEventListener("change", update("to", to));
  order.addEventListener("change", update("order", order));
  clear.addEventListener("click", () => {
    Object.assign(state, { q: "", tag: "", note: "", group: "", from: "", to: "", page: 1 });
    text.value = ""; tagSelect.value = ""; from.value = ""; to.value = "";
    load();
  });

  load();
  return h("div", {},
    h("header", { class: "page-head" }, h("p", { class: "eyebrow" }, "Every dated entry in the cache"), heading),
    h("div", { class: "filters", role: "search" },
      field("Contains", text, true), field("Tag", tagSelect), field("From", from), field("To", to),
      field("Order", order), noteSlot, groupSlot, clear),
    results,
    askSlot);
}

async function entryView([id], params) {
  const at = params.get("at");
  const e = await api(`entries/${id}`, { at });
  const scale = e.mood_scale;
  document.title = `${e.date} · Commonplace`;
  currentNeighbours = { prev: e.prev && href.entry(e.prev.id), next: e.next && href.entry(e.next.id) };

  const moodClassName = moodClass(e.mood, scale);
  const modelMood = e.model_reading ? e.model_reading.mood : null;
  const moodGloss = gloss("Mood",
    e.mood == null
      ? h("span", { class: "quiet" }, "No mood score")
      : [h("div", { class: "mood-big" }, h("span", { class: "num" }, fmtMood(e.mood)), h("span", { class: "of" }, `of ${scale}`)),
        h("div", { class: `scale ${moodClassName}`, "aria-hidden": "true" },
          Array.from({ length: scale }, (_, i) => h("span", { class: i < Math.round(e.mood) ? "on" : null })))],
    e.mood_from_frontmatter && h("div", { class: "quiet" },
      "Your own score, from the file's frontmatter. ",
      modelMood == null ? "The model gave none." : `The model read it as ${fmtMood(modelMood)}.`));
  const fmTags = new Set(e.frontmatter_tags || []);

  const margin = h("aside", { class: "margin", "aria-label": "What the app took from this entry" },
    h("p", { class: "margin-title" }, "The app's reading"),
    moodGloss,
    gloss("Summary", e.summary ? h("p", { class: "gloss-summary" }, e.summary) : h("span", { class: "quiet" }, "No summary stored")),
    e.details && gloss("In more detail", h("p", { class: "gloss-summary" }, e.details),
      h("div", { class: "quiet" }, "Written for long entries. Answers and digests read this one when there's room.")),
    gloss("Tags",
      e.tags.length ? h("div", { class: "chips" }, e.tags.map((t) => h("a", { class: "chip pencil", href: href.tag(t), title: fmTags.has(t) ? "From the frontmatter" : null }, t))) : h("span", { class: "quiet" }, "No tags"),
      e.tags.some((t) => fmTags.has(t)) && h("div", { class: "quiet" }, "Includes the tags in the file's frontmatter.")),
    Object.keys(e.metrics || {}).length > 0 && gloss("Your numbers",
      h("dl", { class: "props numbers" }, Object.entries(e.metrics).map(([k, v]) => [h("dt", {}, h("a", { href: href.metric(k) }, k)), h("dd", {}, fmtMood(v))])),
      h("div", { class: "quiet" }, "From the file's frontmatter")),
    gloss("Mentions",
      e.notes.length ? h("div", { class: "chips" }, e.notes.map((n) => [
        h("a", { class: "chip pencil", href: href.note(n.id), title: kindLabel(n.kind) }, n.title),
        n.sections.map((s) => sectionChip(n.title, href.note(n.id, { at: s.anchor }), s.label, `The part of ${n.title} this entry links to`)),
      ])) : h("span", { class: "quiet" }, "Links to no indexed note"),
      e.unresolved_links.length ? h("div", { class: "quiet" },
        h("div", { class: "gloss-label" }, "Links with no note (not counted)"),
        h("div", { class: "chips" }, e.unresolved_links.map((t) => h("span", { class: "chip ghost" }, t)))) : null),
    (e.links_to_days.length || e.linked_from_days.length) && gloss("Other days",
      e.links_to_days.length ? [h("div", { class: "quiet" }, "This entry links to"), dayChips(e.links_to_days, true)] : null,
      e.linked_from_days.length ? [h("div", { class: "quiet" }, "Linked from"), dayChips(e.linked_from_days, false)] : null),
    e.linked_from_notes.length > 0 && gloss("Notes that link this day",
      h("div", { class: "chips" }, e.linked_from_notes.map((n) => h("a", { class: "chip pencil", href: href.note(n.id), title: kindLabel(n.kind) }, n.title)))),
    e.parts.length > 0 && gloss("Searched in parts",
      h("div", { class: "chips" }, e.parts.map((p) => p.heading
        ? h("a", { class: "chip pencil", href: href.entry(e.id, { at: p.heading }) }, p.heading)
        : h("span", { class: "chip ghost" }, "before the first heading"))),
      h("div", { class: "quiet" }, e.parts.every((p) => p.embedded)
        ? "Each part has its own meaning vector, so a search can find it on its own"
        : "Searched by keywords part by part; some parts have no meaning vector yet")),
    gloss("Read by",
      h("div", {}, e.model_used || "unknown model"),
      h("div", { class: "quiet" }, stamp(e.extracted_at)),
      h("div", { class: "quiet" }, e.embedded ? "Has a meaning vector, so it turns up in meaning search" : "No meaning vector yet, so meaning search can't find it")),
    e.file && gloss("File",
      h("div", {}, e.file.status === "indexed" ? "Indexed" : kindLabel(e.file.status), " ", h("span", { class: "quiet" }, stamp(e.file.last_indexed))),
      e.file.error && h("div", {}, e.file.error)));

  const digestRows = h("div", { class: "digest-rows" }, e.digests.map((d) =>
    h("div", { class: "digest-row" },
      h("span", { class: "type" }, KIND_LABELS[d.type]),
      h("div", {},
        h("a", { href: href.digest(d.type, d.key) }, periodTitle(d.type, d.key)),
        d.stale && h("span", { class: "status pending" }, " out of date"),
        h("div", { class: d.summary ? "text clamp" : "text" }, d.summary || (d.exists ? "Empty digest" : "Not written yet. Run journal rollup."))))));

  const node = h("div", {},
    pageHead({
      eyebrow: h("span", {}, h("a", { href: href.calendar(e.date.slice(0, 4)) }, e.date.slice(0, 4)), ` · ${fmtWeekday.format(toDate(e.date))}`),
      title: fmtDayMonthYear.format(toDate(e.date)),
      actions: [
        button("← Earlier", e.prev && href.entry(e.prev.id), { disabled: !e.prev, key: "k", title: e.prev ? longDate(e.prev.date) : "This is the first entry" }),
        button("Later →", e.next && href.entry(e.next.id), { disabled: !e.next, key: "j", title: e.next ? longDate(e.next.date) : "This is the latest entry" }),
      ],
      meta: [
        h("span", { class: "path" }, e.vault_path),
        h("span", {}, plural(e.words, "word")),
        h("a", { href: e.obsidian_url }, "Open in Obsidian"),
      ],
    }),
    focusNotice(e.focus, "entry"),
    h("div", { class: "folio" },
      h("article", {},
        h("p", { class: "reading-label" }, e.focus && e.focus.lines
          ? `Your entry, as the app stored it. The “${e.focus.label}” part a link points at is marked.`
          : "Your entry, as you wrote it. The models read the same text without the formatting."),
        rawText(e.raw_text, e.focus, "The entry has no text."),
        e.photos.length ? h("div", { class: "photos" },
          h("p", { class: "reading-label" }, "Its photos, as the vision model described them"),
          h("dl", { class: "props" }, e.photos.map((p) => [
            h("dt", { class: "path", title: p.path }, p.name), h("dd", {}, p.description)]))) : null),
      margin),
    section("Close in meaning", entryLines(e.similar, scale, {
      emptyText: e.embedded ? "No other entries have vectors yet." : "This entry has no vector yet, so there's nothing to compare.",
    }), { note: "nearest by stored vector" }),
    section("Digests covering this day", digestRows));
  showFocus(node);
  return node;
}

let currentNeighbours = null;

// `outgoing` days are ones this entry links to, so a part opens at that part;
// the others link here, and their parts are of this day
function dayChips(days, outgoing) {
  return h("div", { class: "chips" }, days.map((d) => d.sections.length
    ? d.sections.map((s) => outgoing
      ? sectionChip(mediumDate(d.date), href.entry(d.id, { at: s.anchor }), s.label, `The part of ${longDate(d.date)} this entry links to`)
      : sectionChip(mediumDate(d.date), href.entry(d.id), s.label, `Links to the “${s.label}” part of this day`))
    : h("a", { class: "chip pencil", href: href.entry(d.id) }, mediumDate(d.date))));
}

async function notesView(_, params) {
  const data = await api("notes");
  document.title = "Notes · Commonplace";
  const state = { kind: params.get("kind") || "", q: params.get("q") || "" };
  const body = h("div", {});
  const kinds = h("div", { class: "chips" });
  const filter = h("input", { type: "search", value: state.q, placeholder: "Title or any name for it" });

  function render() {
    replaceParams("/notes", state);
    fill(kinds,
      h("button", { class: state.kind ? "chip" : "chip active", type: "button", onclick: () => { state.kind = ""; render(); } }, "All", h("span", { class: "n" }, fmtNum(data.notes.length))),
      data.kinds.map((k) => h("button", {
        class: state.kind === k.kind ? "chip active" : "chip", type: "button",
        onclick: () => { state.kind = k.kind; render(); },
      }, kindLabel(k.kind), h("span", { class: "n" }, fmtNum(k.n)))));
    const q = state.q.trim().toLowerCase();
    const shown = data.notes.filter((n) =>
      (!state.kind || n.kind === state.kind) &&
      (!q || [n.title, ...n.aliases, ...n.learned].some((a) => a.toLowerCase().includes(q))));
    const groups = new Map(data.kinds.map((k) => [k.kind, []]));
    for (const n of shown) {
      if (!groups.has(n.kind)) groups.set(n.kind, []);
      groups.get(n.kind).push(n);
    }
    fill(body,
      shown.length ? null : empty("No notes match."),
      [...groups].filter(([, notes]) => notes.length).map(([kind, notes]) => h("section", { class: "kind-group" },
        h("div", { class: "section-head" }, h("h2", {}, kindLabel(kind)), h("span", { class: "section-note" }, plural(notes.length, "note"))),
        h("div", { class: "note-grid" }, notes.map((n) => h("a", { class: "note-card", href: href.note(n.id) },
          h("div", { class: "note-title" }, n.title),
          n.aliases.length ? h("div", { class: "note-aliases" }, `also ${n.aliases.join(", ")}`) : null,
          n.summary ? h("div", { class: "note-summary" }, n.summary) : null,
          h("div", { class: n.mentions ? "note-meta" : "note-meta none" }, n.mentions
            ? `Linked from ${plural(n.mentions, "entry", "entries")}, last ${mediumDate(n.last)}`
            : "No entry links here")))))));
  }
  filter.addEventListener("input", debounce(() => { state.q = filter.value; render(); }, 120));
  render();

  const unresolved = data.unresolved_links.length
    ? section("Links that find nothing", h("div", { class: "table-wrap" }, h("table", { class: "data links" },
      h("thead", {}, h("tr", {},
        h("th", {}, "Link"), h("th", { class: "num" }, "Entries"), h("th", { class: "num" }, "Notes"),
        h("th", { class: "num" }, "Last linked"), h("th", {}, "Why, and what to do"))),
      h("tbody", {}, data.unresolved_links.map((l) => h("tr", {},
        h("td", {}, h("span", { class: "chip ghost" }, l.target)),
        h("td", { class: "num" }, fmtNum(l.entries)),
        h("td", { class: "num" }, fmtNum(l.notes)),
        h("td", { class: "num" }, l.last ? mediumDate(l.last) : "–"),
        h("td", {}, l.why, h("div", { class: "fix" },
          l.like_id != null ? ["Did you mean ", h("a", { href: href.note(l.like_id) }, l.like), "?"] : l.fix))))))), {
      intro: ["These are linked from your entries or notes, but nothing in the cache answers to them, so they don't count as mentions. ",
        h("code", {}, "journal links"), " also looks at the files in your vault, so it can tell when a file is only outside your note folders."],
      note: data.unresolved_total > data.unresolved_links.length
        ? `most linked ${data.unresolved_links.length} of ${fmtNum(data.unresolved_total)}`
        : "most linked first",
    })
    : null;

  const rule = data.gone_quiet_rule;
  const quiet = data.gone_quiet.length
    ? section("Gone quiet", h("div", { class: "chips" }, data.gone_quiet.map((n) =>
      h("a", { class: "chip", href: href.note(n.id), title: `${plural(n.n, "entry", "entries")}, last ${mediumDate(n.last)}` },
        n.title, h("span", { class: "n" }, `last ${mediumDate(n.last)}`)))), {
      intro: `Linked from ${rule.min} or more entries, but not in the ${rule.months} months before your latest one. journal lapsed lists them with more detail.`,
      note: "most linked first",
    })
    : null;

  const groups = data.groups.length
    ? section("Groups", data.groups.map((g) => h("div", { class: "group-row" },
      h("h3", {}, g.name),
      h("div", { class: "chips" }, g.values.map((v) => h("a", {
        class: "chip", href: groupLink(g.key, v.value),
        title: `${plural(v.notes, "note")}, linked from ${plural(v.entries, "entry", "entries")}`,
      }, v.value, h("span", { class: "n" }, fmtNum(v.notes))))))), {
      intro: ["Notes that share a property value, like Relationship: Family. Pick one to see the entries that link any of its notes. In the terminal, ",
        h("code", {}, "journal entities --by"), " and ", h("code", {}, "--group"), " do the same."],
      note: "number of notes",
    })
    : null;

  return h("div", {},
    pageHead({
      eyebrow: "Reference notes",
      title: "Notes",
      lede: "The undated notes about people, places and topics. A note's kind is the folder it sits in. Mentions come only from links in your entries.",
      meta: [h("a", { href: href.map() }, "See them as a map")],
    }),
    h("div", { class: "filters", role: "search" }, field("Filter", filter, true), h("div", { class: "field" }, h("span", {}, "Kind"), kinds)),
    body,
    groups,
    quiet,
    looseNotes(),
    unresolved);
}

function looseNotes() {
  const slot = h("div", {});
  api("unlinked").then((data) => {
    if (!data.notes.length) return;
    fill(slot, section("Named without a link", h("div", { class: "chips" }, data.notes.map((n) =>
      h("a", { class: "chip", href: href.note(n.note_id), title: `Last on ${mediumDate(n.latest)}` }, n.title, h("span", { class: "n" }, fmtNum(n.count))))), {
      note: data.total > data.notes.length ? `${fmtNum(data.notes.length)} of ${fmtNum(data.total)}` : plural(data.total, "note"),
      intro: "Entries that say one of these names without linking it, so they're missing from its numbers. Each note's page shows the sentences, to add the links in Obsidian.",
    }));
  }).catch(() => {});
  return slot;
}

async function noteView([id], params) {
  const n = await api(`notes/${id}`, { at: params.get("at") });
  const scale = n.mood_scale;
  document.title = `${n.title} · Commonplace`;
  const maxYear = Math.max(1, ...n.by_year.map((y) => y.n));

  const summaryNote = n.summary_source === "model"
    ? `Summarised by ${n.model_used}`
    : n.summary_source === "own_text" ? "Short enough to be its own summary" : null;

  const margin = h("aside", { class: "margin", "aria-label": "What the app knows about this note" },
    h("p", { class: "margin-title" }, "The app's reading"),
    gloss("Summary",
      n.summary ? h("p", { class: "gloss-summary" }, n.summary) : h("span", { class: "quiet" }, "No summary, the note is empty"),
      summaryNote && h("div", { class: "quiet" }, summaryNote)),
    gloss("Mentions",
      n.mentions
        ? [h("div", {}, `Linked from ${plural(n.mentions.count, "entry", "entries")}`),
          h("div", { class: "quiet" }, `${mediumDate(n.mentions.first)} to ${mediumDate(n.mentions.last)}`)]
        : h("span", { class: "quiet" }, "No indexed entry links here")),
    n.by_year.length > 0 && gloss("By year", h("div", { class: "year-bars" }, n.by_year.map((y) => [
      h("span", {}, y.year),
      h("span", { class: "hbar", "data-w": y.n / maxYear }),
      h("span", {}, fmtNum(y.n)),
    ]))),
    n.avg_mood != null && gloss("Mood on those days", moodChip(n.avg_mood, scale), " ", delta(n.avg_mood, n.baseline_mood)),
    gloss("Also called", n.aliases.length ? n.aliases.join(", ") : h("span", { class: "quiet" }, "No aliases"),
      n.learned_names.length ? [
        h("div", { class: "gloss-label" }, "How you write it in links"),
        h("div", {}, n.learned_names.map((l) => `${l.name} (${plural(l.entries, "entry", "entries")})`).join(", ")),
        h("div", { class: "quiet" }, "Questions, filters and “named without a link” match these too."),
      ] : null),
    gloss("Indexed", h("div", { class: "quiet" }, stamp(n.extracted_at)),
      h("div", { class: "quiet" }, n.embedded ? "Has a meaning vector" : "No meaning vector yet")));

  const props = Object.entries(n.properties);
  const valuesByKey = new Map();
  for (const p of n.property_groups) {
    if (!valuesByKey.has(p.name)) valuesByKey.set(p.name, []);
    valuesByKey.get(p.name).push(p);
  }
  const propValue = (k, v) => valuesByKey.has(k)
    ? h("span", { class: "chips" }, valuesByKey.get(k).map((p) => h("a", {
      class: "chip", href: groupLink(p.key, p.value),
      title: p.notes > 1 ? `${plural(p.notes, "note")} share this; see the entries that link them` : "See the entries that link notes with this value",
    }, p.value, p.notes > 1 ? h("span", { class: "n" }, fmtNum(p.notes)) : null)))
    : v;
  const qualifies = n.digest_min_mentions > 0 && n.mentions && n.mentions.count >= n.digest_min_mentions;
  const overYears = n.digest
    ? section("Over the years", [
      h("div", { class: "digest-summary md" }, md(n.digest.summary || "The digest is empty.")),
      n.digest.stale && h("p", { class: "turn-meta" }, h("span", { class: "status pending" }, "out of date"),
        " An entry that links here changed since it was written. The next journal rollup rewrites it."),
    ], { note: `written ${stamp(n.digest.generated_at)} from ${plural(n.digest.entries, "entry", "entries")}` })
    : qualifies
      ? section("Over the years", empty("Not written yet. ", h("code", {}, "journal rollup"),
        ` writes one for every note linked from ${n.digest_min_mentions} or more entries.`))
      : null;
  const node = h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.notes({ kind: n.kind }) }, kindLabel(n.kind)),
      title: n.title,
      meta: [h("span", { class: "path" }, n.vault_path), h("span", {}, plural(n.words, "word")), h("a", { href: n.obsidian_url }, "Open in Obsidian")],
    }),
    focusNotice(n.focus, "note"),
    h("div", { class: "folio" },
      h("article", {},
        props.length ? h("dl", { class: "props" }, props.map(([k, v]) => [h("dt", {}, k), h("dd", {}, propValue(k, v))])) : null,
        h("p", { class: "reading-label" }, n.focus && n.focus.lines
          ? `The note, as the app stored it. The “${n.focus.label}” part a link points at is marked.`
          : "The note, as you wrote it"),
        rawText(n.raw_text, n.focus, "This note has no text of its own.")),
      margin),
    overYears,
    askHere({ note: n.id }, `What did I write about ${n.title} last year?`,
      ["Answers only read the entries that link ", n.title, "."]),
    n.linked_with.length ? section("Often linked with", h("div", { class: "chips" }, n.linked_with.map((o) =>
      h("a", { class: "chip", href: href.note(o.id), title: `${kindLabel(o.kind)}, in ${plural(o.n, "entry", "entries")} with ${n.title}` },
        o.title, h("span", { class: "n" }, fmtNum(o.n))))), {
      note: "number of shared entries",
    }) : null,
    (n.outlinks.length || n.backlinks.length) ? section("Connected notes", h("div", { class: "two-col" },
      h("div", {}, h("h3", {}, "This note links to"), h("div", { class: "chips" }, n.outlinks.length ? n.outlinks.map((o) => noteChip(o)) : empty("Nothing."))),
      h("div", {}, h("h3", {}, "Notes that link here"), h("div", { class: "chips" }, n.backlinks.length ? n.backlinks.map((o) => noteChip(o)) : empty("Nothing."))))) : null,
    n.linked_days.length ? section("Days this note links to", entryLines(n.linked_days, scale), {
      note: plural(n.linked_days.length, "day"),
      intro: n.day_links_count
        ? "They count as days that mention this note, since day_links_count is on."
        : ["Its relationship digest reads them. They don't count as mentions unless you turn on ", h("code", {}, "day_links_count"), " under [notes]."],
    }) : null,
    section("Entries that link here", entryLines(n.entries, scale, { emptyText: "No indexed entry links to this note." }), {
      note: n.entries_capped ? `newest ${n.entries.length} shown` : n.entries.length ? h("a", { href: href.entries({ note: n.id }) }, "filter these") : null,
    }),
    n.unlinked.length ? section("Named without a link", h("ol", { class: "lines" }, n.unlinked.map((u) =>
      h("li", {}, h("a", { class: "line", href: href.entry(u.id) },
        h("span", { class: "line-date" }, mediumDate(u.date)),
        h("span", { "aria-hidden": "true" }),
        h("span", { class: "line-summary" }, u.snippet),
        h("span", { class: "line-tags" }, `as “${u.name}”`))))), {
      note: plural(n.unlinked.length, "entry", "entries"),
      intro: ["These entries name ", n.title, " but don't link it, so they're left out of the counts and moods above. Link them in Obsidian and they count from the next index."],
    }) : null);
  for (const bar of node.querySelectorAll(".hbar[data-w]")) bar.style.width = `${Number(bar.dataset.w) * 100}%`;
  showFocus(node);
  return node;
}

async function tagsView(_, params) {
  const data = await api("tags");
  const scale = data.mood_scale;
  document.title = "Tags · Commonplace";
  const state = { q: params.get("q") || "", sort: params.get("sort") || "count" };
  const maxN = Math.max(1, ...data.tags.map((t) => t.n));
  const body = h("tbody", {});
  const filter = h("input", { type: "search", value: state.q, placeholder: "Tag name" });
  const sort = h("select", {},
    [["count", "Most used"], ["name", "A to Z"], ["high", "Highest mood"], ["low", "Lowest mood"], ["recent", "Used most recently"]]
      .map(([v, label]) => h("option", { value: v, selected: state.sort === v }, label)));

  const sorters = {
    count: (a, b) => b.n - a.n || a.name.localeCompare(b.name),
    name: (a, b) => a.name.localeCompare(b.name),
    high: (a, b) => (b.avg_mood ?? -1) - (a.avg_mood ?? -1),
    low: (a, b) => (a.avg_mood ?? 99) - (b.avg_mood ?? 99),
    recent: (a, b) => b.last.localeCompare(a.last),
  };
  function render() {
    replaceParams("/tags", { q: state.q, sort: state.sort === "count" ? "" : state.sort });
    const q = state.q.trim().toLowerCase();
    const rows = data.tags.filter((t) => !q || t.name.toLowerCase().includes(q)).sort(sorters[state.sort] || sorters.count);
    fill(body, ...rows.map((t) => {
      const bar = h("span", { class: "hbar" });
      bar.style.width = `${(t.n / maxN) * 100}%`;
      return h("tr", {},
        h("td", {}, h("a", { href: href.tag(t.name) }, t.name)),
        h("td", { class: "num" }, fmtNum(t.n)),
        h("td", { class: "bar-cell" }, bar),
        h("td", {}, moodChip(t.avg_mood, scale)),
        h("td", {}, delta(t.avg_mood, data.baseline_mood)),
        h("td", { class: "num" }, mediumDate(t.last)));
    }));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: 6 }, empty("No tags match."))));
  }
  filter.addEventListener("input", debounce(() => { state.q = filter.value; render(); }, 120));
  sort.addEventListener("change", () => { state.sort = sort.value; render(); });
  render();

  return h("div", {},
    pageHead({
      eyebrow: "Themes the model picked",
      title: "Tags",
      lede: h("span", {}, "Each entry gets a few tags from the model, plus any #tags you wrote yourself. Mood is the average over entries with that tag; your overall average is ",
        h("strong", {}, fmtMood(data.baseline_mood)), "."),
    }),
    h("div", { class: "filters", role: "search" }, field("Filter", filter, true), field("Sort", sort)),
    data.tags.length
      ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "Tag"), h("th", { class: "num" }, "Entries"), h("th", {}, h("span", { class: "visually-hidden" }, "Share")), h("th", {}, "Average mood"), h("th", {}, "Compared with usual"), h("th", { class: "num" }, "Last used"))),
        body))
      : empty("No tags yet. They come with the first entries that are read."),
    similarTags());
}

// filled in once it arrives, so the tags don't wait on it
function similarTags() {
  const slot = h("div", {});
  const load = async () => {
    const data = await api("similar-tags").catch(() => null);
    if (!data || !data.pairs.length) return fill(slot);
    fill(slot, section("Might be the same tag", h("ul", { class: "upkeep-rows" }, data.pairs.map((p) => h("li", { class: "upkeep-row" },
      h("div", { class: "upkeep-what" },
        h("h3", {}, tagChip(p.tag, p.n), " looks like ", tagChip(p.like, p.like_n))),
      h("div", { class: "upkeep-do" }, h("button", { class: "button", type: "button", onclick: async () => {
        try {
          await post("settings/save", { values: { tags: { merge: { ...data.merge, [p.tag]: p.like } } } });
          toast(`${p.tag} counts as ${p.like} from the next time your files are read.`);
          load();
        } catch (err) {
          toast(err.message, "error");
        }
      } }, `Count as ${p.like}`))))), {
      intro: "Counting one as the other is saved to config.toml and applies the next time your files are read, without a model.",
    }));
  };
  load();
  return slot;
}

async function tagView([name]) {
  const t = await api(`tags/${encodeURIComponent(name)}`);
  const scale = t.mood_scale;
  document.title = `${t.name} · Commonplace`;
  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.tags() }, "Tag"),
      title: t.name,
      meta: [
        h("span", {}, plural(t.total, "entry", "entries")),
        t.avg_mood != null && h("span", {}, "average mood ", moodChip(t.avg_mood, scale), " ", delta(t.avg_mood, t.baseline_mood)),
        h("a", { href: href.entries({ tag: t.name }) }, "Filter entries by this tag"),
      ],
    }),
    t.cooccurring.length ? section("Often tagged alongside", h("div", { class: "chips" }, t.cooccurring.map((c) => tagChip(c.name, c.n))), {
      note: "number of shared entries",
    }) : null,
    askHere({ tag: t.name }, "What do these days have in common?", `Answers only read the entries tagged ${t.name}.`),
    section("Entries", entryLines(t.entries, scale), {
      note: t.entries_capped ? h("a", { href: href.entries({ tag: t.name }) }, `newest ${fmtNum(t.entries.length)} shown, filter to see all`) : null,
    }));
}

async function digestsView([type]) {
  const data = await api("digests");
  const scale = data.mood_scale;
  const current = DIGEST_TABS.includes(type) ? type : "month";
  document.title = "Digests · Commonplace";
  const list = current === "notes" ? [] : data.digests[current] || [];
  const tabCount = (t) => (t === "notes" ? data.notes.length : (data.digests[t] || []).length);
  const noteCards = data.notes.length
    ? h("div", { class: "digest-list" }, data.notes.map((d) => h("a", { class: "digest-card", href: href.note(d.id) },
      h("div", {},
        h("div", { class: "digest-when" }, d.title),
        h("div", { class: "digest-sub" }, kindLabel(d.kind), d.first ? ` · ${mediumDate(d.first)} to ${mediumDate(d.last)}` : ""),
        h("div", { class: "digest-sub" }, moodChip(d.mood, scale), ` · ${plural(d.entries ?? 0, "entry", "entries")}`),
        d.stale && h("div", { class: "digest-sub" }, h("span", { class: "status pending" }, "out of date"))),
      h("div", { class: "digest-summary clamp" }, mdPlain(d.summary) || "No summary text."))))
    : empty(data.note_min_mentions
      ? ["No relationship digests yet. ", h("code", {}, "journal rollup"), ` writes one for every note linked from ${data.note_min_mentions} or more entries.`]
      : ["Relationship digests are turned off (", h("code", {}, "note_min_mentions = 0"), " in config.toml)."]);
  return h("div", {},
    pageHead({
      eyebrow: "Written by the precise model",
      title: "Digests",
      lede: "Summaries of each week, month and year, and of the people, places and topics you link most. Answers read these instead of every entry. A digest goes out of date when an entry it covers changes, until the next journal rollup.",
    }),
    h("nav", { class: "tabs", "aria-label": "Digest periods" }, DIGEST_TABS.map((t) =>
      h("a", { class: "tab", href: href.digests(t), "aria-current": t === current ? "page" : null },
        t === "notes" ? "Notes " : `${KIND_LABELS[t]}s `, h("span", { class: "count" }, fmtNum(tabCount(t)))))),
    current === "notes" ? noteCards : list.length
      ? h("div", { class: "digest-list" }, list.map((d) => h("a", { class: "digest-card", href: href.digest(d.type, d.key) },
        h("div", {},
          h("div", { class: "digest-when" }, periodTitle(d.type, d.key)),
          h("div", { class: "digest-sub" }, periodRange(d.type, d.start, d.end)),
          h("div", { class: "digest-sub" }, moodChip(d.mood, scale), ` · ${plural(d.entries ?? 0, "entry", "entries")}`),
          d.stale && h("div", { class: "digest-sub" }, h("span", { class: "status pending" }, "out of date"))),
        h("div", { class: "digest-summary clamp" }, mdPlain(d.summary) || "No summary text."),
        d.tags.length ? h("div", { class: "digest-tags" }, d.tags.join(" · ")) : null)))
      : current === "range"
        ? empty("No digests of a span of days yet. For a trip or a hard fortnight, ", h("code", {}, "journal digest 2024-03-02..2024-03-11 --refresh"), " writes one, and answers about those days read it.")
        : empty(`No ${current} digests yet. Run `, h("code", {}, "journal rollup"), " to write them."));
}

async function digestView([type, key]) {
  const [d, all] = await Promise.all([api(`digests/${type}/${encodeURIComponent(key)}`), api("digests")]);
  const scale = d.mood_scale;
  document.title = `${periodTitle(type, key)} · Commonplace`;
  const keys = (all.digests[type] || []).map((x) => x.key).sort();
  const i = keys.indexOf(key);
  const prev = i > 0 ? keys[i - 1] : null;
  const next = i >= 0 && i < keys.length - 1 ? keys[i + 1] : null;
  const r = d.digest;
  currentNeighbours = { prev: prev && href.digest(type, prev), next: next && href.digest(type, next) };

  const margin = h("aside", { class: "margin", "aria-label": "About this digest" },
    h("p", { class: "margin-title" }, "About this digest"),
    r ? [
      gloss("Average mood", moodChip(r.mood, scale)),
      gloss("Built from", plural(r.entries ?? 0, "entry", "entries")),
      gloss("Main tags", r.tags.length ? h("div", { class: "chips" }, r.tags.map((t) => h("a", { class: "chip pencil", href: href.tag(t) }, t))) : h("span", { class: "quiet" }, "None")),
      gloss("Written", stamp(r.generated_at), r.stale && h("div", {}, "Out of date: an entry in this period changed since.", h("div", {}, upkeepButton("Rewrite it", "digest", { key })))),
    ] : gloss("Status", "Not written yet.", h("div", {}, upkeepButton("Write it", "digest", { key }))));

  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.digests(type) }, `${KIND_LABELS[type]} digest`),
      title: periodTitle(type, key),
      actions: [
        button("← Earlier", prev && href.digest(type, prev), { disabled: !prev, key: "k" }),
        button("Later →", next && href.digest(type, next), { disabled: !next, key: "j" }),
      ],
      meta: [
        h("span", {}, periodRange(type, d.start, d.end)),
        type !== "range" && d.entries.length ? h("a", { href: href.review(key) }, "Review") : null,
        type !== "range" ? h("a", { href: href.compare(key) }, `Compare with the ${type} before`) : null,
      ],
    }),
    h("div", { class: "folio" },
      h("article", {},
        h("p", { class: "reading-label" }, r ? "What the model wrote about this period" : "No digest for this period yet"),
        r && r.summary ? h("div", { class: "raw md" }, md(r.summary)) : h("div", { class: "raw empty" }, "Nothing written.")),
      margin),
    d.entries.length ? askHere({ from: d.start, to: d.end }, "What stood out in this period?",
      `Answers only read the entries from ${periodRange(type, d.start, d.end)}.`) : null,
    section("Entries in this period", entryLines(d.entries, scale, { emptyText: "No entries in this period." })));
}

async function filesView(_, params) {
  const data = await api("files");
  document.title = "Files · Commonplace";
  const groups = {
    all: { label: "All", test: () => true },
    problems: { label: "Problems", test: (f) => f.status !== "indexed" && f.status !== "private" },
    entries: { label: "Entries", test: (f) => f.entries > 0 },
    notes: { label: "Notes", test: (f) => f.note_id != null },
    private: { label: "Private", test: (f) => f.status === "private" },
    nothing: { label: "Nothing stored", test: (f) => f.status === "indexed" && !f.entries && f.note_id == null },
  };
  let show = groups[params.get("show")] ? params.get("show") : "all";
  const tabs = h("div", { class: "chips" });
  const body = h("tbody", {});

  function render() {
    replaceParams("/files", { show: show === "all" ? "" : show });
    fill(tabs, ...Object.entries(groups).map(([key, g]) => {
      const n = data.files.filter(g.test).length;
      return h("button", {
        class: key === show ? "chip active" : key === "problems" && n ? "chip alert" : "chip",
        type: "button", onclick: () => { show = key; render(); },
      }, g.label, h("span", { class: "n" }, fmtNum(n)));
    }));
    const rows = data.files.filter(groups[show].test);
    fill(body, ...rows.map((f) => h("tr", {},
      h("td", { class: "path" }, f.vault_path),
      h("td", {}, h("span", { class: `status ${f.status}` }, f.status)),
      h("td", {},
        f.note_id != null ? h("a", { href: href.note(f.note_id) }, "a note")
          : f.entries ? h("a", { href: href.entry(f.entry_id) }, plural(f.entries, "entry", "entries"))
            : h("span", { class: "delta" }, f.status === "private" ? "nothing, it's marked private" : "nothing"),
        f.error && h("div", { class: "err" }, f.error)),
      h("td", { class: "num" }, stamp(f.last_indexed)))));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: 4 }, empty("No files here."))));
  }
  render();

  return h("div", {},
    pageHead({
      eyebrow: "What the indexer has seen",
      title: "Files",
      lede: "Every file in your journal folder the cache is tracking. A file that failed is retried on the next journal index.",
    }),
    h("div", { class: "filters" }, tabs),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Status"), h("th", {}, "Became"), h("th", { class: "num" }, "Last indexed"))),
      body)));
}

async function searchView(_, params) {
  const cat = await getCatalog();
  const scale = cat.mood_scale;
  document.title = "Search · Commonplace";
  const input = h("input", { type: "search", value: params.get("q") || "", placeholder: "hiking with John, a hard week at work…", "aria-label": "Search the journal" });
  const status = h("p", { class: "search-status", role: "status" });
  const results = h("div", {});
  let token = 0;

  function show(data) {
    fill(results,
      data.notes.length ? section("Notes", h("div", { class: "note-grid" }, data.notes.map((n) =>
        h("a", { class: "note-card", href: href.note(n.id) },
          h("div", { class: "note-title" }, n.title),
          h("div", { class: "note-aliases" }, kindLabel(n.kind)),
          n.summary ? h("div", { class: "note-summary" }, n.summary) : null)))) : null,
      section("Entries", entryLines(data.entries, scale, { emptyText: "No entries match." }), {
        note: data.mode === "meaning" ? "by meaning and keywords" : "by keywords",
      }));
  }

  async function run() {
    const q = input.value.trim();
    replaceParams("/search", { q });
    const mine = ++token;
    if (!q) {
      status.textContent = "";
      fill(results, empty("Search looks through the text of your entries and notes. With Ollama running it also matches by meaning, so a search for “feeling low” can find entries that never use those words."));
      return;
    }
    status.textContent = "Matching keywords…";
    try {
      const quick = await api("search", { q, mode: "keyword" });
      if (mine !== token) return;
      show(quick);
      status.textContent = "Also looking for matches by meaning…";
      const deep = await api("search", { q, mode: "meaning" });
      if (mine !== token) return;
      show(deep);
      status.textContent = deep.notice ? `Showing keyword matches only. ${deep.notice}` : "";
    } catch (err) {
      if (mine !== token) return;
      status.textContent = err.message;
    }
  }

  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") run(); });
  const go = h("button", { class: "button", type: "button", onclick: run }, "Search");
  run();
  setTimeout(() => input.focus(), 0);
  return h("div", {},
    pageHead({ eyebrow: "Keyword and meaning search", title: "Search" }),
    h("div", { class: "search-box", role: "search" }, input, go),
    status,
    results);
}

// ---------------------------------------------------------------- themes

const THEME_NEWEST = 30;

function themeStatus(st) {
  if (!st.made_at) return null;
  const since = st.placed ? `; ${plural(st.placed, "entry", "entries")} written since go with the nearest theme` : "";
  return h("p", { class: "section-note" }, `Worked out ${stamp(st.made_at)} from ${plural(st.grouped, "entry", "entries")}${since}.`,
    st.stale ? " That's a lot since. " : " ", upkeepButton(st.stale ? "Group them again" : "Refresh", "themes"));
}

// each month a cell, shaded by the share of that month's entries in the theme
function themeStrip(theme, months, totals) {
  const strip = svg("svg", {
    class: "strip theme-strip", viewBox: `0 0 ${months.length} 1`, preserveAspectRatio: "none", role: "img",
    "aria-label": `${theme.label}: ${plural(theme.entries, "entry", "entries")}, ${theme.first ? `${mediumDate(theme.first)} to ${mediumDate(theme.last)}` : ""}`,
  });
  months.forEach((month, i) => {
    if (month.endsWith("-01") && i > 0) {
      strip.append(svg("line", { class: "month", x1: i, x2: i, y1: 0, y2: 1, "vector-effect": "non-scaling-stroke" }));
    }
    const n = theme.months[month] || 0;
    if (!n) return;
    const share = n / (totals[month] || n);
    strip.append(svg("a", { href: href.entries({ from: `${month}-01`, to: lastDayOf(month) }) },
      svg("title", {}, `${fmtMonth.format(toDate(`${month}-01`))}: ${n} of ${plural(totals[month] || n, "entry", "entries")}`),
      svg("rect", { class: `day ${amountStep(share, 0, 1)}`, x: i, y: 0, width: 1, height: 1 })));
  });
  return strip;
}

async function themesView() {
  const data = await api("themes");
  const scale = data.mood_scale;
  document.title = "Themes · Commonplace";
  const head = pageHead({
    eyebrow: "Found from what your entries are about",
    title: "Themes",
    lede: "Threads that run through the journal, found by grouping entries close in meaning, whether or not you tagged or linked them. The fast model names each from the entries nearest its middle.",
  });
  if (!data.themes.length) {
    return h("div", {}, head, empty("No themes yet. Finding them groups your entries, which needs no model, and names each group with one call to the fast model."),
      upkeepButton("Find the themes", "themes"));
  }
  const months = Object.keys(data.month_totals);
  const years = new Map();
  for (const month of months) years.set(month.slice(0, 4), (years.get(month.slice(0, 4)) || 0) + 1);
  const axis = h("div", { class: "theme-years", "aria-hidden": "true" }, [...years].map(([year, n]) => {
    const label = h("span", {}, year);
    label.style.flexGrow = String(n);
    return label;
  }));
  const rows = data.themes.map((t) => h("div", { class: "theme-row" },
    h("div", { class: "theme-name" },
      h("a", { href: href.theme(t.id) }, t.label),
      h("div", { class: "quiet" }, plural(t.entries, "entry", "entries"), " · ", moodChip(t.mood, scale))),
    themeStrip(t, months, data.month_totals)));
  return h("div", {}, head,
    themeStatus(data.status),
    h("div", { class: "theme-rows" }, h("div", { class: "theme-row" }, h("div", {}), axis), rows),
    amountLegend("a small share of that month", "most of it"));
}

async function themeView([id]) {
  const t = await api(`themes/${id}`);
  const scale = t.mood_scale;
  document.title = `${t.label} · Commonplace`;
  const months = Object.entries(t.months).map(([month, n]) => ({ month, avg: n, n }));
  const tip = (m) => `${fmtMonth.format(toDate(`${m.month}-01`))}: ${plural(m.n, "entry", "entries")}`;
  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.themes() }, "Theme"),
      title: t.label,
      lede: t.about,
      meta: [
        h("span", {}, plural(t.entries, "entry", "entries")),
        t.first && h("span", {}, `${mediumDate(t.first)} to ${mediumDate(t.last)}`),
        t.mood != null && h("span", {}, "average mood ", moodChip(t.mood, scale), " ", delta(t.mood, t.baseline_mood)),
      ],
    }),
    section("Entries a month", metricChart(months, "entries", false, tip)),
    section("Closest to its middle", entryLines(t.central, scale), { note: t.name ? "what it was named from" : null }),
    t.tags.length ? section("Tags", h("div", { class: "chips" }, t.tags.map((g) => tagChip(g.name, g.entries)))) : null,
    t.notes.length ? section("Notes", h("div", { class: "chips" }, t.notes.map((n) =>
      h("a", { class: "chip", href: href.note(n.id), title: kindLabel(n.kind) }, n.title, h("span", { class: "n" }, fmtNum(n.entries))))), {
      note: "entries that link each",
    }) : null,
    section("Newest entries", entryLines(t.newest, scale), {
      note: t.entries > t.newest.length ? `newest ${t.newest.length} of ${fmtNum(t.entries)}` : null,
    }));
}

// ---------------------------------------------------------------- note map

// A graph puts any two kinds side by side, and only three colours stay
// distinct for everyone in every pair, so the rest share one gray.
const MAP_KIND_SLOTS = 3;
const MAP_LABELLED = 12;
const MAP_STEPS = 300;
const MAP_WIDTH = 900;
const MAP_HEIGHT = 560;
const MAP_SIZES = [25, 40, 80, 150];

// Fruchterman-Reingold, started from a circle in order of size so the same
// notes always land in the same places
function mapLayout(nodes, edges) {
  const n = nodes.length;
  const cx = MAP_WIDTH / 2;
  const cy = MAP_HEIGHT / 2;
  const pos = nodes.map((_, i) => {
    const a = (i / Math.max(1, n)) * 2 * Math.PI;
    return { x: cx + Math.cos(a) * MAP_WIDTH * 0.32, y: cy + Math.sin(a) * MAP_HEIGHT * 0.32 };
  });
  if (n < 2) return pos.map(() => ({ x: cx, y: cy }));
  const at = new Map(nodes.map((node, i) => [node.id, i]));
  const most = Math.max(...edges.map((e) => e.n), 1);
  const k = Math.sqrt((MAP_WIDTH * MAP_HEIGHT) / n) * 0.55;
  for (let step = 0; step < MAP_STEPS; step++) {
    const heat = (MAP_WIDTH / 12) * (1 - step / MAP_STEPS) + 0.5;
    const move = pos.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = pos[i].x - pos[j].x;
        const dy = pos[i].y - pos[j].y;
        const d = Math.max(1, Math.hypot(dx, dy));
        const push = (k * k) / d;
        move[i].x += (dx / d) * push; move[i].y += (dy / d) * push;
        move[j].x -= (dx / d) * push; move[j].y -= (dy / d) * push;
      }
    }
    for (const e of edges) {
      const i = at.get(e.a);
      const j = at.get(e.b);
      const dx = pos[i].x - pos[j].x;
      const dy = pos[i].y - pos[j].y;
      const d = Math.max(1, Math.hypot(dx, dy));
      const pull = ((d * d) / k) * (0.3 + 0.7 * (e.n / most));
      move[i].x -= (dx / d) * pull; move[i].y -= (dy / d) * pull;
      move[j].x += (dx / d) * pull; move[j].y += (dy / d) * pull;
    }
    pos.forEach((p, i) => {
      // a little gravity keeps notes with no pairs from drifting off
      move[i].x += (cx - p.x) * 0.05 * k / 40;
      move[i].y += (cy - p.y) * 0.05 * k / 40;
      const len = Math.max(1, Math.hypot(move[i].x, move[i].y));
      p.x = Math.min(MAP_WIDTH - 40, Math.max(40, p.x + (move[i].x / len) * Math.min(len, heat)));
      p.y = Math.min(MAP_HEIGHT - 24, Math.max(24, p.y + (move[i].y / len) * Math.min(len, heat)));
    });
  }
  return pos;
}

function noteMap(data) {
  const { nodes, edges } = data;
  const counts = new Map();
  for (const node of nodes) counts.set(node.kind, (counts.get(node.kind) || 0) + 1);
  const slotted = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, MAP_KIND_SLOTS).map(([k]) => k);
  const kindClass = (kind) => (slotted.includes(kind) ? `k${slotted.indexOf(kind) + 1}` : "k-other");
  const pos = mapLayout(nodes, edges);
  const most = Math.max(...nodes.map((x) => x.n), 1);
  const radius = (x) => 4 + 12 * Math.sqrt(x.n / most);
  const mostShared = Math.max(...edges.map((e) => e.n), 1);
  const at = new Map(nodes.map((x, i) => [x.id, i]));
  const labelled = new Set(nodes.slice(0, MAP_LABELLED).map((x) => x.id));
  const neighbours = new Map(nodes.map((x) => [x.id, new Set([x.id])]));
  for (const e of edges) {
    neighbours.get(e.a).add(e.b);
    neighbours.get(e.b).add(e.a);
  }

  const chart = svg("svg", {
    class: "note-map", viewBox: `0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`, role: "group",
    "aria-label": `${plural(nodes.length, "note")} and the ${plural(edges.length, "pair")} of them linked from the same entries`,
  });
  const lines = edges.map((e) => {
    const a = pos[at.get(e.a)];
    const b = pos[at.get(e.b)];
    return svg("line", {
      class: "map-edge", x1: a.x, y1: a.y, x2: b.x, y2: b.y, "data-a": e.a, "data-b": e.b,
      "stroke-width": 1 + 3 * ((e.n - data.min_shared) / Math.max(1, mostShared - data.min_shared)),
    });
  });
  chart.append(svg("g", { "aria-hidden": "true" }, lines));

  const marks = nodes.map((x, i) => {
    const p = pos[i];
    const r = radius(x);
    const left = p.x > MAP_WIDTH - 140;
    const mark = svg("a", {
      class: labelled.has(x.id) ? "map-node labelled" : "map-node", href: href.note(x.id), "data-id": x.id,
      "aria-label": `${x.title}, ${kindLabel(x.kind)}, linked from ${plural(x.n, "entry", "entries")}`,
    },
      svg("title", {}, `${x.title} (${kindLabel(x.kind)}): ${plural(x.n, "entry", "entries")}`),
      svg("circle", { class: kindClass(x.kind), cx: p.x, cy: p.y, r }),
      svg("text", { x: left ? p.x - r - 4 : p.x + r + 4, y: p.y + 4, "text-anchor": left ? "end" : "start" }, x.title));
    const near = () => {
      chart.classList.add("focusing");
      const close = neighbours.get(x.id);
      for (const m of marks) m.classList.toggle("near", close.has(Number(m.dataset.id)));
      for (const line of lines) line.classList.toggle("near", Number(line.dataset.a) === x.id || Number(line.dataset.b) === x.id);
    };
    const away = () => chart.classList.remove("focusing");
    mark.addEventListener("mouseenter", near);
    mark.addEventListener("focus", near);
    mark.addEventListener("mouseleave", away);
    mark.addEventListener("blur", away);
    return mark;
  });
  // biggest last, so they're drawn on top
  chart.append(svg("g", {}, [...marks].reverse()));

  const others = [...counts.keys()].filter((k) => !slotted.includes(k));
  const legend = h("div", { class: "map-legend" },
    slotted.map((k) => h("span", {}, h("span", { class: `map-swatch ${kindClass(k)}`, "aria-hidden": "true" }), `${kindLabel(k)} (${fmtNum(counts.get(k))})`)),
    others.length ? h("span", {}, h("span", { class: "map-swatch k-other", "aria-hidden": "true" }),
      `${others.map(kindLabel).join(", ")} (${fmtNum(others.reduce((sum, k) => sum + counts.get(k), 0))})`) : null,
    h("span", { class: "quiet" }, "Bigger dots are linked from more entries; thicker lines join notes that share more."));

  const byId = new Map(nodes.map((x) => [x.id, x]));
  const table = h("details", { class: "turn-read" },
    h("summary", {}, "As a table"),
    edges.length
      ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "Note"), h("th", {}, "and"), h("th", { class: "num" }, "Shared entries"))),
        h("tbody", {}, edges.map((e) => h("tr", {},
          h("td", {}, h("a", { href: href.note(e.a) }, byId.get(e.a).title)),
          h("td", {}, h("a", { href: href.note(e.b) }, byId.get(e.b).title)),
          h("td", { class: "num" }, fmtNum(e.n)))))))
      : empty("No two of these notes share an entry."));
  return [legend, h("div", { class: "chart-wrap map-wrap" }, chart), table];
}

async function mapView(_, params) {
  const cat = await getCatalog();
  document.title = "Note map · Commonplace";
  const state = {
    kind: params.get("kind") || "", year: params.get("year") || "",
    limit: MAP_SIZES.includes(Number(params.get("limit"))) ? Number(params.get("limit")) : 40,
  };
  const years = [...new Set(cat.entries.map((e) => e.date.slice(0, 4)))].sort().reverse();
  const kinds = [...new Set(cat.notes.map((n) => n.kind))].sort();
  const kindSelect = h("select", {}, h("option", { value: "" }, "Every kind"),
    kinds.map((k) => h("option", { value: k, selected: k === state.kind }, kindLabel(k))));
  const yearSelect = h("select", {}, h("option", { value: "" }, "All years"),
    years.map((y) => h("option", { value: y, selected: y === state.year }, y)));
  const sizeSelect = h("select", {}, MAP_SIZES.map((n) => h("option", { value: n, selected: n === state.limit }, `${n} notes`)));
  const body = h("div", {});
  let token = 0;

  async function load() {
    const mine = ++token;
    replaceParams("/map", { kind: state.kind, year: state.year, limit: state.limit === 40 ? "" : state.limit });
    fill(body, h("p", { class: "search-status", role: "status" }, "Laying out the notes…"));
    const data = await api("graph", {
      kind: state.kind, limit: state.limit,
      from: state.year && `${state.year}-01-01`, to: state.year && `${state.year}-12-31`,
    }).catch((err) => ({ error: err }));
    if (mine !== token) return;
    if (data.error) {
      fill(body, h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't draw the map"), data.error.message));
      return;
    }
    if (!data.nodes.length) {
      fill(body, empty(state.kind || state.year ? "No entry links a note like that in this range." : "No entry links a note yet. Notes come from the folders in notes.paths."));
      return;
    }
    fill(body, noteMap(data));
  }

  const change = (key, el, number = false) => () => { state[key] = number ? Number(el.value) : el.value; load(); };
  kindSelect.addEventListener("change", change("kind", kindSelect));
  yearSelect.addEventListener("change", change("year", yearSelect));
  sizeSelect.addEventListener("change", change("limit", sizeSelect, true));
  load();
  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.notes() }, "Notes"),
      title: "Note map",
      lede: "The notes you link most, joined when the same entries link both. Point at one to see who and what it turns up with, or open it.",
    }),
    h("div", { class: "filters", role: "search" }, field("Kind", kindSelect), field("Years", yearSelect), field("Show", sizeSelect)),
    body);
}

// ---------------------------------------------------------------- compare and review

const PERIOD_HINT = "2024, 2024-03 or 2024-W10";

function share(n, of) {
  return n ? `${fmtNum(n)} (${Math.round(of * 100)}%)` : "–";
}

function changeTable(title, rows, label, cat, periods, { note } = {}) {
  if (!rows.length) return null;
  const notes = new Map(cat.notes.map((n) => [n.title, n]));
  const name = (c) => {
    if (label === "tag") return h("a", { href: href.tag(c.name) }, c.name);
    const found = notes.get(c.name);
    return found ? h("a", { href: href.note(found.id), title: kindLabel(found.kind) }, c.name) : c.name;
  };
  return section(title, h("div", { class: "table-wrap" }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, h("th", {}, label === "tag" ? "Tag" : "Note"),
      h("th", { class: "num" }, periods[0].title), h("th", { class: "num" }, periods[1].title))),
    h("tbody", {}, rows.map((c) => h("tr", {},
      h("td", {}, name(c)),
      h("td", { class: "num" }, share(c.a, c.a_share)),
      h("td", { class: "num" }, share(c.b, c.b_share))))))), { note });
}

async function compareView([first, second]) {
  const inA = h("input", { type: "text", value: first, placeholder: PERIOD_HINT, "aria-label": "First period", spellcheck: "false" });
  const inB = h("input", { type: "text", value: second || "", placeholder: "the one before", "aria-label": "Second period", spellcheck: "false" });
  const form = h("form", { class: "filters", role: "search" },
    field("Compare", inA), field("with", inB), h("button", { class: "button", type: "submit" }, "Compare"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (inA.value.trim()) location.hash = href.compare(inA.value.trim(), inB.value.trim());
  });
  let d;
  let cat;
  let known;
  try {
    [d, cat, known] = await Promise.all([
      api("compare", { first, second }), getCatalog(), api("metrics").then((m) => m.metrics).catch(() => []),
    ]);
  } catch (err) {
    // a period that isn't one, or one with nothing in it; the form is how to fix it
    if (!(err instanceof ApiError) || ![400, 404].includes(err.status)) throw err;
    document.title = "Side by side · Commonplace";
    return h("div", {}, pageHead({ eyebrow: "Side by side", title: "Compare two periods" }), form,
      h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't compare"), h("span", {}, err.message)));
  }
  const yesNo = new Set(known.filter((m) => m.yes_no).map((m) => m.key));
  const scale = d.mood_scale;
  const sides = [d.a, d.b];
  const titles = sides.map((side) => d.periods[side.label]);
  document.title = `${titles[0].title} and ${titles[1].title} · Commonplace`;

  const head = (t) => h("th", { class: "num" }, h("a", { href: href.review(t.key) }, t.title));
  const row = (label, a, b) => h("tr", {}, h("td", {}, label), h("td", { class: "num" }, a), h("td", { class: "num" }, b));
  const metricKeys = [...new Set([...Object.keys(d.a.metrics), ...Object.keys(d.b.metrics)])].sort();
  const numbers = h("div", { class: "table-wrap" }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, h("th", {}, ""), head(titles[0]), head(titles[1]))),
    h("tbody", {},
      row("Entries", fmtNum(d.a.writing.entries), fmtNum(d.b.writing.entries)),
      row("Days written", fmtNum(d.a.writing.days_written), fmtNum(d.b.writing.days_written)),
      row("Words", fmtNum(d.a.writing.words), fmtNum(d.b.writing.words)),
      row("Average mood", moodChip(d.a.mood, scale), moodChip(d.b.mood, scale)),
      row("Longest streak", plural(d.a.writing.longest_streak, "day"), plural(d.b.writing.longest_streak, "day")),
      metricKeys.map((k) => row(h("a", { href: href.metric(k) }, k),
        d.a.metrics[k] ? fmtValue(d.a.metrics[k][0], yesNo.has(k)) : "–", d.b.metrics[k] ? fmtValue(d.b.metrics[k][0], yesNo.has(k)) : "–")))));

  const shareNote = `share of each period's entries; ${d.min_count} or more to count`;
  const changes = [
    changeTable("Tags you wrote more", d.tags_up, "tag", cat, titles, { note: shareNote }),
    changeTable("Tags you wrote less", d.tags_down, "tag", cat, titles, { note: shareNote }),
    changeTable("Notes you linked more", d.notes_up, "note", cat, titles, { note: shareNote }),
    changeTable("Notes you linked less", d.notes_down, "note", cat, titles, { note: shareNote }),
    changeTable(`New in ${titles[1].title}`, d.notes_new, "note", cat, titles, { note: `not linked at all in ${titles[0].title}` }),
    changeTable(`Gone in ${titles[1].title}`, d.notes_gone, "note", cat, titles, { note: `not linked at all in ${titles[1].title}` }),
  ].filter(Boolean);

  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.digests(titles[1].kind) }, "Side by side"),
      title: `${titles[0].title} and ${titles[1].title}`,
      lede: "Two periods from what's already stored. Tags and notes are compared by the share of each period's entries they're on, since a busy month would otherwise seem to have more of everything.",
    }),
    form,
    section("In numbers", numbers),
    changes.length ? changes : empty("No tag or note rose or fell by enough to show."));
}

async function reviewView([period]) {
  const r = await api(`review/${encodeURIComponent(period)}`);
  const scale = r.mood_scale;
  const before = `the ${r.kind} before`;
  const w = r.writing;
  document.title = `${r.title} in review · Commonplace`;

  const digest = r.digest
    ? [h("div", { class: "raw md" }, md(r.digest.summary)),
      r.digest.stale && h("p", { class: "turn-meta" }, h("span", { class: "status pending" }, "out of date"),
        " Some entries changed after this was written. ", upkeepButton("Rewrite it", "digest", { key: r.key }))]
    : [empty(`There's no ${r.kind} digest yet.`), upkeepButton("Write it", "digest", { key: r.key })];

  const facts = h("div", { class: "facts" },
    fact(fmtNum(w.entries), `entries on ${plural(w.days_written, "day")}`, href.entries({ from: r.start, to: r.end })),
    fact(fmtNum(w.words), "words"),
    fact(plural(w.longest_streak, "day"), w.longest_streak_end ? `longest streak, to ${mediumDate(w.longest_streak_end)}` : "longest streak"),
    fact(fmtMood(r.mood.average), r.mood.previous != null ? `average mood, ${fmtMood(r.mood.previous)} ${before}` : "average mood"));

  const partName = r.kind === "year" ? "month" : "week";
  const parts = r.parts.length ? section(`Mood by ${partName}`, h("div", { class: "table-wrap" }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, h("th", {}, partName === "month" ? "Month" : "Week"), h("th", {}, "Mood"), h("th", { class: "num" }, "Entries"))),
    h("tbody", {}, r.parts.map((p) => h("tr", {},
      h("td", {}, p.entries
        ? h("a", { href: partName === "month" ? href.review(p.start.slice(0, 7)) : href.entries({ from: p.start, to: p.end }) }, p.label)
        : p.label),
      h("td", {}, p.entries ? moodChip(p.mood, scale) : h("span", { class: "quiet" }, "nothing written")),
      h("td", { class: "num" }, fmtNum(p.entries)))))))) : null;

  const noteChips = (rows, count) => h("div", { class: "chips" }, rows.map((n) =>
    h("a", { class: "chip", href: href.note(n.id), title: kindLabel(n.kind) }, n.title, h("span", { class: "n" }, count(n)))));

  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.digest(r.kind, r.key) }, `${KIND_LABELS[r.kind]} digest`),
      title: `${r.title} in review`,
      actions: [button(`← ${periodTitle(r.kind, r.previous.key)}`, href.review(r.previous.key), { title: `The review of ${before}` })],
      meta: [h("span", {}, periodRange(r.kind, r.start, r.end)), h("a", { href: href.compare(r.key) }, `Compare with ${before}`),
        h("a", { href: `/api/review/${encodeURIComponent(r.key)}/download`, download: "" }, "Save as Markdown")],
    }),
    section("Digest", digest),
    facts,
    parts,
    r.days.length ? section("Day by day", entryLines(r.days, scale)) : null,
    r.best.length ? section("Best days", entryLines(r.best, scale)) : null,
    r.hardest.length ? section("Hardest days", entryLines(r.hardest, scale)) : null,
    r.notes.length ? section("Who and what came up most", noteChips(r.notes, (n) => fmtNum(n.entries)), { note: "entries that link each" }) : null,
    r.new_notes.length && r.has_earlier ? section(`New this ${r.kind}`, noteChips(r.new_notes, (n) => `from ${mediumDate(n.first)}`)) : null,
    r.tags.length ? section("Tags", h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "Tag"), h("th", { class: "num" }, "Entries"), r.has_earlier ? h("th", { class: "num" }, `${before[0].toUpperCase()}${before.slice(1)}`) : null)),
      h("tbody", {}, r.tags.map((t) => h("tr", {},
        h("td", {}, h("a", { href: href.tag(t.name) }, t.name)),
        h("td", { class: "num" }, fmtNum(t.entries)),
        r.has_earlier ? h("td", { class: "num" }, t.previous ? fmtNum(t.previous) : "–") : null)))))) : null,
    r.fell_away.length ? section("Fell away", h("ul", { class: "plain" }, r.fell_away.map((t) =>
      h("li", {}, h("a", { href: href.tag(t.name) }, t.name), `: ${plural(t.entries, "entry", "entries")}, down from ${fmtNum(t.previous)}`)))) : null,
    r.about_you.length ? section("New about you", h("ul", { class: "plain" }, r.about_you.map((f) =>
      h("li", {}, f.fact, " ", h("a", { href: href.entries({ from: f.date, to: f.date }), class: "quiet" }, mediumDate(f.date)))))) : null);
}

// ---------------------------------------------------------------- numbers

async function metricsView() {
  const data = await api("metrics");
  document.title = "Numbers · Commonplace";
  const head = pageHead({
    eyebrow: "From your frontmatter",
    title: "Numbers",
    lede: "Numbers you keep in your entries' frontmatter, like sleep: 7 or exercise: true. No model reads them; they're kept as you wrote them, and true and false count as 1 and 0.",
  });
  if (!data.metrics.length) {
    return h("div", {}, head, empty("None yet. Add a line like ", h("code", {}, "sleep: 7"), " to a daily note's frontmatter and run ", h("code", {}, "journal index"), "."));
  }
  return h("div", {}, head,
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "Number"), h("th", { class: "num" }, "Entries"), h("th", { class: "num" }, "Average"),
        h("th", { class: "num" }, "Lowest"), h("th", { class: "num" }, "Highest"), h("th", { class: "num" }, "First"), h("th", { class: "num" }, "Last"))),
      h("tbody", {}, data.metrics.map((m) => h("tr", {},
        h("td", {}, h("a", { href: href.metric(m.key) }, m.key), m.yes_no ? h("span", { class: "quiet" }, " yes or no") : null),
        h("td", { class: "num" }, fmtNum(m.n)),
        h("td", { class: "num" }, m.yes_no ? `${fmtValue(m.avg, true)} of days` : fmtValue(m.avg)),
        h("td", { class: "num" }, m.yes_no ? "–" : fmtValue(m.lo)),
        h("td", { class: "num" }, m.yes_no ? "–" : fmtValue(m.hi)),
        h("td", { class: "num" }, mediumDate(m.first)),
        h("td", { class: "num" }, mediumDate(m.last))))))));
}

function splitText(d, scale) {
  const s = d.split;
  if (!s) return empty(`Every entry records the same ${d.key}, so there's nothing to set side by side.`);
  const days = (n) => plural(n, "day");
  const lines = s.yes_no
    ? [
      h("p", { class: "split-line" }, `On the ${days(s.high.days)} with ${d.key}, your mood averaged `, moodChip(s.high.mood, scale), "."),
      h("p", { class: "split-line" }, `On the ${days(s.low.days)} without it, `, moodChip(s.low.mood, scale), "."),
    ]
    : [
      h("p", { class: "split-line" }, `On the ${days(s.high.days)} your ${d.key} was ${fmtValue(s.median)} or more (its median), your mood averaged `, moodChip(s.high.mood, scale), "."),
      h("p", { class: "split-line" }, `On the ${days(s.low.days)} it was less, `, moodChip(s.low.mood, scale), "."),
    ];
  return [lines, h("p", { class: "section-note" }, s.enough
    ? "The two go together on these days; that doesn't mean one causes the other."
    : "There are too few scored days on one side to say much yet.")];
}

async function metricView([key]) {
  const d = await api(`metrics/${encodeURIComponent(key)}`);
  const scale = d.mood_scale;
  const yesNo = d.yes_no;
  document.title = `${d.key} · Commonplace`;
  const onCalendar = href.calendar(d.last.slice(0, 4), d.key);
  const facts = h("div", { class: "facts" },
    fact(fmtValue(d.avg, yesNo), yesNo ? "of the entries that record it say yes" : "average"),
    fact(fmtNum(d.n), "entries record it"),
    yesNo ? null : fact(`${fmtValue(d.lo)} to ${fmtValue(d.hi)}`, "lowest to highest"),
    fact(mediumDate(d.last), `last recorded, first on ${mediumDate(d.first)}`, onCalendar));
  const moodBy = new Map(d.mood_months.map((m) => [m.month, m.avg_mood]));
  const table = h("details", { class: "turn-read" },
    h("summary", {}, "As a table"),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "Month"), h("th", { class: "num" }, yesNo ? "Share of days" : "Average"),
        h("th", { class: "num" }, "Entries"), h("th", {}, "Mood those days"))),
      h("tbody", {}, d.months.map((m) => h("tr", {},
        h("td", {}, h("a", { href: href.entries({ from: `${m.month}-01`, to: lastDayOf(m.month) }) }, fmtMonth.format(toDate(`${m.month}-01`)))),
        h("td", { class: "num" }, fmtValue(m.avg, yesNo)),
        h("td", { class: "num" }, fmtNum(m.n)),
        h("td", {}, moodChip(moodBy.get(m.month) ?? null, scale))))))));
  const aside = (e) => `${d.key} ${fmtValue(e.value)}`;
  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.metrics() }, "Numbers"),
      title: d.key,
      meta: [h("a", { href: onCalendar }, "See it on the calendar")],
    }),
    facts,
    section("By month", h("div", { class: "chart-pair" },
      h("div", {}, h("h3", {}, yesNo ? `Share of days with ${d.key}` : `Average ${d.key}`), metricChart(d.months, d.key, yesNo)),
      h("div", {}, h("h3", {}, "Mood on those days"), monthlyMoodChart(d.mood_months, d.baseline_mood, scale)),
      table), { note: "two charts, one month to a column" }),
    section(yesNo ? `Mood with and without ${d.key}` : "Mood either side of the median", splitText(d, scale)),
    yesNo ? null : section(`Highest ${d.key}`, entryLines(d.highest, scale, { aside })),
    yesNo ? null : section(`Lowest ${d.key}`, entryLines(d.lowest, scale, { aside })));
}

// ---------------------------------------------------------------- ask

const STAGE_LABELS = {
  aggregate: "from statistics over your entries",
  fast_summary: "from stored summaries and notes",
  precise_raw: "from the full text of the entries",
  retrieval_empty: "nothing matched",
};
// matches query.CHAT_HISTORY_TURNS and the part of each answer it keeps
const HISTORY_TURNS = 6;
const HISTORY_ANSWER_CHARS = 400;

// what an answer can be kept to, as /api/ask and the entries page name them
const SCOPE_KEYS = ["note", "tag", "group", "from", "to"];

// Kept outside the view so a question keeps running, and the conversation
// stays, while you look at the entries it cites.
const ask = { turns: loadTurns(), scope: loadScope(), conversation: loadConversation(), controller: null, redraw: null };

function loadTurns() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("ask-turns") || "[]");
    // An answer is written in the background, so one that was running when the
    // page reloaded is picked up again by its id. Only a question that never
    // got one, because the reload came first, has no answer coming.
    return saved.map((t) => (t.status === "running" && !t.run ? { ...t, status: "cancelled" } : t));
  } catch {
    return [];
  }
}

// so the question history, when it's on, keeps a conversation's turns together
function loadConversation() {
  try {
    return sessionStorage.getItem("ask-conversation") || newConversation();
  } catch {
    return newConversation();
  }
}

function newConversation() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function loadScope() {
  try {
    return JSON.parse(sessionStorage.getItem("ask-scope") || "null");
  } catch {
    return null;
  }
}

function saveTurns() {
  try {
    sessionStorage.setItem("ask-turns", JSON.stringify(ask.turns));
    sessionStorage.setItem("ask-scope", JSON.stringify(ask.scope));
    sessionStorage.setItem("ask-conversation", ask.conversation);
  } catch {
    // storage can be full or refused; the conversation still works for this visit
  }
}

function scopeFrom(params) {
  const scope = {};
  for (const key of SCOPE_KEYS) {
    const value = (params.get(key) || "").trim();
    if (value) scope[key] = value;
  }
  return Object.keys(scope).length ? scope : null;
}

function scopeLabel(scope, cat) {
  const parts = [];
  if (scope.note) {
    const note = cat.notes.find((n) => String(n.id) === String(scope.note));
    parts.push(`linking ${note ? note.title : "a note that's gone"}`);
  }
  if (scope.group) {
    const [key, value] = scope.group.split("=");
    parts.push(`linking a note whose ${key} is ${value}`);
  }
  if (scope.tag) parts.push(`tagged ${scope.tag}`);
  if (scope.from && scope.to) parts.push(`from ${mediumDate(scope.from)} to ${mediumDate(scope.to)}`);
  else if (scope.from) parts.push(`from ${mediumDate(scope.from)} on`);
  else if (scope.to) parts.push(`up to ${mediumDate(scope.to)}`);
  return `Only entries ${parts.join(" and ")}`;
}

const scopeParams = (scope) => Object.fromEntries(SCOPE_KEYS.filter((k) => scope && scope[k]).map((k) => [k, scope[k]]));

// a question box on a note, tag, digest or entries page, kept to what that page shows
function askHere(scope, placeholder, intro) {
  const input = h("input", {
    type: "text", placeholder, "aria-label": "Your question", autocomplete: "off", maxlength: 2000,
  });
  const form = h("form", { class: "search-box ask-box ask-here", role: "search" },
    input, h("button", { class: "button", type: "submit" }, "Ask"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    if (q) location.hash = href.ask(q, scope);
  });
  return section("Ask about this", form, { intro });
}

function changed() {
  saveTurns();
  if (ask.redraw) ask.redraw();
}

async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      onEvent(event, data ? JSON.parse(data) : {});
    }
  }
}

// A question asked while another is answering waits its turn, and goes once
// the one before it is done, so it can follow up on that answer.
function askQuestion(question) {
  const turn = { question, status: "queued", scope: ask.scope };
  ask.turns.push(turn);
  if (ask.controller) changed();
  else runTurn(turn);
}

function askNext() {
  const next = ask.turns.find((t) => t.status === "queued");
  if (next) runTurn(next);
}

const RUN_GONE = "This answer was lost when `journal browse` was restarted. Ask it again.";

// `resume` follows an answer that's already being written, after a reload.
async function runTurn(turn, resume = false) {
  const history = ask.turns
    .filter((t) => t.status === "done")
    .slice(-HISTORY_TURNS)
    .map((t) => [t.rewritten || t.question, t.answer.text.slice(0, HISTORY_ANSWER_CHARS)]);
  Object.assign(turn, { status: "running", stage: resume ? "Picking the answer up again…" : "Starting…" });
  const question = turn.question;
  const controller = new AbortController();
  ask.controller = controller;
  changed();
  const onEvent = (event, data) => {
    if (event === "run") turn.run = data.id;
    else if (event === "stage") {
      turn.stage = data.text;
      if (turn.thinking) turn.thinkingPaused = true;
    } else if (event === "thinking") {
      // a fast answer that thought and gave up, then the precise model's own
      const gap = turn.thinkingPaused ? "\n\n" : "";
      turn.thinking = (turn.thinking || "") + gap + data.text;
      turn.thinkingPaused = false;
    } else if (event === "text") turn.partial = (turn.partial || "") + data.text;
    else if (event === "rewritten") turn.rewritten = data.question;
    else if (event === "answer") Object.assign(turn, { status: "done", answer: data });
    else if (event === "problem") Object.assign(turn, { status: "failed", error: data.error });
    else if (event === "cancelled") turn.status = "cancelled";
    else return;
    changed();
  };
  try {
    // If the connection drops while the answer is still being written, follow
    // it again. It's replayed from its first event, so what's shown is rebuilt.
    for (let tries = 0; turn.status === "running" && tries < 6; tries++) {
      const again = resume || tries > 0;
      if (again) {
        for (const key of ["partial", "thinking", "thinkingPaused", "rewritten"]) delete turn[key];
        if (tries > 0) await new Promise((r) => setTimeout(r, 1000 * tries));
      }
      let response;
      try {
        response = again
          ? await fetch(`/api/ask/${turn.run}/events`, { headers: { Accept: "text/event-stream" }, signal: controller.signal })
          : await send(
            "ask", { q: question, history, conversation: ask.conversation, ...scopeParams(turn.scope) },
            { signal: controller.signal, accept: "text/event-stream" },
          );
      } catch (err) {
        if (controller.signal.aborted || !turn.run) throw err;
        continue;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(again && response.status === 404 ? RUN_GONE : data.error || `The question couldn't be asked (${response.status}).`);
      }
      try {
        await readEvents(response, onEvent);
      } catch (err) {
        if (controller.signal.aborted || !turn.run) throw err;
      }
      if (!turn.run) break;
    }
    if (turn.status === "running") {
      Object.assign(turn, { status: "failed", error: "The answer stopped partway. Is `journal browse` still running?" });
    }
    delete turn.partial;
  } catch (err) {
    delete turn.partial;
    if (controller.signal.aborted) turn.status = "cancelled";
    else Object.assign(turn, { status: "failed", error: err.message === "Failed to fetch" ? OFFLINE : err.message });
  } finally {
    if (ask.controller === controller) ask.controller = null;
    if (turn.status === "done" || turn.status === "failed") tellAnswered(turn);
    changed();
    askNext();
  }
}

// Cancel tells the server to stop writing, which is what frees the model.
async function cancelTurn(turn) {
  if (turn.run) {
    try {
      await post(`ask/${turn.run}/cancel`);
      return;
    } catch {
      // it's gone already; stop listening for it
    }
  }
  ask.controller?.abort();
}

// after a reload: follow the answer that was being written, or ask what was waiting
function resumeAsking() {
  const running = ask.turns.find((t) => t.status === "running" && t.run);
  if (running) runTurn(running, true);
  else askNext();
}

// notifications are opt-in from the Ask page, and remembered in this browser
function notifyOn() {
  try {
    return localStorage.getItem("ask-notify") === "on" && "Notification" in window
      && Notification.permission === "granted";
  } catch {
    return false;
  }
}

function tellAnswered(turn) {
  // nothing to tell someone who's looking at it
  if (!notifyOn() || (document.visibilityState === "visible" && document.hasFocus())) return;
  const title = turn.status === "done" ? "Your answer is ready" : "A question couldn't be answered";
  const note = new Notification(title, { body: turn.question, tag: "commonplace-ask" });
  note.onclick = () => {
    window.focus();
    location.hash = "#/ask";
    note.close();
  };
}

function notifyToggle() {
  if (!("Notification" in window)) return null;
  const box = h("input", { type: "checkbox", checked: notifyOn() });
  const said = h("span", { class: "notify-said", role: "status" });
  box.addEventListener("change", async () => {
    said.textContent = "";
    if (!box.checked) {
      try { localStorage.setItem("ask-notify", "off"); } catch { /* not kept, then */ }
      return;
    }
    const answer = Notification.permission === "default"
      ? await Notification.requestPermission() : Notification.permission;
    if (answer !== "granted") {
      box.checked = false;
      said.textContent = "Your browser blocked notifications for this page; allow them in its site settings to use this.";
      return;
    }
    try { localStorage.setItem("ask-notify", "on"); } catch { /* not kept, then */ }
  });
  return h("label", { class: "notify-toggle" }, box, " Tell me when an answer is ready", said);
}

// the sentence of each cited day that has the question's words, to check the answer against
function sourceQuotes(answer) {
  const quoted = answer.sources.dates.filter((d) => d.quote);
  if (!quoted.length) return null;
  return h("details", { class: "turn-read" },
    h("summary", {}, `What those days say: ${plural(quoted.length, "line")} with the question's words`),
    h("ul", { class: "turn-quotes" }, quoted.map((d) => h("li", {},
      d.id == null
        ? h("span", { class: "line-date" }, mediumDate(d.date))
        : h("a", { class: "line-date", href: href.entry(d.id, d.section ? { at: d.section } : null) }, mediumDate(d.date)),
      h("q", {}, d.quote)))));
}

function sourceChips(answer) {
  const { dates, notes } = answer.sources;
  if (!dates.length && !notes.length) return null;
  return h("div", { class: "turn-sources" },
    h("span", { class: "gloss-label" }, "Sources"),
    h("div", { class: "chips" },
      dates.map((d) => d.id == null
        ? h("span", { class: "chip ghost" }, mediumDate(d.date))
        : d.section
          ? sectionChip(mediumDate(d.date), href.entry(d.id, { at: d.section }), d.section, `The part of ${longDate(d.date)} that matched`)
          : h("a", { class: "chip pencil", href: href.entry(d.id) }, mediumDate(d.date))),
      notes.map((n) => n.id != null
        ? h("a", { class: "chip pencil", href: href.note(n.id) }, n.title)
        : h("span", { class: "chip ghost" }, n.title))));
}

// The server draws the reading estimate as a text bar ("... ███░░ 25%, about 3
// minutes left"). Here it's a progress element next to the status, so screen
// readers hear the stage once instead of every second.
const READING_BAR = /^(.*?) [\u2588\u2591]+ (\d+)%, (.*)$/;

function stageView(stage) {
  const m = READING_BAR.exec(stage || "");
  if (!m) return [h("p", { class: "search-status", role: "status" }, stage)];
  return [
    h("p", { class: "search-status", role: "status" }, m[1]),
    h("span", { class: "reading-bar" },
      h("progress", { max: 100, value: m[2], "aria-label": "Estimated reading progress" }),
      ` ${m[2]}%, ${m[3]}`),
  ];
}

function duration(seconds) {
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const whole = Math.round(seconds);
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
  return `${Math.floor(whole / 3600)}h ${String(Math.floor((whole % 3600) / 60)).padStart(2, "0")}m`;
}

// " by qwen3:14b", for after "Answered from ..."
function byText(model) {
  return model ? ` by ${model}` : "";
}

// " in 4m 10s", for after "Answered from ..."
function tookText(timings) {
  if (!timings?.length) return "";
  return ` in ${duration(timings.reduce((sum, t) => sum + t.seconds, 0))}`;
}

// `holder` remembers whether it was opened, since a turn is redrawn as others come in
function timingsView(timings, holder = {}) {
  const shown = (timings || []).filter((t) => t.seconds >= 0.5);
  if (!shown.length) return null;
  const remember = (e) => { holder.timingsOpen = !e.currentTarget.parentElement.open; };
  return h("details", { class: "turn-read", open: holder.timingsOpen },
    h("summary", { onclick: remember }, "How long each part took"),
    h("table", { class: "turn-timings" },
      h("tbody", {}, shown.map((t) => h("tr", {},
        h("td", {}, t.step[0].toUpperCase() + t.step.slice(1)),
        h("td", { class: "num" }, duration(t.seconds)))))));
}

// what a thinking model reasoned before answering, with [ollama] show_thinking on
function thinkingView(turn) {
  if (!turn.thinking) return null;
  const open = turn.thinkingOpen ?? (turn.status === "running" && !turn.partial);
  // a click on the summary, since toggle also fires when a redraw opens it
  const remember = (e) => { turn.thinkingOpen = !e.currentTarget.parentElement.open; };
  return h("details", { class: "turn-read turn-thinking", open },
    h("summary", { onclick: remember },
      turn.status === "running" && !turn.partial ? "Its thinking so far" : "Its thinking"),
    h("div", { class: "turn-thinking-text md" }, md(turn.thinking)));
}

function turnView(turn, scale, index) {
  const parts = [h("p", { class: "turn-q" }, turn.question)];
  if (turn.scope) parts.push(h("p", { class: "turn-rewrite" }, `${turn.scope.label}.`));
  if (turn.rewritten) parts.push(h("p", { class: "turn-rewrite" }, "Read as: ", h("q", {}, turn.rewritten)));
  if (turn.status !== "failed") parts.push(thinkingView(turn));
  if (turn.status === "queued") {
    const ahead = ask.turns.slice(0, index).filter((t) => t.status === "running" || t.status === "queued").length;
    parts.push(h("div", { class: "turn-running" },
      h("p", { class: "search-status" }, ahead > 1 ? `Waiting for the ${ahead} questions above.` : "Waiting for the question above."),
      h("button", {
        class: "button", type: "button",
        onclick: () => { ask.turns.splice(ask.turns.indexOf(turn), 1); changed(); },
      }, "Remove")));
  } else if (turn.status === "running") {
    if (turn.partial) parts.push(h("div", { class: "turn-answer md writing" }, md(turn.partial)));
    parts.push(h("div", { class: "turn-running" },
      ...stageView(turn.partial ? "Writing…" : turn.stage),
      h("button", { class: "button", type: "button", onclick: () => cancelTurn(turn) }, "Cancel")));
  } else if (turn.status === "failed") {
    parts.push(h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't answer"), h("span", {}, turn.error)));
  } else if (turn.status === "cancelled") {
    parts.push(h("p", { class: "turn-meta" }, "Cancelled before it finished."));
  } else {
    const a = turn.answer;
    const readCount = a.entries.length + a.notes.length;
    parts.push(
      h("div", { class: "turn-answer md" }, md(a.body)),
      sourceChips(a),
      sourceQuotes(a),
      h("p", { class: "turn-meta" }, `Answered ${STAGE_LABELS[a.stage] || a.stage}${byText(a.model)}${tookText(a.timings)}.`),
      timingsView(a.timings, turn),
      readCount ? h("details", { class: "turn-read" },
        h("summary", {}, `What it read: ${plural(a.entries.length, "entry", "entries")}, ${plural(a.notes.length, "note")}`),
        a.notes.length ? h("div", { class: "note-grid" }, a.notes.map((n) =>
          h("a", { class: "note-card", href: href.note(n.id) },
            h("div", { class: "note-title" }, n.title),
            h("div", { class: "note-aliases" }, kindLabel(n.kind)),
            n.summary ? h("div", { class: "note-summary" }, n.summary) : null))) : null,
        a.entries.length ? entryLines(a.entries, scale) : null) : null);
  }
  return h("article", { class: "turn", "aria-label": `Question ${index + 1}` }, parts);
}

async function askView(_, params) {
  const cat = await getCatalog();
  const scale = cat.mood_scale;
  document.title = "Ask · Commonplace";
  const thread = h("div", { class: "thread", "aria-live": "polite" });
  const start = h("div", { class: "ask-empty" });
  const input = h("input", {
    type: "text", placeholder: "When did I last see John? How was March?", "aria-label": "Your question",
    autocomplete: "off", maxlength: 2000,
  });
  const go = h("button", { class: "button primary", type: "submit" }, "Ask");
  const reset = h("button", { class: "button", type: "button" }, "New conversation");
  const hint = h("span", { class: "section-note" });
  const scopeSlot = h("div", { class: "ask-scope" });
  const form = h("form", { class: "search-box ask-box", role: "search" }, input, go);
  const earlier = earlierQuestions();

  // a question from the jump box is about everything; one from a page, about what it shows
  const fromLink = scopeFrom(params);
  if (fromLink) ask.scope = { ...fromLink, label: scopeLabel(fromLink, cat) };
  else if ((params.get("q") || "").trim()) ask.scope = null;

  const draw = () => {
    const busy = askBusy();
    go.textContent = busy ? "Queue" : "Ask";
    go.title = busy ? "Ask this once the questions above are answered" : "";
    reset.hidden = !ask.turns.length || busy;
    fill(scopeSlot, ask.scope
      ? h("span", { class: "chip pencil" }, ask.scope.label,
        h("button", {
          type: "button", "aria-label": "Ask about the whole journal again",
          title: "Ask about the whole journal again",
          onclick: () => { ask.scope = null; changed(); input.focus(); },
        }, "×"))
      : null);
    hint.textContent = ask.turns.some((t) => t.status === "done")
      ? "A follow-up like “and the month after?” builds on the answers above."
      : "Needs Ollama running. An answer can take a few minutes.";
    start.hidden = earlier.hidden = ask.turns.length > 0;
    fill(thread, ask.turns.map((t, i) => turnView(t, scale, i)));
  };
  ask.redraw = () => {
    if (thread.isConnected) draw();
  };

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    askQuestion(q);
    // the new question is at the bottom, by the box it was typed in
    requestAnimationFrame(() => window.scrollTo({ top: document.documentElement.scrollHeight }));
  });
  reset.addEventListener("click", () => {
    ask.turns = [];
    ask.conversation = newConversation();
    changed();
    input.focus();
  });

  fill(start,
    h("p", {}, "Ask in plain words. The answer cites the days and notes it drew on, lists everything it read, and you can open each one."));

  const preset = (params.get("q") || "").trim();
  if (preset || fromLink) {
    replaceParams("/ask", {});
    saveTurns();
  }
  if (preset) askQuestion(preset);
  draw();
  setTimeout(() => input.focus({ preventScroll: true }), 0);
  return h("div", { class: "ask-page" },
    pageHead({ title: "Ask", actions: [reset, h("a", { class: "button", href: href.historyList() }, "Earlier questions")] }),
    start, thread, earlier,
    h("div", { class: "composer" },
      scopeSlot, form,
      h("div", { class: "ask-foot" }, hint, notifyToggle())));
}

// ---------------------------------------------------------------- earlier questions

const ASK_PAGE_QUESTIONS = 5;
const answerBody = (text) => text.replace(/\n\nSources: .*$/s, "");
const askBusy = () => ask.turns.some((t) => t.status === "running" || t.status === "queued");

function questionLine(k, onForget) {
  return h("li", { class: "line-row" },
    h("a", { class: "line", href: href.history(k.id) },
      h("span", { class: "line-date" }, stamp(k.asked_at)),
      h("span", { "aria-hidden": "true" }),
      h("span", { class: "line-summary" }, k.question),
      k.scope ? h("span", { class: "line-tags" }, `only ${k.scope}`) : null),
    onForget && h("button", {
      class: "button quiet", type: "button", "aria-label": `Forget “${k.question}”`, onclick: () => onForget(k),
    }, "Forget"));
}

async function forgetQuestion(k) {
  const sure = await confirmAction({
    title: "Forget this question?",
    body: `“${k.question}” and its answer are deleted from your history for good.`,
    action: "Forget it",
  });
  if (!sure) return false;
  try {
    await post(`history/${k.id}/delete`);
  } catch (err) {
    toast(err.message, "error");
    return false;
  }
  // it's still on the Ask page if it was asked in this tab
  ask.turns = ask.turns.filter((t) => t.answer?.history_id !== k.id);
  saveTurns();
  toast("Forgotten.");
  return true;
}

async function clearHistory(total) {
  const sure = await confirmAction({
    title: `Delete all ${plural(total, "question")}?`,
    body: "The history file is deleted with every question and answer in it, and the conversation on the Ask page is cleared. This can't be undone.",
    action: "Delete them all",
  });
  if (!sure) return false;
  try {
    await post("history/clear");
  } catch (err) {
    toast(err.message, "error");
    return false;
  }
  ask.turns = ask.turns.filter((t) => t.status === "running" || t.status === "queued");
  ask.conversation = newConversation();
  saveTurns();
  toast(`Deleted ${plural(total, "question")}.`);
  return true;
}

// filled in once the list arrives, so the Ask page doesn't wait on it
function earlierQuestions() {
  const slot = h("div", {});
  api("history", { limit: ASK_PAGE_QUESTIONS }).then((data) => {
    if (!data.items.length) return;
    fill(slot, section("Lately you asked", h("ol", { class: "lines" }, data.items.map((k) => questionLine(k))), {
      note: data.enabled
        ? h("a", { href: href.historyList() }, data.total > data.items.length ? `All ${plural(data.total, "question")}` : "Manage")
        : "history is off, so nothing new is kept",
    }));
  }).catch(() => {});
  return slot;
}

async function historyListView() {
  const data = await api("history", { limit: 500 });
  document.title = "Earlier questions · Commonplace";
  const head = pageHead({
    eyebrow: h("a", { href: "#/ask" }, "Ask"),
    title: "Earlier questions",
    lede: data.enabled
      ? "Every question you asked is kept with its answer, in a file of its own next to the cache. Forgetting one overwrites it, so it's gone for good."
      : "History is off, so nothing new is kept. The questions from before are still here until you delete them.",
  });
  if (!data.items.length) {
    return h("div", {}, head, empty(data.enabled
      ? ["No questions kept yet. ", h("a", { href: "#/ask" }, "Ask one"), " and it shows up here."]
      : ["Nothing is kept. Set ", h("code", {}, "enabled = true"), " under ", h("code", {}, "[history]"), " in config.toml to keep your questions and their answers."]));
  }
  const list = h("ol", { class: "lines" });
  const count = h("p", { class: "result-count", role: "status" });
  const filter = h("input", { type: "search", placeholder: "Words in the question", autocomplete: "off" });
  let items = data.items;
  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = q ? items.filter((k) => k.question.toLowerCase().includes(q)) : items;
    count.textContent = q
      ? `${plural(shown.length, "question")} of ${fmtNum(items.length)}`
      : `${plural(items.length, "question")}, newest first`;
    fill(list, shown.length
      ? shown.map((k) => questionLine(k, async (kept) => {
        if (await forgetQuestion(kept)) {
          items = items.filter((x) => x.id !== kept.id);
          if (items.length) draw();
          else render();
        }
      }))
      : h("li", {}, empty("No question has those words.")));
  };
  filter.addEventListener("input", debounce(draw, 80));
  draw();
  const clear = h("button", { class: "button danger", type: "button", onclick: async () => {
    if (await clearHistory(items.length)) render();
  } }, "Delete all");
  return h("div", {}, head,
    h("div", { class: "filters" }, field("Filter", filter, true), clear),
    count, list);
}

// a kept question as a turn on the Ask page, so it can be followed up
function keptTurn(k) {
  return {
    question: k.question, rewritten: k.standalone || undefined, status: "done",
    scope: k.scope ? { label: `Only ${k.scope}` } : null,
    answer: {
      body: answerBody(k.answer), text: k.answer, sources: k.sources, stage: k.stage, model: k.model,
      timings: k.timings, entries: [], notes: [], history_id: k.id,
    },
  };
}

function keptAnswer(k) {
  return [
    k.standalone ? h("p", { class: "turn-rewrite" }, "Read as: ", h("q", {}, k.standalone)) : null,
    h("div", { class: "turn-answer md" }, md(answerBody(k.answer))),
    sourceChips(k),
    h("p", { class: "turn-meta" }, `Answered ${STAGE_LABELS[k.stage] || k.stage}${byText(k.model)}${tookText(k.timings)}.`),
    timingsView(k.timings),
  ];
}

async function historyView([id]) {
  const k = await api(`history/${id}`);
  document.title = `${k.question} · Commonplace`;
  const where = { ask: "journal ask", chat: "journal ask", browse: "the Ask page" }[k.source] || k.source;
  const follow = h("button", { class: "button primary", type: "button", disabled: askBusy(), title: askBusy() ? "A question is being answered on the Ask page" : null, onclick: () => {
    ask.turns = [...k.earlier, k].map(keptTurn);
    ask.conversation = k.conversation;
    ask.scope = null;
    saveTurns();
    location.hash = "#/ask";
  } }, "Follow up");
  const forget = h("button", { class: "button", type: "button", onclick: async () => {
    if (await forgetQuestion(k)) location.hash = href.historyList();
  } }, "Forget");
  return h("div", { class: "kept" },
    pageHead({
      eyebrow: h("a", { href: href.historyList() }, "Earlier questions"),
      title: k.question,
      meta: [h("span", {}, `Asked ${stamp(k.asked_at)} in ${where}`), k.scope ? h("span", {}, `only ${k.scope}`) : null],
      actions: [follow, forget],
    }),
    k.earlier.length ? h("details", { class: "turn-read kept-earlier" },
      h("summary", {}, `${plural(k.earlier.length, "question")} came before this one in the same conversation`),
      h("div", { class: "thread" }, k.earlier.map((e) => h("article", { class: "turn" },
        h("p", { class: "turn-q" }, h("a", { href: href.history(e.id) }, e.question)), keptAnswer(e))))) : null,
    keptAnswer(k),
    h("p", { class: "section-note" }, "The days and notes it cited may have changed since."));
}

// ---------------------------------------------------------------- insights and writing prompts

// One model call that's written in the background, like an answer. The id is
// kept for this tab, so a reload follows it again where a new one would start.
async function thinkRun(key, path, body, onEvent) {
  const forget = () => { try { sessionStorage.removeItem(key); } catch { /* nothing kept */ } };
  let response;
  if (body) {
    response = await send(path, body, { accept: "text/event-stream" });
  } else {
    let id = null;
    try { id = sessionStorage.getItem(key); } catch { /* nothing kept */ }
    if (!id) return false;
    response = await fetch(`/api/ask/${id}/events`, { headers: { Accept: "text/event-stream" } });
  }
  if (!response.ok) {
    forget();
    if (!body) return false;
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `That couldn't be started (${response.status}).`);
  }
  await readEvents(response, (event, data) => {
    if (event === "run") {
      try { sessionStorage.setItem(key, data.id); } catch { /* not kept, then */ }
    } else {
      if (event !== "stage") forget();
      onEvent(event, data);
    }
  });
  return true;
}

// the shared shape of both pages: a few boxes, a button, and what came back
function thinkPage({ key, path, title, lede, controls, read, button, show }) {
  const out = h("div", { "aria-live": "polite" });
  const go = h("button", { class: "button primary", type: "submit" }, button);
  const onEvent = (event, data) => {
    if (event === "stage") fill(out, h("div", { class: "turn-running" }, h("p", { class: "search-status", role: "status" }, data.text)));
    else if (event === "result") fill(out, show(data));
    else if (event === "problem") fill(out, h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't do that"), h("span", {}, data.error)));
    if (event !== "stage") go.disabled = false;
  };
  const run = async (body) => {
    go.disabled = true;
    try {
      const followed = await thinkRun(key, path, body, onEvent);
      if (!followed) go.disabled = false;
    } catch (err) {
      onEvent("problem", { error: err.message });
    }
  };
  const form = h("form", { class: "filters" }, controls, go);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(read());
  });
  // one that was being written when the page reloaded
  run(null);
  return h("div", { class: "kept" },
    pageHead({ title, lede }), form, out);
}

async function insightsView() {
  document.title = "Insights · Commonplace";
  const from = h("input", { type: "date" });
  const to = h("input", { type: "date" });
  return thinkPage({
    key: "think-insights", path: "insights", title: "Insights", button: "Look for patterns",
    lede: "Patterns you didn't ask about: tags and people your mood runs high or low around, days of the week, seasons, and the numbers you keep. They're worked out from the cache first, then a model puts the ones that stand out into words. They're correlations, not causes.",
    controls: [field("From", from), field("To", to)],
    read: () => ({ from: from.value, to: to.value }),
    show: (d) => [
      h("div", { class: "turn-answer md" }, md(d.text)),
      d.signals.length ? section("What it looked at", h("ul", { class: "plain" }, d.signals.map((s) => h("li", {}, s))), {
        note: `${plural(d.entries, "scored entry", "scored entries")}`,
      }) : null,
    ],
  });
}

async function promptsView() {
  document.title = "Writing prompts · Commonplace";
  const days = h("input", { type: "number", min: 1, max: 365, value: 14 });
  const count = h("input", { type: "number", min: 1, max: 12, value: 4 });
  return thinkPage({
    key: "think-prompts", path: "prompts", title: "Writing prompts", button: "Suggest some",
    lede: "Things to write about next, picked up from threads your recent entries left open: a project you named, someone who keeps coming up, a feeling you didn't finish with.",
    controls: [field("Days to look back", days), field("How many", count)],
    read: () => ({ days: Number(days.value) || 14, count: Number(count.value) || 4 }),
    show: (d) => (d.prompts.length
      ? h("ol", { class: "prompt-list" }, d.prompts.map((p) => h("li", { class: "md" }, md(p))))
      : empty(`Nothing written in the last ${plural(d.days, "day")} to draw from.`)),
  });
}

// ---------------------------------------------------------------- health and setup

const STATE_WORDS = { ok: "Fine", warn: "Worth a look", fail: "Needs fixing" };

async function healthView() {
  document.title = "Health · Commonplace";
  const body = h("div", {}, empty("Checking…"));
  const run = async () => {
    fill(body, empty("Checking…"));
    let data;
    try {
      data = await api("health");
    } catch (err) {
      fill(body, h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't check"), h("span", {}, err.message)));
      return;
    }
    const wrong = data.checks.filter((c) => c.state !== "ok");
    fill(body,
      h("p", { class: "lede" }, wrong.length
        ? `${plural(wrong.length, "thing")} to look at, out of ${fmtNum(data.checks.length)} checked.`
        : `All ${fmtNum(data.checks.length)} checks are fine.`),
      h("ul", { class: "upkeep-rows health" }, data.checks.map((c) => h("li", { class: "upkeep-row" },
        h("div", { class: "upkeep-what" },
          h("h3", {}, c.name, " ", h("span", { class: c.state === "ok" ? "status" : c.state === "fail" ? "status error" : "status pending" }, STATE_WORDS[c.state])),
          h("p", {}, c.said),
          c.command ? h("p", {}, "In a terminal: ", h("code", {}, c.command)) : null,
          c.fix ? h("p", {}, "In config.toml: ", h("code", {}, c.fix.line)) : null),
        c.fix ? h("div", { class: "upkeep-do" }, h("button", { class: "button", type: "button", onclick: async () => {
          try {
            await post("settings/save", { values: { [c.fix.section]: { [c.fix.key]: c.fix.value } } });
            toast("Saved to config.toml.");
            run();
          } catch (err) {
            toast(err.message, "error");
          }
        } }, "Use this")) : null))));
  };
  run();
  return h("div", { class: "upkeep" },
    pageHead({
      title: "Health",
      lede: "Whether Commonplace can reach what it needs: your journal folder, Ollama and its models, and how your settings sit with Obsidian's.",
      actions: h("button", { class: "button", type: "button", onclick: run }, "Check again"),
    }),
    body);
}

// a box to type a model in, with Ollama's own list to pick from
function modelBox(id, value, models) {
  const list = h("datalist", { id: `${id}-list` }, models.map((m) => h("option", { value: m })));
  const box = h("input", { id, type: "text", value, list: `${id}-list`, spellcheck: "false", autocomplete: "off" });
  return [box, list];
}

async function setupView() {
  document.title = "Set up · Commonplace";
  const found = await api("models").catch(() => ({ reachable: false, models: [], host: "", error: OFFLINE }));
  const embedders = found.models.filter((m) => /embed/i.test(m));
  const writers = found.models.filter((m) => !/embed/i.test(m));
  const folder = h("input", { id: "setup-folder", type: "text", placeholder: "C:/Users/you/Documents/Journal", spellcheck: "false", autocomplete: "off" });
  const seen = h("p", { class: "setting-about", role: "status" });
  const problem = h("p", { class: "setting-problem", role: "alert" });
  const [fast, fastList] = modelBox("setup-fast", writers[0] || "", writers);
  const [precise, preciseList] = modelBox("setup-precise", writers[0] || "", writers);
  const [embed, embedList] = modelBox("setup-embed", embedders[0] || "", found.models);
  const save = h("button", { class: "button primary", type: "submit" }, "Save and open Commonplace");

  const look = debounce(async () => {
    try {
      const got = await post("setup/look", { path: folder.value });
      fill(seen, got.said, got.sample?.length ? [" Like ", got.sample.slice(0, 3).map((name, i) => [i ? ", " : "", h("code", {}, name)]), "."] : null,
        got.obsidian ? " It's an Obsidian vault." : null);
    } catch (err) {
      seen.textContent = err.message;
    }
  }, 350);
  folder.addEventListener("input", look);

  const form = h("form", { class: "settings" },
    h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, "Your journal")),
      h("div", { class: "setting" },
        h("label", { for: "setup-folder" }, "The folder your journal is in"),
        folder, seen,
        h("p", { class: "setting-about" }, "Paste its path from File Explorer's address bar. It's only ever read; nothing is written to it."))),
    h("section", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, "Models"),
        h("span", { class: "section-note" }, found.reachable ? `${plural(found.models.length, "model")} in Ollama` : null)),
      found.reachable ? null : h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Ollama isn't answering"),
        h("span", {}, "Start Ollama and reload this page to pick from its models, or type their names. You can also leave these and change them later on the Settings page.")),
      h("div", { class: "setting" }, h("label", { for: "setup-fast" }, "Fast model"), fast, fastList,
        h("p", { class: "setting-about" }, "Reads each entry and answers from summaries. A small one is quicker. Empty uses llama3.2:3b.")),
      h("div", { class: "setting" }, h("label", { for: "setup-precise" }, "Precise model"), precise, preciseList,
        h("p", { class: "setting-about" }, "Writes digests, your profile and answers from full text. Can be the same model. Empty uses qwen3:14b.")),
      h("div", { class: "setting" }, h("label", { for: "setup-embed" }, "Embedding model"), embed, embedList,
        h("p", { class: "setting-about" }, "Makes the vectors for search by meaning. Changing it later means rebuilding the cache. Empty uses qwen3-embedding:0.6b."))),
    h("div", { class: "save-bar" }, problem, save));

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    problem.textContent = "";
    save.disabled = true;
    try {
      await post("setup/save", { vault: folder.value, fast_model: fast.value, precise_model: precise.value, embed_model: embed.value });
      setupMode = false;
      document.body.classList.remove("setting-up");
      toast("Saved. Next, read your journal.");
      location.hash = "#/upkeep";
      render();
    } catch (err) {
      problem.textContent = err.message;
      save.disabled = false;
      folder.focus();
    }
  });
  setTimeout(() => folder.focus(), 0);
  return h("div", {},
    pageHead({
      title: "Set up Commonplace",
      lede: "Two things to say before it can read your journal: where the journal is, and which Ollama models to use. The answers go in a config.toml you can change later on the Settings page.",
    }),
    form);
}

// ---------------------------------------------------------------- settings

const EFFECT_LABELS = {
  now: null, index: "next time files are read", reread: "files read from now on",
  rebuild: "needs a rebuild", restart: "a different file",
};

// one setting's control, with how to read its value back out
function settingControl(f, id) {
  const common = { id, "aria-describedby": `${id}-about` };
  if (f.kind === "bool") {
    const box = h("input", { ...common, type: "checkbox", checked: f.value === true });
    return { el: box, read: () => box.checked };
  }
  if (f.kind === "maybe") {
    const pick = h("select", common,
      h("option", { value: "", selected: f.value == null }, "Leave it to the model"),
      h("option", { value: "true", selected: f.value === true }, "On"),
      h("option", { value: "false", selected: f.value === false }, "Off"));
    return { el: pick, read: () => (pick.value === "" ? null : pick.value === "true") };
  }
  if (f.kind === "choice") {
    const pick = h("select", common, f.choices.map((c) => h("option", { value: c, selected: c === f.value }, c)));
    return { el: pick, read: () => pick.value };
  }
  if (f.kind === "int" || f.kind === "float") {
    const box = h("input", { ...common, type: "number", value: f.value, min: f.min, max: f.max, step: f.kind === "int" ? 1 : "any" });
    return { el: box, read: () => (box.value.trim() === "" ? null : Number(box.value)) };
  }
  if (f.kind === "list") {
    const box = h("textarea", { ...common, rows: Math.min(8, Math.max(2, f.value.length + 1)), spellcheck: "false", placeholder: "One on each line" }, f.value.join("\n"));
    return { el: box, read: () => box.value.split("\n").map((x) => x.trim()).filter(Boolean) };
  }
  if (f.kind === "pairs") {
    const lines = Object.entries(f.value).map(([k, v]) => `${k} = ${v}`);
    const box = h("textarea", { ...common, rows: Math.min(8, Math.max(2, lines.length + 1)), spellcheck: "false", placeholder: "this = that, one on each line" }, lines.join("\n"));
    return { el: box, read: () => Object.fromEntries(box.value.split("\n").map((line) => line.split("=").map((x) => x.trim())).filter((p) => p.length === 2 && p[0] && p[1])) };
  }
  const box = h("input", { ...common, type: "text", value: f.value ?? "", spellcheck: "false", autocomplete: "off" });
  return { el: box, read: () => box.value };
}

async function settingsView() {
  let data = await api("settings");
  const pulled = (await api("models").catch(() => ({ models: [] }))).models;
  document.title = "Settings · Commonplace";
  const body = h("div", {});
  const bar = h("div", { class: "save-bar" });
  let controls = [];

  const changes = () => {
    const out = {};
    let n = 0;
    for (const c of controls) {
      const now = c.read();
      if (JSON.stringify(now) === JSON.stringify(c.field.value ?? (c.field.kind === "text" ? "" : null))) continue;
      (out[c.section] ||= {})[c.field.key] = now;
      n += 1;
    }
    return { out, n };
  };

  const drawBar = (said) => {
    const { n } = changes();
    fill(bar,
      h("span", { class: "save-said", role: "status" }, said || (n ? `${plural(n, "change")} not saved` : data.writable ? "Nothing changed" : null)),
      h("button", { class: "button", type: "button", disabled: !n, onclick: () => draw() }, "Undo"),
      h("button", { class: "button primary", type: "button", disabled: !n || !data.writable, onclick: save }, "Save"));
  };

  const save = async () => {
    const { out } = changes();
    const response = await send("settings/save", { values: out }).catch((err) => ({ ok: false, json: async () => ({ error: err.message }) }));
    const reply = await response.json().catch(() => ({}));
    for (const c of controls) c.problem.textContent = "";
    if (!response.ok) {
      const fields = reply.fields || {};
      let first = null;
      for (const c of controls) {
        const said = fields[`${c.section}.${c.field.key}`];
        if (!said) continue;
        c.problem.textContent = said;
        first ||= c.el;
      }
      first?.focus();
      // a rule about two settings together belongs to no one box
      const named = new Set(controls.map((c) => `${c.section}.${c.field.key}`));
      const loose = Object.entries(fields).filter(([name]) => !named.has(name)).map(([name, said]) => `[${name}] ${said}.`);
      drawBar([reply.error || "That couldn't be saved.", ...loose].join(" "));
      return;
    }
    const effects = reply.costs.filter((e) => e !== "now").map((e) => reply.effects[e]);
    data = reply;
    draw(`Saved to config.toml.${effects.length ? ` ${effects.join(" ")}` : ""}`);
    toast("Settings saved.");
  };

  const draw = (said) => {
    controls = [];
    fill(body, data.sections.map((s) => h("section", { class: "section settings-section", id: `settings-${s.name}` },
      h("div", { class: "section-head" }, h("h2", {}, s.title), h("span", { class: "section-note" }, h("code", {}, `[${s.name}]`))),
      s.fields.map((f) => {
        const id = `set-${s.name}-${f.key}`;
        const control = settingControl(f, id);
        const problem = h("p", { class: "setting-problem", role: "alert" });
        const picks = s.name === "ollama" && f.key.endsWith("_model") && pulled.length
          ? h("datalist", { id: `${id}-list` }, pulled.map((m) => h("option", { value: m }))) : null;
        if (picks) control.el.setAttribute("list", `${id}-list`);
        control.el.disabled = !data.writable;
        control.el.addEventListener("input", () => drawBar());
        controls.push({ ...control, field: f, section: s.name, problem });
        const cost = EFFECT_LABELS[f.effect];
        return h("div", { class: f.kind === "bool" ? "setting check" : "setting" },
          h("label", { for: id }, h("code", {}, f.key), cost ? h("span", { class: "status", title: data.effects[f.effect] }, cost) : null),
          control.el, picks,
          h("p", { class: "setting-about", id: `${id}-about` }, f.about),
          problem);
      }))));
    drawBar(said);
  };

  draw();
  return h("div", { class: "settings" },
    pageHead({
      title: "Settings",
      lede: data.path
        ? ["Everything in ", h("code", {}, "config.toml"), ". Saving changes only the lines you changed and keeps your comments. The file as it was is kept next to it."]
        : "Commonplace was started without a config file, so these can be read here but not changed.",
      meta: data.path ? [h("span", { class: "path" }, data.path)] : null,
    }),
    data.path && !data.writable ? h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Can't save"), h("span", {}, "The config file can't be written to from here.")) : null,
    h("nav", { class: "chips settings-jump", "aria-label": "Jump to a part" }, data.sections.map((s) => h("a", { class: "chip", href: `#settings-${s.name}`, onclick: (ev) => {
      ev.preventDefault();
      document.getElementById(`settings-${s.name}`).scrollIntoView({ block: "start" });
    } }, s.title))),
    body, bar);
}

// ---------------------------------------------------------------- upkeep

const upkeepCount = document.getElementById("upkeep-count");

// "In line" on the page that asked, and the way to watch it
async function startUpkeep(kind, params, { quiet } = {}) {
  try {
    const job = await post("upkeep/start", { kind, ...params });
    if (!quiet) toast(job.status === "running" ? "Started. Upkeep shows how it's going." : "In line. Upkeep shows how it's going.");
    watchUpkeep();
    return job;
  } catch (err) {
    toast(err.message, "error");
    return null;
  }
}

function upkeepButton(label, kind, params = {}) {
  return h("button", { class: "button", type: "button", onclick: async () => {
    if (await startUpkeep(kind, params, { quiet: true })) location.hash = "#/upkeep";
  } }, label);
}

// Keeping up is remembered in this browser, and turned back on when a page
// loads after `journal browse` was restarted.
async function setWatching(on, { quiet } = {}) {
  try {
    await post("upkeep/watch", { on });
    try { localStorage.setItem("upkeep-watch", on ? "on" : "off"); } catch { /* not remembered, then */ }
  } catch (err) {
    if (!quiet) toast(err.message, "error");
  }
}

async function resumeWatching() {
  let wanted = false;
  try { wanted = localStorage.getItem("upkeep-watch") === "on"; } catch { /* storage refused */ }
  if (!wanted) return;
  const data = await api("upkeep").catch(() => null);
  if (data && !data.watching.on) setWatching(true, { quiet: true });
}

// the mark by Upkeep in the list of sections while something runs
let upkeepTimer = null;

async function watchUpkeep() {
  clearTimeout(upkeepTimer);
  let busy = false;
  try {
    const data = await api("upkeep");
    busy = Boolean(data.running || data.queued.length);
    upkeepCount.textContent = data.running ? "running" : data.watching.on ? "watching" : "";
    busy = busy || data.watching.on;
  } catch {
    upkeepCount.textContent = "";
  }
  if (busy) upkeepTimer = setTimeout(watchUpkeep, 5000);
}

function jobPanel(job, redraw) {
  const known = job.total > 0;
  const left = job.seconds_left != null ? `, about ${duration(job.seconds_left)} left` : "";
  const stop = h("button", { class: "button", type: "button", disabled: job.stopping, onclick: async () => {
    await post("upkeep/stop").catch((err) => toast(err.message, "error"));
    redraw();
  } }, job.stopping ? "Stopping after this step…" : "Stop after this step");
  return h("section", { class: "job-running", "aria-live": "polite" },
    h("div", { class: "job-head" },
      h("h2", {}, job.label),
      known ? h("span", { class: "section-note" }, `${fmtNum(job.done)} of ${fmtNum(job.total)}${left}`) : null),
    known
      ? h("progress", { class: "job-bar", max: job.total, value: job.done, "aria-label": "How far it is" })
      : h("progress", { class: "job-bar", "aria-label": "Working" }),
    h("p", { class: "job-message" }, job.message || "Starting…",
      job.paused >= 1 ? ` Waited ${duration(job.paused)} for questions.` : null),
    h("div", { class: "job-actions" }, stop,
      h("span", { class: "section-note" }, "Stopping is safe. What's done is kept, and the rest stays on this page.")));
}

async function upkeepView() {
  let data = await api("upkeep");
  document.title = "Upkeep · Commonplace";
  const body = h("div", {});
  let timer = null;

  const refresh = async () => {
    clearTimeout(timer);
    if (!body.isConnected) return;
    try {
      data = await api("upkeep");
      draw();
    } catch {
      // the next try may get through
    }
    timer = setTimeout(refresh, data.running || data.queued.length ? 1500 : data.watching.on ? 4000 : 15000);
    watchUpkeep();
  };

  const start = async (kind, params, sure) => {
    if (sure && !(await confirmAction(sure))) return;
    await startUpkeep(kind, params || {}, { quiet: true });
    refresh();
  };

  // a row's right-hand side: how its job stands, or the button that starts one
  const control = (kind, label, params) => {
    const same = (j) => j && j.kind === kind && Boolean(j.params.rebuild) === Boolean(params?.rebuild);
    if (same(data.running)) return h("span", { class: "status pending" }, "Running");
    const waiting = data.queued.find(same);
    if (waiting) {
      return [h("span", { class: "status" }, "In line"),
        h("button", { class: "button quiet", type: "button", onclick: async () => {
          await post(`upkeep/${waiting.id}/remove`).catch((err) => toast(err.message, "error"));
          refresh();
        } }, "Remove")];
    }
    return h("button", { class: "button", type: "button", disabled: Boolean(data.elsewhere), onclick: () => start(kind, params) }, label);
  };

  // the files row comes first: digests and the profile are written from what's been read
  const row = (title, detail, ...right) => h("li", { class: "upkeep-row" },
    h("div", { class: "upkeep-what" }, h("h3", {}, title), h("p", {}, detail)),
    h("div", { class: "upkeep-do" }, right));

  const draw = () => {
    const d = data.digests;
    const p = data.profile;
    const t = data.themes;
    const profileWaiting = [
      p.unread ? `${plural(p.unread, "entry", "entries")} not read yet` : null,
      p.unfolded ? `${plural(p.unfolded, "new fact")} to add` : null,
    ].filter(Boolean);
    const f = data.files;
    const w = data.watching;
    const filesWaiting = [
      f.new ? `${fmtNum(f.new)} new` : null,
      f.changed ? `${fmtNum(f.changed)} changed` : null,
      f.retry ? `${fmtNum(f.retry)} that failed last time` : null,
      f.gone ? `${fmtNum(f.gone)} no longer there` : null,
    ].filter(Boolean);
    const others = data.queued.filter((j) => j.kind === "digest");
    fill(body,
      data.elsewhere ? h("div", { class: "notice info" },
        h("span", { class: "notice-label" }, "Busy elsewhere"),
        h("span", {}, `${data.elsewhere.what} is working on the cache in another window${data.elsewhere.since ? `, since ${stamp(data.elsewhere.since)}` : ""}. Upkeep can start here once it has finished.`)) : null,
      data.running ? jobPanel(data.running, refresh) : null,
      others.length ? h("p", { class: "section-note" }, `Next in line: ${others.map((j) => j.label).join(", ")}`) : null,
      h("ul", { class: "upkeep-rows" },
        row("Journal files", f.error ? f.error : filesWaiting.length
          ? `${filesWaiting.join(", ")}.${f.calls ? ` About ${plural(f.calls, "model call")}.` : ""}${f.wont_parse ? ` ${plural(f.wont_parse, "file")} won't parse as they are; Files says why afterwards.` : ""}`
          : f.tracked
            ? `All ${plural(f.tracked, "file")} in your journal folder ${f.tracked === 1 ? "has" : "have"} been read.`
            : "No files found in your journal folder. If it's on a drive that isn't connected, nothing is dropped from the cache.",
        f.error ? null : f.due ? control("index", "Read them") : null),
        row("Keep up as you write", w.on
          ? `On. A file you save is read once it has been left alone for ${duration(w.debounce)}${w.waiting ? `; ${plural(w.waiting, "file")} waiting for that now` : ""}. It stays on while journal browse is running.`
          : "Off. Files you save wait until you press Read them.",
        h("button", { class: "button", type: "button", disabled: Boolean(f.error), onclick: () => setWatching(!w.on).then(refresh) }, w.on ? "Turn off" : "Turn on")),
        row("Digests", d.due
          ? `${d.kinds.map((k) => k.text).join(", ")} to write. One call to the precise model for each.`
          : d.written ? "All up to date." : "None to write yet. They come once there are entries.",
        d.due ? control("digests", "Write them") : null),
        p.enabled
          ? row("Your profile", profileWaiting.length
            ? `${profileWaiting.join(", ")}. ${plural(p.calls, "call")} to the fast model, then one to the precise model.`
            : p.written ? "Up to date." : "Not written yet.",
          p.due || !p.written ? control("profile", p.written ? "Update" : "Write it") : null)
          : row("Your profile", "Turned off in config.toml."),
        row("Themes", t.made_at
          ? `Grouped ${stamp(t.made_at)} from ${plural(t.grouped, "entry", "entries")}${t.placed ? `, ${fmtNum(t.placed)} written since` : ""}.${t.stale ? " That's a lot since." : ""}`
          : "Not worked out yet. Grouping needs no model, and naming is one fast-model call for each theme.",
        data.entries ? control("themes", t.made_at ? (t.stale ? "Refresh" : "Refresh anyway") : "Find them") : null)),
      h("details", { class: "about" },
        h("summary", {}, "Start over"),
        h("ul", { class: "upkeep-rows" },
          row("Read every file again", "Every entry and note is read as if it were new. What the models said before is reused where nothing changed, so it's mostly quick unless a model or a prompt has changed.",
            h("button", { class: "button danger", type: "button", disabled: Boolean(data.elsewhere) || Boolean(f.error), onclick: () => start("index", { rebuild: true }, {
              title: "Read every file again?", action: "Read them all",
              body: "Each file is gone through again. Kept model results are reused, but anything a new model or prompt changes is asked for again, which on a CPU can take hours. You can stop it partway.",
            }) }, "Read all")),
          row("Rewrite every digest", "Every week, month, year and relationship digest is written again, with your profile as it is now. On a CPU this can take hours.",
            h("button", { class: "button danger", type: "button", disabled: Boolean(data.elsewhere), onclick: () => start("digests", { rebuild: true }, {
              title: "Rewrite every digest?", action: "Rewrite them",
              body: "Each one is a call to the precise model, so this can run for hours. You can stop it partway; the ones already rewritten are kept.",
            }) }, "Rewrite all")),
          p.enabled ? row("Rebuild your profile", "Writes the profile again from every fact, oldest first.",
            h("button", { class: "button danger", type: "button", disabled: Boolean(data.elsewhere), onclick: () => start("profile", { rebuild: true }, {
              title: "Rebuild your profile?", action: "Rebuild it",
              body: "The profile is written again from all the facts found so far. The one you have now is replaced.",
            }) }, "Rebuild")) : null)),
      data.recent.length ? section("Earlier", h("ol", { class: "lines" }, data.recent.map((j) => h("li", {},
        h("div", { class: "line" },
          h("span", { class: "line-date" }, stamp(j.finished_at)),
          h("span", { "aria-hidden": "true" }),
          h("span", { class: "line-summary" }, j.label),
          h("span", { class: j.status === "failed" ? "line-tags err" : "line-tags" }, j.error || j.result))))),
      { note: "since Commonplace was started" }) : null);
  };

  draw();
  timer = setTimeout(refresh, data.running || data.queued.length ? 1500 : 15000);
  return h("div", { class: "upkeep" },
    pageHead({
      title: "Upkeep",
      lede: "What's out of date, and a button to bring each up to date. They run one at a time, a question you ask goes first, and they carry on with this tab closed for as long as journal browse is running.",
    }),
    body);
}

// ---------------------------------------------------------------- you

const FACT_LABELS = {
  names: "Names", personality: "Personality", preferences: "Likes and dislikes",
  hobbies: "Hobbies and interests", routines: "Routines", work: "Work and studies",
  relationships: "People", values: "Values and goals", health: "Health", other: "Other",
};

async function youView() {
  const data = await api("profile");
  document.title = "You · Commonplace";
  const head = pageHead({
    eyebrow: "What your journal says about you",
    title: "You",
    lede: "A short profile the app keeps from your entries, in your own voice. Answers, digests, insights and writing prompts read it as background. Reading a single entry never uses it, so a wrong guess can't feed on itself.",
  });
  if (!data.enabled) {
    return h("div", {}, head, empty("The profile is turned off. Set ", h("code", {}, "enabled = true"), " under ", h("code", {}, "[profile]"), " in config.toml to keep one."));
  }

  const waiting = [];
  if (data.unread) waiting.push(plural(data.unread, "entry", "entries") + " not read yet");
  if (data.unfolded) waiting.push(plural(data.unfolded, "new fact") + " not added yet");
  const catchUp = waiting.length
    ? h("div", { class: "notice info" }, h("span", { class: "notice-label" }, "Catching up"),
      h("span", {}, `${waiting.join(", ")}. `, upkeepButton("Update your profile", "profile")))
    : null;

  const p = data.profile;
  const profileSection = p
    ? h("div", { class: "folio" },
      h("article", {},
        h("p", { class: "reading-label" }, "As the precise model wrote it from the facts below"),
        h("div", { class: "raw md" }, md(p.text))),
      h("aside", { class: "margin", "aria-label": "About this profile" },
        h("p", { class: "margin-title" }, "Where it comes from"),
        p.names && gloss("Names", h("p", { class: "gloss-summary" }, p.names)),
        gloss("Written", h("div", {}, stamp(p.updated_at)), h("div", { class: "quiet" }, `from ${plural(data.facts_count, "fact")}`)),
        gloss("Your own note", data.note
          ? [h("div", { class: "path" }, data.note), h("div", { class: "quiet" }, "Wins whenever it disagrees with what was inferred")]
          : h("span", { class: "quiet" }, "None set. Point [profile] note at a note about yourself to correct or add to this."))))
    : [empty(`No profile yet. Writing one reads your entries${data.unread ? ` (${plural(data.unread, "entry", "entries")})` : ""} first.`), upkeepButton("Write your profile", "profile")];

  // the same fact turns up on many days; show it once, at its latest day
  const groups = new Map();
  for (const f of data.facts) {
    if (!groups.has(f.category)) groups.set(f.category, new Map());
    const byText = groups.get(f.category);
    const key = f.fact.toLowerCase();
    if (!byText.has(key)) byText.set(key, { ...f, days: 0, waiting: false });
    const seen = byText.get(key);
    seen.days += 1;
    seen.waiting = seen.waiting || !f.folded;
  }
  const order = Object.keys(FACT_LABELS).filter((k) => groups.has(k));
  const factSections = order.map((key) => {
    const facts = [...groups.get(key).values()];
    return h("section", { class: "kind-group" },
      h("div", { class: "section-head" }, h("h2", {}, FACT_LABELS[key]), h("span", { class: "section-note" }, plural(facts.length, "fact"))),
      h("ol", { class: "lines" }, facts.map((f) => h("li", {},
        h(f.entry_id != null ? "a" : "div", { class: "line", href: f.entry_id != null ? href.entry(f.entry_id) : null },
          h("span", { class: "line-date" }, mediumDate(f.date)),
          h("span", { "aria-hidden": "true" }),
          h("span", { class: "line-summary" }, f.fact),
          h("span", { class: "line-tags" },
            f.days > 1 ? `on ${plural(f.days, "day")}, latest shown` : null,
            f.waiting && [f.days > 1 ? " · " : null, h("span", { class: "status pending" }, "not added yet")]))))));
  });

  return h("div", {}, head, catchUp, profileSection,
    data.facts.length
      ? section("What it's built from", factSections, { note: "newest first", intro: "Lasting facts the fast model found in your entries, each with the day it came from. When two disagree, the later one wins." })
      : null);
}

function notFoundView() {
  document.title = "Not found · Commonplace";
  return h("div", {}, pageHead({ title: "There's no page here" }),
    empty("Try the jump box above, or go back to the ", h("a", { href: href.overview() }, "overview"), "."));
}

function errorView(err) {
  document.title = "Error · Commonplace";
  const missing = err.status === 404;
  return h("div", { class: "error-page" },
    pageHead({ eyebrow: missing ? "Not found" : "Couldn't load this page", title: missing ? "Nothing here" : "Something went wrong" }),
    h("pre", {}, err.message || String(err)),
    h("p", {}, h("a", { class: "button", href: href.overview() }, "Back to the overview")));
}

// ---------------------------------------------------------------- router

const routes = [
  [/^\/?$/, overviewView, "overview"],
  [/^\/calendar(?:\/(\d{4}))?$/, calendarView, "calendar"],
  [/^\/entries$/, entriesView, "entries"],
  [/^\/entry\/(\d+)$/, entryView, "entries"],
  [/^\/notes$/, notesView, "notes"],
  [/^\/map$/, mapView, "notes"],
  [/^\/themes$/, themesView, "themes"],
  [/^\/theme\/(\d+)$/, themeView, "themes"],
  [/^\/note\/(\d+)$/, noteView, "notes"],
  [/^\/tags$/, tagsView, "tags"],
  [/^\/tag\/(.+)$/, tagView, "tags"],
  [/^\/metrics$/, metricsView, "metrics"],
  [/^\/metric\/(.+)$/, metricView, "metrics"],
  [/^\/digests(?:\/(year|month|week|range|notes))?$/, digestsView, "digests"],
  [/^\/digest\/(year|month|week|range)\/([^/]+)$/, digestView, "digests"],
  [/^\/compare\/([^/]+)(?:\/([^/]+))?$/, compareView, "digests"],
  [/^\/review\/([^/]+)$/, reviewView, "digests"],
  [/^\/upkeep$/, upkeepView, "upkeep"],
  [/^\/settings$/, settingsView, "settings"],
  [/^\/health$/, healthView, "health"],
  [/^\/insights$/, insightsView, "insights"],
  [/^\/prompts$/, promptsView, "prompts"],
  [/^\/files$/, filesView, "files"],
  [/^\/search$/, searchView, "search"],
  [/^\/ask$/, askView, "ask"],
  [/^\/history$/, historyListView, "ask"],
  [/^\/history\/(\d+)$/, historyView, "ask"],
  [/^\/you$/, youView, "you"],
];

let renderToken = 0;
// until there's a config file, every address shows the Setup page
let setupMode = false;
const sessionReady = api("session").then((s) => {
  sessionToken = s.token;
  setupMode = Boolean(s.setup);
  document.body.classList.toggle("setting-up", setupMode);
}).catch(() => {});
const linksReady = sessionReady.then(() => (setupMode ? null : getCatalog())).catch(() => null);

async function render() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, search = ""] = raw.split("?");
  const params = new URLSearchParams(search);
  const mine = ++renderToken;
  currentNeighbours = null;

  let view = notFoundView;
  let args = [];
  let nav = null;
  for (const [pattern, fn, section] of routes) {
    const match = path.match(pattern);
    if (match) {
      view = fn;
      nav = section;
      args = match.slice(1).map((g) => (g == null ? g : decodeURIComponent(g)));
      break;
    }
  }
  for (const link of document.querySelectorAll("[data-nav]")) {
    link.toggleAttribute("aria-current", false);
    if (link.dataset.nav === nav) link.setAttribute("aria-current", "page");
  }

  const slow = setTimeout(() => document.body.classList.add("is-loading"), 120);
  try {
    // links in what the models wrote need to know which days and notes exist
    await linksReady;
    const node = setupMode ? await setupView() : await view(args, params);
    if (mine !== renderToken) return;
    fill(page, node);
  } catch (err) {
    if (mine !== renderToken) return;
    fill(page, errorView(err));
  } finally {
    clearTimeout(slow);
    if (mine === renderToken) document.body.classList.remove("is-loading");
  }
  window.scrollTo(0, 0);
  page.querySelector("h1")?.focus({ preventScroll: true });
}

function updateRail(overview) {
  const counts = overview.counts;
  for (const el of document.querySelectorAll("[data-count]")) {
    const key = el.dataset.count;
    el.textContent = counts[key] != null ? fmtNum(counts[key]) : "";
    if (key === "files" && counts.errored_files) {
      el.prepend(h("span", { class: "alert-dot", role: "img", "aria-label": `${plural(counts.errored_files, "file")} couldn't be indexed`, title: `${plural(counts.errored_files, "file")} couldn't be indexed` }), " ");
    }
  }
}

// ---------------------------------------------------------------- jump box

const jump = {
  input: document.getElementById("jump"),
  list: document.getElementById("jump-list"),
  options: [],
  active: -1,
};

async function jumpSearch() {
  const raw = jump.input.value.trim();
  if (!raw) return closeJump();
  let cat;
  try {
    cat = await getCatalog();
  } catch {
    return closeJump();
  }
  if (jump.input.value.trim() !== raw) return;
  const q = raw.toLowerCase();
  const groups = [];

  const datePrefix = /^\d{4}(-\d{0,2}){0,2}$/.test(q) ? q : null;
  const entries = datePrefix
    ? cat.entries.filter((e) => e.date.startsWith(datePrefix)).slice(-6).reverse()
    : cat.entries.filter((e) => e.summary && e.summary.toLowerCase().includes(q)).slice(-4).reverse();
  const entryGroup = ["Entries", entries.map((e) => ({ primary: mediumDate(e.date), secondary: e.summary || "", link: href.entry(e.id) }))];
  // a typed date wants entries first; a typed word most likely names a note or tag
  if (datePrefix && entries.length) groups.push(entryGroup);

  const rank = (name) => (name.toLowerCase().startsWith(q) ? 0 : 1);
  const notes = cat.notes
    .map((n) => {
      const names = [n.title, ...n.aliases, ...n.learned];
      const hit = names.find((x) => x.toLowerCase().includes(q));
      return hit ? { n, score: Math.min(...names.filter((x) => x.toLowerCase().includes(q)).map(rank)), hit } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.score - b.score || a.n.title.localeCompare(b.n.title))
    .slice(0, 6);
  if (notes.length) {
    groups.push(["Notes", notes.map(({ n, hit }) => ({
      primary: n.title,
      secondary: hit !== n.title ? `${kindLabel(n.kind)}, also “${hit}”` : kindLabel(n.kind),
      link: href.note(n.id),
    }))]);
  }

  const tags = cat.tags.filter((t) => t.name.toLowerCase().includes(q))
    .sort((a, b) => rank(a.name) - rank(b.name) || b.n - a.n).slice(0, 5);
  if (tags.length) {
    groups.push(["Tags", tags.map((t) => ({ primary: t.name, secondary: plural(t.n, "entry", "entries"), link: href.tag(t.name) }))]);
  }
  if (!datePrefix && entries.length) groups.push(entryGroup);
  groups.push(["Search", [
    { primary: `Search for “${raw}”`, secondary: "keywords and meaning", link: href.search(raw) },
    { primary: `Ask “${raw}”`, secondary: "an answer from your entries and notes", link: href.ask(raw) },
  ]]);

  jump.options = [];
  fill(jump.list, ...groups.flatMap(([label, items]) => [
    h("li", { class: "jump-group", role: "presentation" }, label),
    ...items.map((item) => {
      const index = jump.options.length;
      const li = h("li", {
        class: "jump-option", role: "option", id: `jump-${index}`, "aria-selected": "false",
        onmousedown: (ev) => { ev.preventDefault(); goJump(index); },
        onmousemove: () => setActive(index),
      }, h("span", { class: "primary" }, item.primary), h("span", { class: "secondary" }, item.secondary));
      jump.options.push({ ...item, el: li });
      return li;
    }),
  ]));
  jump.list.hidden = false;
  jump.input.setAttribute("aria-expanded", "true");
  setActive(0);
}

function setActive(index) {
  jump.active = index;
  jump.options.forEach((o, i) => o.el.setAttribute("aria-selected", i === index ? "true" : "false"));
  const active = jump.options[index];
  if (active) {
    jump.input.setAttribute("aria-activedescendant", active.el.id);
    active.el.scrollIntoView({ block: "nearest" });
  }
}

function goJump(index) {
  const option = jump.options[index];
  if (!option) return;
  closeJump();
  jump.input.value = "";
  jump.input.blur();
  location.hash = option.link;
}

function closeJump() {
  jump.list.hidden = true;
  jump.options = [];
  jump.input.setAttribute("aria-expanded", "false");
  jump.input.removeAttribute("aria-activedescendant");
}

jump.input.addEventListener("input", debounce(jumpSearch, 60));
jump.input.addEventListener("focus", () => { if (jump.input.value.trim()) jumpSearch(); });
jump.input.addEventListener("blur", () => setTimeout(closeJump, 100));
jump.input.addEventListener("keydown", (ev) => {
  if (ev.key === "ArrowDown" && jump.options.length) {
    ev.preventDefault();
    setActive((jump.active + 1) % jump.options.length);
  } else if (ev.key === "ArrowUp" && jump.options.length) {
    ev.preventDefault();
    setActive((jump.active - 1 + jump.options.length) % jump.options.length);
  } else if (ev.key === "Enter") {
    ev.preventDefault();
    if (jump.options.length) goJump(jump.active);
    else if (jump.input.value.trim()) location.hash = href.search(jump.input.value.trim());
  } else if (ev.key === "Escape") {
    jump.input.value = "";
    closeJump();
    jump.input.blur();
  }
});

// ---------------------------------------------------------------- keyboard, theme

document.addEventListener("keydown", (ev) => {
  if (ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const target = ev.target;
  const typing = target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName));
  if (typing) return;
  if (document.querySelector("dialog[open]")) return;
  if (ev.key === "/") {
    ev.preventDefault();
    jump.input.focus();
  } else if (ev.key === "?") {
    showKeys();
  } else if (ev.key === "a") {
    ev.preventDefault();
    location.hash = "#/ask";
  } else if (ev.key === "h") {
    location.hash = "#/";
  } else if (currentNeighbours && (ev.key === "k" || ev.key === "ArrowLeft") && currentNeighbours.prev) {
    location.hash = currentNeighbours.prev;
  } else if (currentNeighbours && (ev.key === "j" || ev.key === "ArrowRight") && currentNeighbours.next) {
    location.hash = currentNeighbours.next;
  }
});

const KEYS = [
  [["/"], "Find a day, note or tag, or ask a question"],
  [["a"], "Go to Ask"],
  [["h"], "Go to Home"],
  [["j", "k"], "The next and the previous entry, on an entry's page"],
  [["?"], "This list"],
  [["Esc"], "Close a list or a box like this one"],
];

function showKeys() {
  const dialog = h("dialog", { class: "dialog", "aria-labelledby": "keys-title" },
    h("h2", { id: "keys-title" }, "Keyboard shortcuts"),
    h("dl", { class: "keys" }, KEYS.map(([keys, what]) => [
      h("dt", {}, keys.map((k) => h("kbd", {}, k))), h("dd", {}, what),
    ])),
    h("div", { class: "dialog-actions" },
      h("button", { class: "button", type: "button", autofocus: true, onclick: () => dialog.close() }, "Close")));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

document.getElementById("keys").addEventListener("click", showKeys);
document.getElementById("skip").addEventListener("click", () => page.focus());

// On a narrow screen the sections slide in from the menu button.
const menuButton = document.getElementById("menu");
const railShade = document.getElementById("rail-shade");

function setMenu(open) {
  document.body.classList.toggle("rail-open", open);
  menuButton.setAttribute("aria-expanded", String(open));
  railShade.hidden = !open;
}

menuButton.addEventListener("click", () => setMenu(!document.body.classList.contains("rail-open")));
railShade.addEventListener("click", () => setMenu(false));
document.getElementById("rail").addEventListener("click", (ev) => {
  if (ev.target.closest("a")) setMenu(false);
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && document.body.classList.contains("rail-open")) {
    setMenu(false);
    menuButton.focus();
  }
});

const THEMES = ["system", "light", "dark"];
const themeButtons = [...document.querySelectorAll("[data-theme-pick]")];

function readTheme() {
  try {
    return THEMES.includes(localStorage.getItem("theme")) ? localStorage.getItem("theme") : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  for (const button of themeButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.themePick === theme));
  }
}

for (const button of themeButtons) {
  button.addEventListener("click", () => {
    try {
      localStorage.setItem("theme", button.dataset.themePick);
    } catch {
      // private windows can refuse storage; the theme still changes for this visit
    }
    applyTheme(button.dataset.themePick);
  });
}

applyTheme(readTheme());
window.addEventListener("hashchange", () => setMenu(false));
window.addEventListener("hashchange", render);
sessionReady.then(() => {
  if (setupMode) return;
  api("overview").then(updateRail).catch(() => {});
  resumeAsking();
  resumeWatching().then(watchUpkeep);
});
render();

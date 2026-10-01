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

const KIND_LABELS = { week: "Week", month: "Month", year: "Year" };
const kindLabel = (kind) => kind.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());

// ---------------------------------------------------------------- links

const href = {
  overview: () => "#/",
  calendar: (year) => (year ? `#/calendar/${year}` : "#/calendar"),
  entries: (params) => `#/entries${query(params)}`,
  entry: (id) => `#/entry/${id}`,
  notes: (params) => `#/notes${query(params)}`,
  note: (id) => `#/note/${id}`,
  tags: () => "#/tags",
  tag: (name) => `#/tag/${encodeURIComponent(name)}`,
  digests: (type) => `#/digests/${type || "month"}`,
  digest: (type, key) => `#/digest/${type}/${encodeURIComponent(key)}`,
  files: (params) => `#/files${query(params)}`,
  search: (q) => `#/search${query({ q })}`,
  ask: (q) => `#/ask${query({ q })}`,
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

async function api(path, params) {
  let response;
  try {
    response = await fetch(`/api/${path}${query(params)}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("The journal browser isn't answering. Is `journal browse` still running?", 0);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data.error || `Request failed (${response.status}).`, response.status);
  return data;
}

let catalogCache = null;
let catalogAt = 0;

function getCatalog(fresh = false) {
  if (fresh || !catalogCache || Date.now() - catalogAt > 60_000) {
    catalogAt = Date.now();
    catalogCache = api("catalog").catch((err) => {
      catalogCache = null;
      throw err;
    });
  }
  return catalogCache;
}

// ---------------------------------------------------------------- shared pieces

function pageHead({ eyebrow, title, lede, actions, meta }) {
  return h("header", { class: "page-head" },
    eyebrow && h("p", { class: "eyebrow" }, eyebrow),
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

function entryLines(entries, scale, { emptyText } = {}) {
  if (!entries.length) return empty(emptyText || "No entries.");
  return h("ol", { class: "lines" }, entries.map((e) =>
    h("li", {},
      h("a", { class: "line", href: href.entry(e.id) },
        h("span", { class: "line-date" }, mediumDate(e.date)),
        swatch(e.mood, scale),
        h("span", { class: e.summary ? "line-summary" : "line-summary none" }, e.summary || "No summary stored"),
        e.tags && e.tags.length ? h("span", { class: "line-tags" }, e.tags.join(" · ")) : null))));
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

// ---------------------------------------------------------------- charts

function monthlyMoodChart(months, baseline, scale) {
  const scored = months.filter((m) => m.avg_mood != null);
  if (!scored.length) return empty("No entry has a mood score yet.");
  // every month from the first to the last, so gaps in writing show as gaps
  const first = toDate(`${months[0].month}-01`);
  const last = toDate(`${months[months.length - 1].month}-01`);
  const byKey = new Map(months.map((m) => [m.month, m]));
  const keys = [];
  for (let d = first; d <= last; d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1))) {
    keys.push(d.toISOString().slice(0, 7));
  }
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
      pageHead({ eyebrow: "What the app knows", title: "Nothing indexed yet" }),
      empty("The cache is empty. Run ", h("code", {}, "journal index"), " and reload this page."));
  }

  const lede = h("p", { class: "lede" },
    "The app has read ", h("strong", {}, plural(c.entries, "entry", "entries")),
    c.date_from ? [" written between ", h("strong", {}, fmtDayMonthYear.format(toDate(c.date_from))), " and ", h("strong", {}, fmtDayMonthYear.format(toDate(c.date_to)))] : null,
    ", ", h("strong", {}, plural(c.words, "word")), " in all, plus ",
    h("strong", {}, plural(c.notes, "reference note")),
    " about the people, places and things they link to. Each mark below is one entry, coloured by the mood the app gave it.");

  const facts = h("div", { class: "facts" },
    fact(fmtMood(o.baseline_mood), `average mood, from ${plural(o.scored_entries, "scored entry", "scored entries")}`, href.tags()),
    fact(fmtNum(w.longest_streak), `longest streak in days${w.longest_streak_end ? `, to ${mediumDate(w.longest_streak_end)}` : ""}`, href.calendar(w.longest_streak_end?.slice(0, 4))),
    fact(fmtNum(c.tags), "tags the model gave", href.tags()),
    fact(fmtNum(c.links), "links from entries to notes", href.notes()),
    fact(fmtNum(c.rollups), "digests written", href.digests()),
    fact(fmtNum(c.files), `files tracked${c.errored_files ? `, ${c.errored_files} with errors` : ""}`, href.files()));

  const problems = [
    ...o.errored_files.map((f) => h("div", { class: "notice" },
      h("span", { class: "notice-label" }, "Couldn't index"),
      h("span", {}, h("span", { class: "path" }, f.vault_path), " ", f.error))),
    ...o.future_entries.map((f) => h("div", { class: "notice info" },
      h("span", { class: "notice-label" }, "Dated in the future"),
      h("span", {}, futureLink(cat, f.date), " from ", h("span", { class: "path" }, f.vault_path),
        ". Usually a typo in the file name, and it throws off streaks."))),
  ];

  const s = o.setup;
  const setup = h("dl", { class: "setup" },
    h("dt", {}, "Journal folder"), h("dd", { class: "path" }, s.vault),
    h("dt", {}, "Note folders"), h("dd", {}, s.notes_paths.length ? s.notes_paths.join(", ") : "none set"),
    h("dt", {}, "Note kinds"), h("dd", {}, o.note_kinds.length
      ? h("span", { class: "chips" }, o.note_kinds.map((k) => h("a", { class: "chip", href: href.notes({ kind: k.kind }) }, kindLabel(k.kind), h("span", { class: "n" }, fmtNum(k.n)))))
      : "none"),
    h("dt", {}, "Cache file"), h("dd", {}, h("span", { class: "path" }, s.db_path), ` (${bytes(s.db_bytes)}, format ${s.schema_version})`),
    h("dt", {}, "Reads entries with"), h("dd", {}, s.fast_model),
    h("dt", {}, "Writes digests with"), h("dd", {}, s.precise_model),
    h("dt", {}, "Meaning vectors"), h("dd", {}, s.embed_model, s.embed_dim ? `, ${s.embed_dim} dimensions` : ""),
    h("dt", {}, "Reranking"), h("dd", {}, s.reranking || "off"));

  return h("div", {},
    h("header", { class: "page-head" },
      h("p", { class: "eyebrow" }, "What the app knows"),
      h("h1", { tabindex: "-1" }, "Everything it has read, at a glance"),
      lede,
      cat.entries.length ? yearStrips(cat.entries, scale) : null),
    problems.length ? section("Needs a look", problems) : null,
    h("section", { class: "section" }, facts),
    section("Mood by month", monthlyMoodChart(o.monthly_mood, o.baseline_mood, scale), {
      note: "click a bar to see that month's entries",
    }),
    h("div", { class: "two-col" },
      section(`On this day`, entryLines(o.on_this_day, scale, {
        emptyText: `Nothing written on ${fmtShort.format(toDate(o.today))} in other years.`,
      }), { note: fmtShort.format(toDate(o.today)) }),
      section("How it's set up", setup)));
}

function futureLink(cat, iso) {
  const found = cat.entries.find((e) => e.date === iso);
  return found ? h("a", { href: href.entry(found.id) }, longDate(iso)) : longDate(iso);
}

function fact(value, label, link) {
  return h(link ? "a" : "div", { class: "fact", href: link },
    h("div", { class: "fact-value" }, value), h("div", { class: "fact-label" }, label));
}

async function calendarView([yearParam]) {
  const cat = await getCatalog();
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

  const preview = h("aside", { class: "preview", "aria-live": "polite" });
  const showPreview = (iso) => {
    const list = byDate.get(iso) || [];
    fill(preview,
      h("div", { class: "preview-date" }, longDate(iso)),
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
    const mood = list.reduce((sum, e) => sum + (e.mood ?? 0), 0) / (list.filter((e) => e.mood != null).length || 1);
    const hasMood = list.some((e) => e.mood != null);
    const cls = hasMood ? `cell has ${moodClass(mood, scale)}` : "cell has nomood";
    const cell = h("a", {
      class: iso === todayIso ? `${cls} today` : cls,
      href: href.entry(list[0].id),
      "aria-label": `${longDate(iso)}, mood ${hasMood ? fmtMood(mood) : "none"}`,
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

  return h("div", {},
    pageHead({
      eyebrow: "Calendar", title: year,
      lede: "Every day you wrote, coloured by the mood the app gave that entry. Dashed squares are entries without a mood score.",
    }),
    h("nav", { class: "year-tabs", "aria-label": "Years" }, years.map((y) =>
      h("a", { class: y === year ? "chip active" : "chip", href: href.calendar(y), "aria-current": y === year ? "page" : null },
        y, h("span", { class: "n" }, fmtNum(cat.entries.filter((e) => e.date.startsWith(y)).length))))),
    h("div", { class: "cal-scroll" }, cal),
    moodLegend(scale),
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
    from: params.get("from") || "", to: params.get("to") || "",
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
  const clear = h("button", { class: "button", type: "button" }, "Clear filters");

  let token = 0;
  async function load() {
    const mine = ++token;
    replaceParams("/entries", { ...state, order: state.order === "newest" ? "" : state.order, page: state.page > 1 ? state.page : "" });
    const data = await api("entries", {
      q: state.q, tag: state.tag, note: state.note, from: state.from, to: state.to,
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
    const pages = Math.max(1, Math.ceil(data.total / pageSize));
    const filtered = state.q || state.tag || state.note || state.from || state.to;
    const go = (p) => () => { state.page = p; load(); window.scrollTo(0, 0); };
    fill(results,
      h("p", { class: "result-count" }, filtered
        ? `${plural(data.total, "entry", "entries")} match`
        : `${plural(data.total, "entry", "entries")} in the cache`,
        pages > 1 ? `, page ${state.page} of ${pages}` : ""),
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
    Object.assign(state, { q: "", tag: "", note: "", from: "", to: "", page: 1 });
    text.value = ""; tagSelect.value = ""; from.value = ""; to.value = "";
    load();
  });

  load();
  return h("div", {},
    h("header", { class: "page-head" }, h("p", { class: "eyebrow" }, "Every dated entry in the cache"), heading),
    h("div", { class: "filters", role: "search" },
      field("Contains", text, true), field("Tag", tagSelect), field("From", from), field("To", to),
      field("Order", order), noteSlot, clear),
    results);
}

async function entryView([id]) {
  const e = await api(`entries/${id}`);
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
    gloss("Tags",
      e.tags.length ? h("div", { class: "chips" }, e.tags.map((t) => h("a", { class: "chip pencil", href: href.tag(t), title: fmTags.has(t) ? "From the frontmatter" : null }, t))) : h("span", { class: "quiet" }, "No tags"),
      e.tags.some((t) => fmTags.has(t)) && h("div", { class: "quiet" }, "Includes the tags in the file's frontmatter.")),
    gloss("Mentions",
      e.notes.length ? h("div", { class: "chips" }, e.notes.map((n) => h("a", { class: "chip pencil", href: href.note(n.id), title: kindLabel(n.kind) }, n.title))) : h("span", { class: "quiet" }, "Links to no indexed note"),
      e.unresolved_links.length ? h("div", { class: "quiet" },
        h("div", { class: "gloss-label" }, "Links with no note (not counted)"),
        h("div", { class: "chips" }, e.unresolved_links.map((t) => h("span", { class: "chip ghost" }, t)))) : null),
    (e.links_to_days.length || e.linked_from_days.length) && gloss("Other days",
      e.links_to_days.length ? [h("div", { class: "quiet" }, "This entry links to"), dayChips(e.links_to_days)] : null,
      e.linked_from_days.length ? [h("div", { class: "quiet" }, "Linked from"), dayChips(e.linked_from_days)] : null),
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

  return h("div", {},
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
    h("div", { class: "folio" },
      h("article", {},
        h("p", { class: "reading-label" }, "Your entry, as the app stored it. Links and embeds appear as plain text, the way the models read them."),
        h("div", { class: e.raw_text ? "raw" : "raw empty" }, e.raw_text || "The entry has no text.")),
      margin),
    section("Close in meaning", entryLines(e.similar, scale, {
      emptyText: e.embedded ? "No other entries have vectors yet." : "This entry has no vector yet, so there's nothing to compare.",
    }), { note: "nearest by stored vector" }),
    section("Digests covering this day", digestRows));
}

let currentNeighbours = null;

function dayChips(days) {
  return h("div", { class: "chips" }, days.map((d) => h("a", { class: "chip pencil", href: href.entry(d.id) }, mediumDate(d.date))));
}

async function notesView(_, params) {
  const data = await api("notes");
  document.title = "Notes · Commonplace";
  const state = { kind: params.get("kind") || "", q: params.get("q") || "" };
  const body = h("div", {});
  const kinds = h("div", { class: "chips" });
  const filter = h("input", { type: "search", value: state.q, placeholder: "Title or alias" });

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
      (!q || n.title.toLowerCase().includes(q) || n.aliases.some((a) => a.toLowerCase().includes(q))));
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
    ? section("Links with no note", h("div", { class: "chips" }, data.unresolved_links.map((l) =>
      h("span", { class: "chip ghost", title: `Last linked ${mediumDate(l.last)}` }, l.target, h("span", { class: "n" }, fmtNum(l.n))))), {
      intro: "Your entries link to these, but there's no note file for them in your note folders, so they don't count as mentions yet. Create the note and they will.",
      note: "most linked first",
    })
    : null;

  return h("div", {},
    pageHead({
      eyebrow: "Reference notes",
      title: "Notes",
      lede: "The undated notes about people, places and topics. A note's kind is the folder it sits in. Mentions come only from links in your entries.",
    }),
    h("div", { class: "filters", role: "search" }, field("Filter", filter, true), h("div", { class: "field" }, h("span", {}, "Kind"), kinds)),
    body,
    unresolved);
}

async function noteView([id]) {
  const n = await api(`notes/${id}`);
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
    gloss("Also called", n.aliases.length ? n.aliases.join(", ") : h("span", { class: "quiet" }, "No aliases")),
    gloss("Indexed", h("div", { class: "quiet" }, stamp(n.extracted_at)),
      h("div", { class: "quiet" }, n.embedded ? "Has a meaning vector" : "No meaning vector yet")));

  const props = Object.entries(n.properties);
  const qualifies = n.digest_min_mentions > 0 && n.mentions && n.mentions.count >= n.digest_min_mentions;
  const overYears = n.digest
    ? section("Over the years", [
      h("p", { class: "digest-summary" }, n.digest.summary || "The digest is empty."),
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
    h("div", { class: "folio" },
      h("article", {},
        props.length ? h("dl", { class: "props" }, props.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])) : null,
        h("p", { class: "reading-label" }, "The note, as the app stored it"),
        h("div", { class: n.raw_text ? "raw" : "raw empty" }, n.raw_text || "This note has no text of its own.")),
      margin),
    overYears,
    (n.outlinks.length || n.backlinks.length) ? section("Connected notes", h("div", { class: "two-col" },
      h("div", {}, h("h3", {}, "This note links to"), h("div", { class: "chips" }, n.outlinks.length ? n.outlinks.map((o) => noteChip(o)) : empty("Nothing."))),
      h("div", {}, h("h3", {}, "Notes that link here"), h("div", { class: "chips" }, n.backlinks.length ? n.backlinks.map((o) => noteChip(o)) : empty("Nothing."))))) : null,
    section("Entries that link here", entryLines(n.entries, scale, { emptyText: "No indexed entry links to this note." }), {
      note: n.entries_capped ? `newest ${n.entries.length} shown` : n.entries.length ? h("a", { href: href.entries({ note: n.id }) }, "filter these") : null,
    }));
  for (const bar of node.querySelectorAll(".hbar[data-w]")) bar.style.width = `${Number(bar.dataset.w) * 100}%`;
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
      : empty("No tags yet. Run ", h("code", {}, "journal index"), " first."));
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
    section("Entries", entryLines(t.entries, scale), {
      note: t.entries_capped ? h("a", { href: href.entries({ tag: t.name }) }, `newest ${fmtNum(t.entries.length)} shown, filter to see all`) : null,
    }));
}

async function digestsView([type]) {
  const data = await api("digests");
  const scale = data.mood_scale;
  const current = ["year", "month", "week", "notes"].includes(type) ? type : "month";
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
      h("div", { class: "digest-summary clamp" }, d.summary || "No summary text."))))
    : empty(data.note_min_mentions
      ? ["No relationship digests yet. ", h("code", {}, "journal rollup"), ` writes one for every note linked from ${data.note_min_mentions} or more entries.`]
      : ["Relationship digests are turned off (", h("code", {}, "note_min_mentions = 0"), " in config.toml)."]);
  return h("div", {},
    pageHead({
      eyebrow: "Written by the precise model",
      title: "Digests",
      lede: "Summaries of each week, month and year, and of the people, places and topics you link most. Answers read these instead of every entry. A digest goes out of date when an entry it covers changes, until the next journal rollup.",
    }),
    h("nav", { class: "tabs", "aria-label": "Digest periods" }, ["year", "month", "week", "notes"].map((t) =>
      h("a", { class: "tab", href: href.digests(t), "aria-current": t === current ? "page" : null },
        t === "notes" ? "Notes " : `${KIND_LABELS[t]}s `, h("span", { class: "count" }, fmtNum(tabCount(t)))))),
    current === "notes" ? noteCards : list.length
      ? h("div", { class: "digest-list" }, list.map((d) => h("a", { class: "digest-card", href: href.digest(d.type, d.key) },
        h("div", {},
          h("div", { class: "digest-when" }, periodTitle(d.type, d.key)),
          h("div", { class: "digest-sub" }, periodRange(d.type, d.start, d.end)),
          h("div", { class: "digest-sub" }, moodChip(d.mood, scale), ` · ${plural(d.entries ?? 0, "entry", "entries")}`),
          d.stale && h("div", { class: "digest-sub" }, h("span", { class: "status pending" }, "out of date"))),
        h("div", { class: "digest-summary clamp" }, d.summary || "No summary text."),
        d.tags.length ? h("div", { class: "digest-tags" }, d.tags.join(" · ")) : null)))
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
      gloss("Written", stamp(r.generated_at), r.stale && h("div", {}, "Out of date: an entry in this period changed since. Run journal rollup.")),
    ] : gloss("Status", "Not written yet. Run journal rollup."));

  return h("div", {},
    pageHead({
      eyebrow: h("a", { href: href.digests(type) }, `${KIND_LABELS[type]} digest`),
      title: periodTitle(type, key),
      actions: [
        button("← Earlier", prev && href.digest(type, prev), { disabled: !prev, key: "k" }),
        button("Later →", next && href.digest(type, next), { disabled: !next, key: "j" }),
      ],
      meta: [h("span", {}, periodRange(type, d.start, d.end))],
    }),
    h("div", { class: "folio" },
      h("article", {},
        h("p", { class: "reading-label" }, r ? "What the model wrote about this period" : "No digest for this period yet"),
        h("div", { class: r && r.summary ? "raw" : "raw empty" }, (r && r.summary) || "Nothing written.")),
      margin),
    section("Entries in this period", entryLines(d.entries, scale, { emptyText: "No entries in this period." })));
}

async function filesView(_, params) {
  const data = await api("files");
  document.title = "Files · Commonplace";
  const groups = {
    all: { label: "All", test: () => true },
    problems: { label: "Problems", test: (f) => f.status !== "indexed" },
    entries: { label: "Entries", test: (f) => f.entries > 0 },
    notes: { label: "Notes", test: (f) => f.note_id != null },
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
            : h("span", { class: "delta" }, "nothing"),
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

// Kept outside the view so a question keeps running, and the conversation
// stays, while you look at the entries it cites.
const ask = { turns: loadTurns(), controller: null, redraw: null };

function loadTurns() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("ask-turns") || "[]");
    // a question that was still running when the page reloaded has no answer coming
    return saved.map((t) => (t.status === "running" ? { ...t, status: "cancelled" } : t));
  } catch {
    return [];
  }
}

function saveTurns() {
  try {
    sessionStorage.setItem("ask-turns", JSON.stringify(ask.turns));
  } catch {
    // storage can be full or refused; the conversation still works for this visit
  }
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

async function askQuestion(question) {
  const history = ask.turns
    .filter((t) => t.status === "done")
    .slice(-HISTORY_TURNS)
    .map((t) => [t.rewritten || t.question, t.answer.text.slice(0, HISTORY_ANSWER_CHARS)]);
  const turn = { question, status: "running", stage: "Starting…" };
  ask.turns.push(turn);
  const controller = new AbortController();
  ask.controller = controller;
  changed();
  try {
    const response = await fetch(`/api/ask${query({ q: question, history: history.length ? JSON.stringify(history) : "" })}`, {
      headers: { Accept: "text/event-stream" }, signal: controller.signal,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `The question couldn't be asked (${response.status}).`);
    }
    await readEvents(response, (event, data) => {
      if (event === "stage") turn.stage = data.text;
      else if (event === "rewritten") turn.rewritten = data.question;
      else if (event === "answer") Object.assign(turn, { status: "done", answer: data });
      else if (event === "problem") Object.assign(turn, { status: "failed", error: data.error });
      changed();
    });
    if (turn.status === "running") {
      Object.assign(turn, { status: "failed", error: "The answer stopped partway. Is `journal browse` still running?" });
    }
  } catch (err) {
    if (controller.signal.aborted) turn.status = "cancelled";
    else Object.assign(turn, { status: "failed", error: err.message === "Failed to fetch" ? "The journal browser isn't answering. Is `journal browse` still running?" : err.message });
  } finally {
    if (ask.controller === controller) ask.controller = null;
    changed();
  }
}

function sourceChips(answer) {
  const { dates, notes } = answer.sources;
  if (!dates.length && !notes.length) return null;
  return h("div", { class: "turn-sources" },
    h("span", { class: "gloss-label" }, "Sources"),
    h("div", { class: "chips" },
      dates.map((d) => d.id != null
        ? h("a", { class: "chip pencil", href: href.entry(d.id) }, mediumDate(d.date))
        : h("span", { class: "chip ghost" }, mediumDate(d.date))),
      notes.map((n) => n.id != null
        ? h("a", { class: "chip pencil", href: href.note(n.id) }, n.title)
        : h("span", { class: "chip ghost" }, n.title))));
}

function turnView(turn, scale, index) {
  const parts = [h("p", { class: "turn-q" }, turn.question)];
  if (turn.rewritten) parts.push(h("p", { class: "turn-rewrite" }, "Read as: ", h("q", {}, turn.rewritten)));
  if (turn.status === "running") {
    parts.push(h("div", { class: "turn-running" },
      h("p", { class: "search-status", role: "status" }, turn.stage),
      h("button", { class: "button", type: "button", onclick: () => ask.controller?.abort() }, "Cancel")));
  } else if (turn.status === "failed") {
    parts.push(h("div", { class: "notice" }, h("span", { class: "notice-label" }, "Couldn't answer"), h("span", {}, turn.error)));
  } else if (turn.status === "cancelled") {
    parts.push(h("p", { class: "turn-meta" }, "Cancelled before it finished."));
  } else {
    const a = turn.answer;
    const readCount = a.entries.length + a.notes.length;
    parts.push(
      h("div", { class: "turn-answer" }, a.body),
      sourceChips(a),
      h("p", { class: "turn-meta" }, `Answered ${STAGE_LABELS[a.stage] || a.stage}.`),
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
  const input = h("input", {
    type: "text", placeholder: "When did I last see John? How was March?", "aria-label": "Your question",
    autocomplete: "off", maxlength: 2000,
  });
  const go = h("button", { class: "button", type: "submit" }, "Ask");
  const reset = h("button", { class: "button", type: "button" }, "New conversation");
  const hint = h("p", { class: "section-note" });
  const form = h("form", { class: "search-box ask-box", role: "search" }, input, go);

  const draw = () => {
    const running = ask.turns.some((t) => t.status === "running");
    go.disabled = running;
    reset.hidden = !ask.turns.length || running;
    hint.textContent = ask.turns.some((t) => t.status === "done")
      ? "Follow-ups like “what about the month after?” build on the answers above."
      : "";
    fill(thread, ask.turns.length
      ? ask.turns.map((t, i) => turnView(t, scale, i))
      : empty("Ask anything about your journal. Answers are built from your entries and notes the same way ", h("code", {}, "journal ask"), " does, and each one lists what it read. Questions need Ollama running; everything else on these pages works without it."));
  };
  ask.redraw = () => {
    if (thread.isConnected) draw();
  };

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    if (!q || ask.controller) return;
    input.value = "";
    askQuestion(q);
  });
  reset.addEventListener("click", () => {
    ask.turns = [];
    changed();
    input.focus();
  });

  const preset = (params.get("q") || "").trim();
  if (preset) {
    replaceParams("/ask", {});
    if (!ask.controller) askQuestion(preset);
  }
  draw();
  setTimeout(() => input.focus(), 0);
  return h("div", {},
    pageHead({
      eyebrow: "Questions about your journal", title: "Ask",
      lede: "Ask in plain words. The answer cites the days and notes it drew on, and you can open each one.",
    }),
    thread, form, h("div", { class: "ask-foot" }, hint, reset));
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
  [/^\/note\/(\d+)$/, noteView, "notes"],
  [/^\/tags$/, tagsView, "tags"],
  [/^\/tag\/(.+)$/, tagView, "tags"],
  [/^\/digests(?:\/(year|month|week|notes))?$/, digestsView, "digests"],
  [/^\/digest\/(year|month|week)\/([^/]+)$/, digestView, "digests"],
  [/^\/files$/, filesView, "files"],
  [/^\/search$/, searchView, "search"],
  [/^\/ask$/, askView, "ask"],
];

let renderToken = 0;

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
    const node = await view(args, params);
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
      el.prepend(h("span", { class: "alert-dot", title: `${plural(counts.errored_files, "file")} failed to index` }, "●"), " ");
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
      const names = [n.title, ...n.aliases];
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
  if (ev.key === "/") {
    ev.preventDefault();
    jump.input.focus();
  } else if (currentNeighbours && (ev.key === "k" || ev.key === "ArrowLeft") && currentNeighbours.prev) {
    location.hash = currentNeighbours.prev;
  } else if (currentNeighbours && (ev.key === "j" || ev.key === "ArrowRight") && currentNeighbours.next) {
    location.hash = currentNeighbours.next;
  }
});

document.getElementById("skip").addEventListener("click", () => page.focus());

const themeButton = document.getElementById("theme");
const THEMES = ["system", "light", "dark"];

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
  themeButton.textContent = `Theme: ${theme}`;
  themeButton.setAttribute("aria-label", `Colour theme: ${theme}. Click to change.`);
}

let theme = readTheme();

themeButton.addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try {
    localStorage.setItem("theme", theme);
  } catch {
    // private windows can refuse storage; the theme still changes for this visit
  }
  applyTheme(theme);
});

applyTheme(theme);
window.addEventListener("hashchange", render);
api("overview").then(updateRail).catch(() => {});
render();

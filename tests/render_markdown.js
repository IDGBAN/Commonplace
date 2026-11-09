// Runs the browser page's Markdown functions outside a browser, for test_markdown.py.
// Reads {"catalog": ..., "texts": [...]} on stdin and prints what each text
// turns into, as HTML-like strings. Text is escaped and elements aren't, so a
// test can tell a real element from markup that stayed text.

const fs = require("fs");

const script = fs.readFileSync(process.argv[2], "utf8");
const start = script.indexOf("const linkIndex =");
const end = script.indexOf("// ---------------------------------------------------------------- charts");
if (start < 0 || end < start) throw new Error("couldn't find the Markdown section of app.js");

const escapeText = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

class El {
  constructor(tag, props, kids) {
    this.tag = tag;
    this.props = props || {};
    this.kids = [];
    this.append(...kids);
  }

  append(...kids) {
    for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) this.kids.push(kid);
  }

  toString() {
    const attrs = Object.entries(this.props)
      .filter(([, v]) => v != null && v !== false)
      .map(([k, v]) => ` ${k}="${escapeText(v)}"`).join("");
    const inner = this.kids.map((k) => (k instanceof El ? String(k) : escapeText(k))).join("");
    return `<${this.tag}${attrs}>${inner}</${this.tag}>`;
  }
}

const h = (tag, props, ...kids) => new El(tag, props, kids);
const at = (p) => (p ? `?at=${p.at}` : "");
const href = { entry: (id, p) => `#/entry/${id}${at(p)}`, note: (id, p) => `#/note/${id}${at(p)}` };
const mediumDate = (iso) => `day ${iso}`;

const fns = new Function(
  "h", "href", "mediumDate",
  `${script.slice(start, end)}; return { md, mdPlain, indexLinks };`,
)(h, href, mediumDate);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
fns.indexLinks(input.catalog);
const out = input.texts.map((text) => {
  const began = process.hrtime.bigint();
  const html = fns.md(text).map(String).join("\n");
  return { html, plain: fns.mdPlain(text), ms: Number(process.hrtime.bigint() - began) / 1e6 };
});
process.stdout.write(JSON.stringify(out));

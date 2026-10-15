"""C6 · Report view: a single self-contained HTML file (and CSV / Markdown files) with the C5 tables and plugin
sections.

Plugin sections that set `order` lay the report out as chapters (`group`) in that order, with a table of contents;
the generic C5 tables then move to a hardware-detail appendix. Cells that are `RtlName`s show a friendly name and keep
the RTL identifier: a tooltip in the body, inline in hardware-detail tables, an extra "<column> (RTL)" CSV column.
"""

from __future__ import annotations

import csv
import html
import math
import re
from pathlib import Path

from .analyze import Table
from .plugin import HW_GROUP, RtlName, Section

CSS = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --head:#f5f5f7; --accent:#0a66c2; --warn:#d97706; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#f2f2f7; --muted:#a1a1a6; --line:#2c2c2e;
  --head:#1f1f22; --accent:#5ac8fa; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0 auto; padding:24px 16px;
  max-width:1280px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:19px; margin:36px 0 4px; border-bottom:1px solid var(--line);
  padding-bottom:4px; } h3 { font-size:16px; margin:24px 0 6px; }
.muted { color:var(--muted); } .note { color:var(--muted); margin:2px 0 8px; max-width:960px; }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { padding:5px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; vertical-align:top; }
td.wrapcell { white-space:normal; min-width:220px; max-width:520px; }
th { background:var(--head); position:sticky; top:0; font-weight:600; }
th[title], td[title] { text-decoration:underline dotted var(--muted); text-underline-offset:3px; cursor:help; }
td.n { text-align:right; } tr:last-child td { border-bottom:none; }
.rtl { color:var(--muted); font:11px ui-monospace, monospace; }
.warn { border-left:3px solid var(--warn); padding:6px 10px; margin:8px 0; }
.bar { display:inline-block; height:8px; background:var(--accent); border-radius:2px; vertical-align:middle; }
.card { border-collapse:collapse; width:auto; margin:8px 0 4px; } .card td { border:none; padding:2px 14px 2px 0; }
.card td:first-child { color:var(--muted); }
dl.legend { display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; margin:8px 0 4px; font-size:13px;
  max-width:1100px; } dl.legend dt { font-weight:600; } dl.legend dd { margin:0; color:var(--muted); }
nav.toc { columns:2 320px; font-size:13px; margin:12px 0; } nav.toc ul { margin:0 0 8px; padding-left:18px; }
nav.toc > ul { padding-left:0; list-style:none; } nav.toc li.g { font-weight:600; break-inside:avoid; }
nav.toc li.g li { font-weight:400; }
"""


def _anchor(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def fmt_num(v) -> str:
    """Human-readable number: thousands separators, no float noise."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return str(v)
        if v == int(v) and abs(v) < 1e15:
            return fmt_num(int(v))
        if abs(v) >= 1000:
            return f"{v:,.1f}"
        return f"{v:.4g}" if abs(v) >= 1e-3 else f"{v:.2g}"
    return str(v)


def _cell(v, inline_rtl: bool = False, wrap: bool = False) -> str:
    if v is None:
        return '<td class="n muted">–</td>'
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f'<td class="n">{fmt_num(v)}</td>'
    cls = ' class="wrapcell"' if wrap else ""
    if isinstance(v, RtlName) and v.rtl and v.rtl != str(v):
        if inline_rtl:
            return f'<td{cls}>{html.escape(str(v))}<div class="rtl">{html.escape(v.rtl)}</div></td>'
        return f'<td{cls} title="RTL: {html.escape(v.rtl)}">{html.escape(str(v))}</td>'
    return f"<td{cls}>{html.escape(str(v))}</td>"


def _legend(legend: dict, columns: list[str]) -> str:
    if not legend:
        return ""
    keys = [c for c in columns if c in legend] + [k for k in legend if k not in columns]
    return '<dl class="legend">' + "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(legend[k])}</dd>"
                                           for k in keys) + "</dl>"


WRAP_COLS = {"finding", "evidence", "what to try", "meaning", "how to read it", "warning", "what it gives",
             "if it is off"}


def _table(title: str, columns: list[str], rows: list[list], note: str = "", bar_col: str | None = None,
           legend: dict | None = None, inline_rtl: bool = False, tag: str = "h2", anchor: str = "",
           empty: str = "") -> str:
    a = f' id="{anchor}"' if anchor else ""
    out = [f"<{tag}{a}>{html.escape(title)}</{tag}>"]
    if note:
        out.append(f'<p class="note">{html.escape(note)}</p>')
    if not rows:
        out.append(f'<p class="muted">{html.escape(empty or "Nothing to show.")}</p>')
        out.append(_legend(legend or {}, columns))
        return "\n".join(out)
    legend = legend or {}
    bi = columns.index(bar_col) if bar_col in columns else None
    mx = max((r[bi] for r in rows if bi is not None and isinstance(r[bi], (int, float))), default=0) or 1
    head = "".join(f'<th title="{html.escape(legend[c])}">{html.escape(c)}</th>' if c in legend
                   else f"<th>{html.escape(c)}</th>" for c in columns)
    out.append('<div class="wrap"><table><thead><tr>' + head + ("<th></th>" if bi is not None else "") +
               "</tr></thead><tbody>")
    wrap = [c in WRAP_COLS for c in columns]
    for r in rows:
        bar = ""
        if bi is not None:
            w = int(120 * max(0, r[bi] or 0) / mx) if isinstance(r[bi], (int, float)) else 0
            bar = f'<td><span class="bar" style="width:{w}px"></span></td>'
        out.append("<tr>" + "".join(_cell(v, inline_rtl, wrap[i] if i < len(wrap) else False)
                                    for i, v in enumerate(r)) + bar + "</tr>")
    out.append("</tbody></table></div>")
    out.append(_legend(legend, columns))
    return "\n".join(out)


BARS = {"Modules: input-side vs output-side stall": "bottleneck score", "Interfaces (ready/valid)": "stall %",
        "Time in each value (change probes)": "%"}


def layout(tables: list[Table], sections: list[Section]) -> list[Section]:
    """All report items in display order. When any plugin section is ordered, the generic tables go to the
    hardware-detail appendix; otherwise the legacy order (generic tables, then plugin sections) is kept."""
    generic = [Section(t.title, t.columns, t.rows, t.note, key=t.key) for t in tables]
    if not any(s.order is not None for s in sections):
        return generic + list(sections)
    for k, g in enumerate(generic):
        g.group, g.order, g.level = HW_GROUP, 910 + k / 100, "hw"
    items = list(sections) + generic
    return sorted(items, key=lambda s: 899 if s.order is None else s.order)


def write_html(path: str, tables: list[Table], sections: list[Section], summary: dict, warnings: list[str],
               title: str = "rtltrace report", intro: str = "", details: dict | None = None):
    """`summary` is the run card; `details` (input files, time base, ...) goes under it, collapsed."""
    items = layout(tables, sections)
    chapters = any(s.group for s in items)
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
             f"initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style></head><body>",
             f"<h1>{html.escape(title)}</h1>"]
    card = lambda d: "".join(f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"  # noqa: E731
                             for k, v in d.items())
    if chapters:
        parts.append(f'<table class="card">{card(summary)}</table>')
        if details:
            parts.append(f'<details><summary class="muted">Trace details</summary><table class="card">'
                         f"{card(details)}</table></details>")
    else:
        parts.append('<p class="muted">' + " · ".join(f"{html.escape(k)}: {html.escape(str(v))}"
                                                      for k, v in {**summary, **(details or {})}.items()) + "</p>")
    if intro:
        parts.append(f'<p class="note">{html.escape(intro)}</p>')
    for w in warnings:
        parts.append(f'<p class="warn">{html.escape(w)}</p>')
    used: set[str] = set()

    def uniq(name: str) -> str:
        a, n = _anchor(name), 2
        while a in used:
            a, n = f"{_anchor(name)}-{n}", n + 1
        used.add(a)
        return a

    g_id, s_id = {}, []
    for s in items:
        if s.group not in g_id:
            g_id[s.group] = uniq(s.group or "other")
        s_id.append(uniq(s.title))
    if chapters:
        toc, cur = ['<nav class="toc"><ul>'], None
        for s, sid in zip(items, s_id):
            if s.group != cur:
                if cur is not None:
                    toc.append("</ul></li>")
                cur = s.group
                toc.append(f'<li class="g"><a href="#{g_id[cur]}">{html.escape(cur or "Other")}</a><ul>')
            toc.append(f'<li><a href="#{sid}">{html.escape(s.title)}</a></li>')
        toc.append("</ul></li></ul></nav>")
        parts.append("".join(toc))
    cur = None
    for s, sid in zip(items, s_id):
        if chapters and s.group != cur:
            cur = s.group
            parts.append(f'<h2 id="{g_id[cur]}">{html.escape(cur or "Other")}</h2>')
        tag = "h3" if chapters else "h2"
        if s.columns:
            parts.append(_table(s.title, s.columns, s.rows, s.text, BARS.get(s.title), s.legend, s.level == "hw",
                                tag, sid if chapters else "", s.empty))
        else:
            a = f' id="{sid}"' if chapters else ""
            parts.append(f"<{tag}{a}>{html.escape(s.title)}</{tag}><pre>{html.escape(s.text)}</pre>" +
                         _legend(s.legend, []))
    parts.append("</body></html>")
    Path(path).write_text("\n".join(parts))


def _csv_name(t) -> str:
    return "".join(c if c.isalnum() else "_" for c in (t.key or t.title).lower()).strip("_")[:60]


def write_csv(dirpath: str, tables: list[Table]):
    """One CSV per table. A column holding RtlNames gets a '<column> (RTL)' column with the RTL identifiers next to
    it."""
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    seen: dict[str, int] = {}
    for t in tables:
        name = _csv_name(t)
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:  # two tables whose titles reduce to the same name: keep both files
            name = f"{name}_{seen[name]}"
        rtl_cols = {i for i in range(len(t.columns))
                    if any(isinstance(r[i], RtlName) and r[i].rtl and r[i].rtl != str(r[i])
                           for r in t.rows if i < len(r))}
        cols = []
        for i, c in enumerate(t.columns):
            cols.append(c)
            if i in rtl_cols:
                cols.append(f"{c} (RTL)")
        with open(d / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in t.rows:
                row = []
                for i, v in enumerate(r):
                    row.append(v)
                    if i in rtl_cols:
                        row.append(v.rtl if isinstance(v, RtlName) and v.rtl else v)
                w.writerow(row)


def _md_cell(v) -> str:
    if v is None:
        return "–"
    s = fmt_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    # only what Markdown would swallow: a '<' that opens a tag (<foo>, </x>, <!--) and an '&' that reads as an
    # entity; '< 1 %' stays as it is so the raw file reads well
    s = re.sub(r"&(?=#?\w+;)", "&amp;", s)
    s = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", s)
    return s.replace("|", "\\|").replace("\r", "").replace("\n", " ")


def write_md(path: str, tables: list[Table], sections: list[Section], summary: dict, warnings: list[str],
             title: str = "rtltrace report", intro: str = "", html_name: str = ""):
    """Markdown version of the report body (sections with level "app"); hardware detail and the trace details stay
    in the HTML/CSV."""
    items = [s for s in layout(tables, sections) if s.level != "hw"]
    out = [f"# {title}", ""]
    out += [f"- **{_md_cell(k)}**: {_md_cell(v)}" for k, v in summary.items()] + [""]
    if intro:
        out += [intro, ""]
    out += [f"> ⚠ {w}" for w in warnings] + ([""] if warnings else [])
    cur = None
    for s in items:
        if s.group and s.group != cur:
            cur = s.group
            out += [f"## {cur}", ""]
        out += [f"### {s.title}" if s.group else f"## {s.title}", ""]
        if s.text and s.columns:
            out += [f"_{s.text}_", ""]
        if s.columns:
            if s.rows:
                out.append("| " + " | ".join(_md_cell(c) for c in s.columns) + " |")
                out.append("|" + "---|" * len(s.columns))
                out += ["| " + " | ".join(_md_cell(v) for v in r) + " |" for r in s.rows]
            else:
                out.append(s.empty or "Nothing to show.")
            out.append("")
        elif s.text:
            fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", s.text)), default=0))
            out += [fence, s.text, fence, ""]
        if s.legend:
            out += ["<details><summary>Legend</summary>", ""] + \
                   [f"- **{_md_cell(k)}**: {v}" for k, v in s.legend.items()] + ["", "</details>", ""]
    if any(s.level == "hw" for s in layout(tables, sections)):
        out += ["---", f"Hardware detail (generic analyses, RTL name map) is in the HTML report"
                f"{f' ({html_name})' if html_name else ''} and the CSV files.", ""]
    Path(path).write_text("\n".join(out))


def write_text(out, tables: list[Table], sections: list[Section]):
    for t in list(tables) + [Table(s.title, s.columns, s.rows, s.text) for s in sections]:
        print(f"\n== {t.title}", file=out)
        if not t.columns and t.note:  # a text-only plugin section
            print("\n".join("   " + ln for ln in t.note.splitlines()), file=out)
            continue
        if not t.rows:
            print("   (nothing)", file=out)
            continue
        cols = [str(c) for c in t.columns]
        n = max(len(cols), *(len(r) for r in t.rows))
        cols += [""] * (n - len(cols))
        rows = [["–" if v is None else str(v) for v in r] + [""] * (n - len(r)) for r in t.rows]
        w = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
        print("   " + "  ".join(c.ljust(w[i]) for i, c in enumerate(cols)), file=out)
        for r in rows:
            print("   " + "  ".join(v.ljust(w[i]) for i, v in enumerate(r)), file=out)

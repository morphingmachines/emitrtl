"""C6 · Report view: a single self-contained HTML file (and CSV files) with the C5 tables and plugin sections."""

from __future__ import annotations

import csv
import html
from pathlib import Path

from .analyze import Table
from .plugin import Section

CSS = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --head:#f5f5f7; --accent:#0a66c2; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#f2f2f7; --muted:#a1a1a6; --line:#2c2c2e;
  --head:#1f1f22; --accent:#5ac8fa; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0 auto; padding:24px 16px;
  max-width:1280px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 6px; }
.muted { color:var(--muted); } .note { color:var(--muted); margin:2px 0 8px; max-width:900px; }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { padding:5px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
th { background:var(--head); position:sticky; top:0; font-weight:600; }
td.n { text-align:right; } tr:last-child td { border-bottom:none; }
.warn { border-left:3px solid #d97706; padding:6px 10px; margin:8px 0; }
.bar { display:inline-block; height:8px; background:var(--accent); border-radius:2px; vertical-align:middle; }
"""


def _cell(v) -> str:
    if v is None:
        return '<td class="n muted">–</td>'
    if isinstance(v, (int, float)):
        return f'<td class="n">{v}</td>'
    return f"<td>{html.escape(str(v))}</td>"


def _table(title: str, columns: list[str], rows: list[list], note: str = "", bar_col: str | None = None) -> str:
    out = [f"<h2>{html.escape(title)}</h2>"]
    if note:
        out.append(f'<p class="note">{html.escape(note)}</p>')
    if not rows:
        out.append('<p class="muted">Nothing to show.</p>')
        return "\n".join(out)
    bi = columns.index(bar_col) if bar_col in columns else None
    mx = max((r[bi] for r in rows if bi is not None and isinstance(r[bi], (int, float))), default=0) or 1
    out.append('<div class="wrap"><table><thead><tr>' + "".join(f"<th>{html.escape(c)}</th>" for c in columns) +
               ("<th></th>" if bi is not None else "") + "</tr></thead><tbody>")
    for r in rows:
        bar = ""
        if bi is not None:
            w = int(120 * max(0, r[bi] or 0) / mx) if isinstance(r[bi], (int, float)) else 0
            bar = f'<td><span class="bar" style="width:{w}px"></span></td>'
        out.append("<tr>" + "".join(_cell(v) for v in r) + bar + "</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


BARS = {"Modules: input-side vs output-side stall": "bottleneck score", "Interfaces (ready/valid)": "stall %",
        "Time in each value (change probes)": "%"}


def write_html(path: str, tables: list[Table], sections: list[Section], summary: dict, warnings: list[str]):
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
             f"initial-scale=1'><title>rtltrace report</title><style>{CSS}</style></head><body>",
             "<h1>rtltrace report</h1>",
             '<p class="muted">' + " · ".join(f"{html.escape(k)}: {html.escape(str(v))}" for k, v in summary.items()) +
             "</p>"]
    for w in warnings:
        parts.append(f'<p class="warn">{html.escape(w)}</p>')
    for t in tables:
        parts.append(_table(t.title, t.columns, t.rows, t.note, BARS.get(t.title)))
    for s in sections:
        if s.columns:
            parts.append(_table(s.title, s.columns, s.rows, s.text))
        else:
            parts.append(f"<h2>{html.escape(s.title)}</h2><pre>{html.escape(s.text)}</pre>")
    parts.append("</body></html>")
    Path(path).write_text("\n".join(parts))


def write_csv(dirpath: str, tables: list[Table]):
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    for t in tables:
        name = "".join(c if c.isalnum() else "_" for c in t.title.lower()).strip("_")[:60]
        with open(d / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(t.columns)
            w.writerows(t.rows)


def write_text(out, tables: list[Table], sections: list[Section]):
    for t in list(tables) + [Table(s.title, s.columns, s.rows, s.text) for s in sections]:
        print(f"\n== {t.title}", file=out)
        if not t.rows:
            print("   (nothing)", file=out)
            continue
        cols = [str(c) for c in t.columns]
        rows = [["–" if v is None else str(v) for v in r] for r in t.rows]
        w = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
        print("   " + "  ".join(c.ljust(w[i]) for i, c in enumerate(cols)), file=out)
        for r in rows:
            print("   " + "  ".join(v.ljust(w[i]) for i, v in enumerate(r)), file=out)

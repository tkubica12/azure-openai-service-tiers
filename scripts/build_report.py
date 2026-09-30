"""Build a self-contained HTML report from downloaded result blobs (results/blobs/**).

    uv run python scripts/build_report.py [--campaign main] [--out results/report.html]
"""
from __future__ import annotations

import argparse
import html
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from common import RESULTS_DIR, ROOT

COLORS = {"standard": "#2563eb", "priority": "#7c3aed", "flex": "#f59e0b", "batch": "#10b981"}
LABEL = {"standard": "Standard", "priority": "Priority", "flex": "Flex", "batch": "Batch"}
PRIORITY_TPS_TARGET = 80  # documented Priority latency target for gpt-5.6-sol: 99 % of 5-min windows with p50 > 80 tokens/s


# ------------------------------------------------------------------ data loading
def load(campaigns: list[str] | None) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (records, run_files, batch_files)."""
    blob_dir = RESULTS_DIR / "blobs"
    records, files, batches = [], [], []
    for path in sorted(blob_dir.glob("*/*.json")):
        run_id = path.parent.name
        if campaigns and not any(run_id.startswith(c + "-") for c in campaigns):
            continue
        if path.name == "batch-job.json":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["_file"] = str(path.relative_to(RESULTS_DIR))
        doc.setdefault("run_id", run_id)
        files.append(doc)
        if doc["mode"] == "batch":
            batches.append(doc)
        for r in doc["records"]:
            r["_message"] = doc.get("message", {})
            r["_worker"] = doc.get("worker", {})
            records.append(r)
    return records, files, batches


def dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ------------------------------------------------------------------ statistics
def is_flex_meter(meter: str) -> bool:
    """Flex meters are named '... Flex Gl' or abbreviated '... Fl Gl' (e.g. '56sol ShCo Opt Fl Gl 1M Tokens')."""
    t = f" {str(meter).lower()} "
    return "flex" in t or " fl " in t


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


def stats(values: list[float]) -> dict:
    return {
        "n": len(values),
        "min": min(values) if values else None,
        "p50": pct(values, 50),
        "p90": pct(values, 90),
        "p95": pct(values, 95),
        "max": max(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "stdev": statistics.stdev(values) if len(values) > 1 else None,
    }


def fmt(v, unit="s", digits=2) -> str:
    if v is None:
        return "–"
    if unit == "s" and v >= 120:
        return f"{v / 60:.1f} min"
    return f"{v:.{digits}f} {unit}".strip()


def esc(s) -> str:
    return html.escape(str(s))


# ------------------------------------------------------------------ SVG charts
def svg_strip(groups: dict[str, list[float]], title: str, unit: str = "s", log: bool = False,
              width: int = 820) -> str:
    """Horizontal strip/box chart – one row per group, every sample drawn as a dot."""
    groups = {k: v for k, v in groups.items() if v}
    if not groups:
        return "<p><em>no data</em></p>"
    all_v = [x for v in groups.values() for x in v]
    lo, hi = min(all_v), max(all_v)
    if log:
        lo = max(lo, 0.1)
        f = lambda x: math.log10(max(x, lo))
        a, b = f(lo) - 0.05, f(hi) + 0.05
    else:
        f = lambda x: x
        a, b = 0.0, hi * 1.05 if hi > 0 else 1.0
    left, right, row_h, top = 150, 30, 46, 30
    plot_w = width - left - right
    height = top + row_h * len(groups) + 40
    X = lambda x: left + (f(x) - a) / (b - a) * plot_w
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">',
             f'<text x="{left}" y="18" class="ct">{esc(title)}</text>']
    # axis ticks
    ticks = []
    if log and unit == "s":
        nice = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400, 28800, 86400]
        ticks = [t for t in nice if a <= math.log10(t) <= b]
    elif log:
        e = math.floor(a)
        while e <= b:
            for m in (1, 2, 5):
                t = m * 10 ** e
                if a <= math.log10(t) <= b:
                    ticks.append(t)
            e += 1
    else:
        step = 10 ** math.floor(math.log10(b / 5)) if b > 0 else 1
        for m in (1, 2, 5, 10):
            if b / (step * m) <= 8:
                step *= m
                break
        t = 0.0
        while t <= b:
            ticks.append(t)
            t += step
    y_axis = top + row_h * len(groups)
    for t in ticks:
        x = X(t)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{y_axis}" class="grid"/>')
        if unit != "s" or t < 60:
            label = f"{t:g}" + ("s" if unit == "s" and log else "")
        elif t < 3600:
            label = f"{t / 60:g}m"
        else:
            label = f"{t / 3600:g}h"
        parts.append(f'<text x="{x:.1f}" y="{y_axis + 16}" class="tick">{label}</text>')
    parts.append(f'<text x="{left + plot_w / 2}" y="{y_axis + 34}" class="tick">{esc(unit)}'
                 f'{" (log scale)" if log else ""}</text>')
    for i, (name, vals) in enumerate(groups.items()):
        cy = top + row_h * i + row_h / 2
        mode = name.split(" ")[0].lower()
        color = COLORS.get(mode, "#6b7280")
        parts.append(f'<text x="{left - 10}" y="{cy + 4}" class="lbl">{esc(name)}</text>')
        q1, q2, q3 = pct(vals, 25), pct(vals, 50), pct(vals, 75)
        p90 = pct(vals, 90)
        parts.append(f'<rect x="{X(q1):.1f}" y="{cy - 12}" width="{max(X(q3) - X(q1), 1):.1f}" height="24" '
                     f'fill="{color}" opacity="0.18" stroke="{color}"/>')
        parts.append(f'<line x1="{X(q2):.1f}" y1="{cy - 14}" x2="{X(q2):.1f}" y2="{cy + 14}" stroke="{color}" '
                     f'stroke-width="3"/>')
        parts.append(f'<line x1="{X(p90):.1f}" y1="{cy - 10}" x2="{X(p90):.1f}" y2="{cy + 10}" stroke="{color}" '
                     f'stroke-dasharray="3,2" stroke-width="2"/>')
        for j, v in enumerate(vals):
            jitter = ((j * 7919) % 17 - 8) * 0.9
            parts.append(f'<circle cx="{X(v):.1f}" cy="{cy + jitter:.1f}" r="3" fill="{color}" opacity="0.75">'
                         f'<title>{esc(name)}: {v:.2f} {esc(unit)}</title></circle>')
    parts.append("</svg>")
    parts.append('<p class="legend">Box = p25–p75, thick line = median (p50), dashed line = p90, dots = '
                 'individual samples.</p>')
    return "".join(parts)


def svg_timeseries(series: dict[str, list[tuple[datetime, float]]], title: str, unit: str = "s",
                   width: int = 820, height: int = 300, log: bool = False) -> str:
    series = {k: v for k, v in series.items() if v}
    if not series:
        return "<p><em>no data</em></p>"
    pts = [p for v in series.values() for p in v]
    t0, t1 = min(p[0] for p in pts), max(p[0] for p in pts)
    span = max((t1 - t0).total_seconds(), 60)
    vmax = max(p[1] for p in pts) * 1.08
    vmin = max(min(p[1] for p in pts), 0.1) if log else 0.0
    f = (lambda x: math.log10(max(x, vmin))) if log else (lambda x: x)
    left, right, top, bottom = 60, 20, 30, 50
    pw, ph = width - left - right, height - top - bottom
    X = lambda t: left + (t - t0).total_seconds() / span * pw
    Y = lambda v: top + ph - (f(v) - f(vmin)) / ((f(vmax) - f(vmin)) or 1) * ph
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">',
             f'<text x="{left}" y="18" class="ct">{esc(title)}</text>',
             f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" class="axis"/>',
             f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + ph}" class="axis"/>']
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4 if not log else 10 ** (f(vmin) + (f(vmax) - f(vmin)) * k / 4)
        y = Y(v)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" class="ytick">{v:.1f}</text>')
    multi_day = (t1 - t0).total_seconds() > 20 * 3600
    for k in range(6):
        t = t0.timestamp() + span * k / 5
        x = left + pw * k / 5
        parts.append(f'<text x="{x:.1f}" y="{top + ph + 18}" class="tick">'
                     f'{datetime.fromtimestamp(t, tz=t0.tzinfo).strftime("%d.%m. %H:%M" if multi_day else "%H:%M")}</text>')
    parts.append(f'<text x="{left + pw / 2}" y="{height - 8}" class="tick">time of request (UTC) · y: {esc(unit)}'
                 f'{" (log)" if log else ""}</text>')
    lx = left + 10
    for name, vals in series.items():
        mode = name.split(" ")[0].lower()
        color = COLORS.get(mode, "#6b7280")
        for t, v in vals:
            parts.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="3.2" fill="{color}" opacity="0.75">'
                         f'<title>{esc(name)} {t:%H:%M:%S}: {v:.2f} {esc(unit)}</title></circle>')
        parts.append(f'<circle cx="{lx}" cy="{top + 6}" r="5" fill="{color}"/>'
                     f'<text x="{lx + 9}" y="{top + 10}" class="lg">{esc(name)}</text>')
        lx += 18 + 7 * len(name)
    parts.append("</svg>")
    return "".join(parts)


def svg_batch_timeline(batches: list[dict], width: int = 820) -> str:
    rows = []
    for b in sorted(batches, key=lambda d: d["run_id"]):
        bt = b.get("batch", {})
        created, prog, fin, done = (dt(bt.get("created_at")), dt(bt.get("in_progress_at")),
                                    dt(bt.get("finalizing_at")), dt(bt.get("completed_at") or bt.get("failed_at")
                                                                    or bt.get("expired_at")))
        if not created or not done:
            continue
        segs = []
        cur = created
        for label, nxt, color in (("validating", prog, "#a7f3d0"), ("in_progress", fin, "#10b981"),
                                  ("finalizing", done, "#047857")):
            if nxt:
                segs.append((label, (cur - created).total_seconds(), (nxt - created).total_seconds(), color))
                cur = nxt
        rows.append((b["run_id"], segs, (done - created).total_seconds(), bt.get("status")))
    if not rows:
        return "<p><em>no completed batch jobs</em></p>"
    tmax = max(r[2] for r in rows) * 1.05
    left, right, row_h, top = 150, 70, 22, 30
    pw = width - left - right
    height = top + row_h * len(rows) + 40
    X = lambda s: left + s / tmax * pw
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">',
             f'<text x="{left}" y="18" class="ct">Batch job phases per run (service timestamps, created → '
             f'completed)</text>']
    for k in range(6):
        s = tmax * k / 5
        x = X(s)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + row_h * len(rows)}" class="grid"/>')
        parts.append(f'<text x="{x:.1f}" y="{top + row_h * len(rows) + 16}" class="tick">{s / 60:.0f} min</text>')
    for i, (run_id, segs, total, status) in enumerate(rows):
        y = top + i * row_h
        parts.append(f'<text x="{left - 8}" y="{y + 15}" class="lbl">{esc(run_id)}</text>')
        for label, s0, s1, color in segs:
            parts.append(f'<rect x="{X(s0):.1f}" y="{y + 3}" width="{max(X(s1) - X(s0), 1):.1f}" height="{row_h - 6}" '
                         f'fill="{color}"><title>{label}: {(s1 - s0) / 60:.1f} min</title></rect>')
        parts.append(f'<text x="{X(total) + 4:.1f}" y="{y + 15}" class="ytick" text-anchor="start">'
                     f'{total / 60:.1f} min {"" if status == "completed" else esc(status)}</text>')
    lx = left
    for label, color in (("validating", "#a7f3d0"), ("in_progress", "#10b981"), ("finalizing", "#047857")):
        parts.append(f'<rect x="{lx}" y="{height - 14}" width="10" height="10" fill="{color}"/>'
                     f'<text x="{lx + 14}" y="{height - 5}" class="lg">{label}</text>')
        lx += 110
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------ time of day
PRAGUE_OFFSET_H = 2  # CEST (UTC+2) for the whole measurement window (ends before the switch on 25 Oct)
BUCKET_H = 3


def bucket_of(t: datetime) -> int:
    return t.hour // BUCKET_H


def bucket_label(b: int) -> str:
    s, e = b * BUCKET_H, b * BUCKET_H + BUCKET_H
    ps, pe = (s + PRAGUE_OFFSET_H) % 24, (e + PRAGUE_OFFSET_H) % 24
    return f"{s:02d}–{e:02d} UTC ({ps:02d}–{pe:02d} Prague)"


def svg_bars(labels: list[str], series: dict[str, list[float | None]], title: str, unit: str = "s",
             width: int = 820, height: int = 280) -> str:
    vals = [v for vs in series.values() for v in vs if v is not None]
    if not vals:
        return "<p><em>no data</em></p>"
    vmax = max(vals) * 1.15
    left, right, top, bottom = 60, 20, 34, 58
    pw, ph = width - left - right, height - top - bottom
    gw = pw / len(labels)
    bw = gw * 0.8 / len(series)
    Y = lambda v: top + ph - v / vmax * ph
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">',
             f'<text x="{left}" y="18" class="ct">{esc(title)}</text>',
             f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" class="axis"/>']
    for k in range(5):
        v = vmax * k / 4
        parts.append(f'<line x1="{left}" y1="{Y(v):.1f}" x2="{left + pw}" y2="{Y(v):.1f}" class="grid"/>'
                     f'<text x="{left - 6}" y="{Y(v) + 4:.1f}" class="ytick">{v:.1f}</text>')
    for i, lab in enumerate(labels):
        x0 = left + i * gw + gw * 0.1
        for j, (name, vs) in enumerate(series.items()):
            v = vs[i]
            if v is None:
                continue
            color = COLORS.get(name.split(" ")[0].lower(), "#6b7280")
            parts.append(f'<rect x="{x0 + j * bw:.1f}" y="{Y(v):.1f}" width="{bw - 2:.1f}" height="{top + ph - Y(v):.1f}" '
                         f'fill="{color}"><title>{esc(name)} {esc(lab)}: {v:.2f} {esc(unit)}</title></rect>')
        short = lab.split(" (")[0]
        parts.append(f'<text x="{left + i * gw + gw / 2:.1f}" y="{top + ph + 16}" class="tick">{esc(short)}</text>')
    lx = left
    for name in series:
        color = COLORS.get(name.split(" ")[0].lower(), "#6b7280")
        parts.append(f'<rect x="{lx}" y="{height - 16}" width="10" height="10" fill="{color}"/>'
                     f'<text x="{lx + 14}" y="{height - 7}" class="lg">{esc(name)}</text>')
        lx += 30 + 7 * len(name)
    parts.append(f'<text x="{left + pw / 2}" y="{top + ph + 34}" class="tick">UTC hour bucket · y: {esc(unit)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def time_of_day(by: dict, batches: list[dict]) -> tuple[str, str, str]:
    """Returns (bucket table html, bar chart svg, weekday/weekend table html)."""
    def ok(mode, pair):
        return [r for r in by[(mode, pair)] if r["success"] and r.get("started_at")]

    std, pri, flex, stdb = ok("standard", "A"), ok("priority", "A"), ok("flex", "A"), ok("standard", "B")
    flex_all = by[("flex", "A")]
    turn = [(dt(b["batch"]["created_at"]), b["records"][0]["latency_s"]) for b in batches
            if b.get("records") and b.get("batch", {}).get("created_at") and b["records"][0].get("latency_s")]
    rows, p50_std, p50_pri, p50_flex = [], [], [], []
    labels = []
    for b in range(24 // BUCKET_H):
        s = [r["latency_s"] for r in std if bucket_of(dt(r["started_at"])) == b]
        p = [r["latency_s"] for r in pri if bucket_of(dt(r["started_at"])) == b]
        f = [r["latency_s"] for r in flex if bucket_of(dt(r["started_at"])) == b]
        sb = [r["latency_s"] for r in stdb if bucket_of(dt(r["started_at"])) == b]
        bt = [v for t, v in turn if bucket_of(t) == b]
        n429 = sum(1 for r in flex_all if r.get("started_at") and bucket_of(dt(r["started_at"])) == b
                   for a in r.get("attempts", []) if a.get("status") == 429)
        fail = sum(1 for r in flex_all if r.get("started_at") and bucket_of(dt(r["started_at"])) == b and not r["success"])
        if not (s or p or f or sb or bt):
            continue
        labels.append(bucket_label(b))
        p50_std.append(pct(s, 50))
        p50_pri.append(pct(p, 50))
        p50_flex.append(pct(f, 50))
        ratio = f"{pct(f, 50) / pct(s, 50):.2f}×" if s and f else "–"
        pratio = f"{pct(p, 50) / pct(s, 50):.2f}×" if s and p else "–"
        rows.append(f"<tr><td class='l'>{esc(bucket_label(b))}</td><td>{len(s)}</td><td>{fmt(pct(s, 50))}</td>"
                    f"<td>{fmt(pct(s, 90))}</td><td>{len(p)}</td><td>{fmt(pct(p, 50))}</td><td>{fmt(pct(p, 90))}</td>"
                    f"<td>{pratio}</td><td>{len(f)}</td><td>{fmt(pct(f, 50))}</td><td>{fmt(pct(f, 90))}</td>"
                    f"<td>{fmt(max(f) if f else None)}</td><td>{ratio}</td><td>{n429}</td><td>{fail}</td>"
                    f"<td>{fmt(pct(sb, 50))}</td><td>{len(bt)}</td><td>{fmt(pct(bt, 50))}</td></tr>")
    table = ("<div style='overflow-x:auto'><table><thead><tr><th>hour bucket</th><th>Std n</th><th>Std p50</th><th>Std p90</th>"
             "<th>Prio n</th><th>Prio p50</th><th>Prio p90</th><th>Prio/Std p50</th><th>Flex n</th>"
             "<th>Flex p50</th><th>Flex p90</th><th>Flex max</th><th>Flex/Std p50</th><th>Flex 429s</th><th>Flex failed</th>"
             "<th>Std B p50</th><th>Batch jobs</th><th>Batch p50</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table></div>")
    chart = svg_bars(labels, {"Standard p50": p50_std, "Priority p50": p50_pri, "Flex p50": p50_flex},
                     "Median latency by time of day (UTC), gpt-5.6-sol")

    wk_rows = []
    for name, pred in (("Weekday (Mon–Fri)", lambda t: t.weekday() < 5), ("Weekend (Sat–Sun)", lambda t: t.weekday() >= 5)):
        s = [r["latency_s"] for r in std if pred(dt(r["started_at"]))]
        p = [r["latency_s"] for r in pri if pred(dt(r["started_at"]))]
        f = [r["latency_s"] for r in flex if pred(dt(r["started_at"]))]
        n429 = sum(1 for r in flex_all if r.get("started_at") and pred(dt(r["started_at"]))
                   for a in r.get("attempts", []) if a.get("status") == 429)
        bt = [v for t, v in turn if pred(t)]
        wk_rows.append(f"<tr><td class='l'>{name}</td><td>{len(s)}</td><td>{fmt(pct(s, 50))}</td><td>{fmt(pct(s, 90))}</td>"
                       f"<td>{len(p)}</td><td>{fmt(pct(p, 50))}</td><td>{fmt(pct(p, 90))}</td>"
                       f"<td>{len(f)}</td><td>{fmt(pct(f, 50))}</td><td>{fmt(pct(f, 90))}</td><td>{n429}</td>"
                       f"<td>{len(bt)}</td><td>{fmt(pct(bt, 50))}</td></tr>")
    wk = ("<table><thead><tr><th></th><th>Std n</th><th>Std p50</th><th>Std p90</th><th>Prio n</th><th>Prio p50</th>"
          "<th>Prio p90</th><th>Flex n</th><th>Flex p50</th>"
          "<th>Flex p90</th><th>Flex 429s</th><th>Batch jobs</th><th>Batch p50</th></tr></thead><tbody>"
          + "".join(wk_rows) + "</tbody></table>")
    return table, chart, wk


# ------------------------------------------------------------------ metering & billing
# measured (mode, pair) -> retail meter names (input, cached input, output); Flex on gpt-5.6-sol has no public meter yet
PRICE_METERS = {
    ("standard", "A"): ("5.6 sol ShortCo Inp Std Gl", "5.6 sol ShortCo Cd Inp Std Gl", "5.6 sol ShortCo Opt Std Gl"),
    ("priority", "A"): ("5.6 sol ShortCo Inp PP Gl", "5.6 sol ShortCo Cd Inp PP Gl", "5.6 sol ShortCo Opt PP Gl"),
    ("flex", "A"): None,
    ("standard", "B"): ("5.4 mini Inp Gl", "5.4 mini cd Inp Gl", "5.4 mini Opt Gl"),
    ("batch", "B"): ("5.4 mini Batch Inp Gl", "5.4 mini Batch cd Inp Gl", "5.4 mini Batch Opt Gl"),
}


def billing_section(records: list[dict]) -> str:
    path = RESULTS_DIR / "billing_evidence.json"
    ev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    retail = {r["meter"]: r["price_per_1m"] for r in ev.get("retail", [])}

    # --- tokens measured by the workers + cost estimate
    tok = defaultdict(lambda: {"n": 0, "inp": 0, "cached": 0, "out": 0, "reasoning": 0})
    for r in records:
        if not r["success"]:
            continue
        u = r.get("usage") or {}
        mode = r["mode"]
        # a Priority request downgraded to Standard is billed at the Standard price -> own row
        if mode == "priority" and r.get("service_tier") not in ("priority", "auto"):
            mode = "priority→standard"
        t = tok[(mode, r["pair"])]
        t["n"] += 1
        t["cached"] += u.get("cached_tokens") or 0
        t["inp"] += (u.get("prompt_tokens") or 0) - (u.get("cached_tokens") or 0)
        t["out"] += u.get("completion_tokens") or 0
        t["reasoning"] += u.get("reasoning_tokens") or 0

    def prices(key):
        meters = PRICE_METERS.get(key)
        if meters and all(m in retail for m in meters):
            return [retail[m] for m in meters], "retail meter"
        if key == ("flex", "A") and all(m in retail for m in PRICE_METERS[("standard", "A")]):
            return [retail[m] * 0.5 for m in PRICE_METERS[("standard", "A")]], "50 % of Standard (Fl meter not in retail API; billed in Cost Management)"
        if key == ("priority→standard", "A"):
            p, _ = prices(("standard", "A"))
            return p, "Standard meter (downgraded, billed as Standard)"
        return None, "–"

    cost_rows = []
    for key in (("standard", "A"), ("priority", "A"), ("priority→standard", "A"), ("flex", "A"), ("standard", "B"),
                ("batch", "B")):
        t = tok.get(key)
        if not t:
            continue
        p, src = prices(key)
        std_key = ("standard", key[1])
        ps, _ = prices(std_key)
        cost = (t["inp"] * p[0] + t["cached"] * p[1] + t["out"] * p[2]) / 1e6 if p else None
        cost_std = (t["inp"] * ps[0] + t["cached"] * ps[1] + t["out"] * ps[2]) / 1e6 if ps else None
        per1k = cost / t["n"] * 1000 if cost is not None and t["n"] else None
        saving = ("baseline" if key[0] == "standard" else
                  f"{100 * (1 - cost / cost_std):.0f} % cheaper" if cost and cost_std and cost < cost_std else
                  f"{cost / cost_std:.1f}× the price" if cost and cost_std else "–")
        cost_rows.append(
            f"<tr><td class='l'>{esc(LABEL.get(key[0], 'Priority requested → served Standard'))} · pair {key[1]}</td>"
            f"<td>{t['n']:,}</td><td>{t['inp']:,}</td>"
            f"<td>{t['cached']:,}</td><td>{t['out']:,}</td><td>{t['reasoning']:,}</td>"
            f"<td>{' / '.join(f'${x:.3g}' for x in p) if p else '–'}</td><td class='l'>{esc(src)}</td>"
            f"<td>{f'${cost:.4f}' if cost is not None else '–'}</td><td>{f'${per1k:.2f}' if per1k is not None else '–'}</td>"
            f"<td>{saving}</td></tr>")
    cost_table = ("<table><thead><tr><th></th><th>requests</th><th>input tok</th><th>cached tok</th><th>output tok</th>"
                  "<th>of which reasoning</th><th>$/1M in / cached / out</th><th>price source</th><th>est. cost</th>"
                  "<th>per 1k requests</th><th>vs Standard</th></tr></thead><tbody>" + "".join(cost_rows)
                  + "</tbody></table>")

    # --- Azure Monitor metrics split by service tier
    agg = defaultdict(lambda: defaultdict(float))
    for m in ev.get("metrics", []):
        k = (m["deployment"], m["tier_request"], m["tier_response"])
        name = m["metric"]
        if name == "AzureOpenAIRequests":
            name = f"req {m.get('status')}"
        agg[k][name] += m["total"]
    status_cols = sorted({c for v in agg.values() for c in v if c.startswith("req ")})
    metric_rows = []
    mismatch = []
    for (dep, treq, tresp), v in sorted(agg.items()):
        if treq != tresp:
            mismatch.append((dep, treq, tresp))
        flag = " <b>⚠ requested ≠ served</b>" if treq != tresp else ""
        metric_rows.append(
            f"<tr><td class='l'>{esc(dep)}</td><td class='l'>{esc(treq)}</td><td class='l'>{esc(tresp)}{flag}</td>"
            + "".join(f"<td>{v.get(c, 0):,.0f}</td>" for c in status_cols)
            + f"<td>{v.get('ProcessedPromptTokens', 0):,.0f}</td><td>{v.get('GeneratedTokens', 0):,.0f}</td></tr>")
    metric_table = ("<table><thead><tr><th>deployment</th><th>ServiceTierRequest</th><th>ServiceTierResponse</th>"
                    + "".join(f"<th>requests HTTP {c[4:]}</th>" for c in status_cols)
                    + "<th>ProcessedPromptTokens</th><th>GeneratedTokens</th></tr></thead><tbody>"
                    + "".join(metric_rows) + "</tbody></table>") if metric_rows else "<p><em>no metric data</em></p>"

    # --- Cost Management: meters actually charged
    cost = ev.get("cost", {})
    cm_rows = cost.get("rows", [])
    hl = lambda m: is_flex_meter(str(m)) or " pp " in f" {str(m).lower()} "
    if cm_rows:
        total = sum(r.get("Cost") or 0 for r in cm_rows)
        cm_html = ("<table><thead><tr><th>meter category</th><th>meter subcategory</th><th>meter</th><th>quantity</th>"
                   "<th>cost</th></tr></thead><tbody>"
                   + "".join(
                       f"<tr><td class='l'>{esc(r.get('MeterCategory'))}</td><td class='l'>{esc(r.get('MeterSubCategory'))}</td>"
                       f"<td class='l'>{'<b>' if hl(r.get('Meter', '')) else ''}{esc(r.get('Meter'))}"
                       f"{'</b>' if hl(r.get('Meter', '')) else ''}</td>"
                       f"<td>{r.get('UsageQuantity') or 0:,.4f}</td><td>{r.get('Cost') or 0:,.4f} {esc(r.get('Currency', ''))}</td></tr>"
                       for r in cm_rows)
                   + f"<tr><td class='l' colspan='4'><b>Total resource group</b></td><td><b>{total:,.2f}</b></td></tr></tbody></table>")
    else:
        cm_html = ("<p class='note'>Cost Management had no rows yet for this resource group "
                   f"({esc(cost.get('error', 'usage data typically appears 8–24 h after consumption'))}). "
                   "Re-run <code>scripts/collect_billing_evidence.py</code> later.</p>")

    flex_meters = sorted(m for m in retail if is_flex_meter(m))
    has_56_flex = any("5.6" in m or "56 " in m or "56sol" in m for m in flex_meters)
    cm_flex56 = sorted({r.get("Meter", "") for r in cm_rows
                        if is_flex_meter(r.get("Meter", "")) and ("56sol" in r.get("Meter", "") or "5.6" in r.get("Meter", ""))})
    same_as_batch = ""
    fm = [retail.get(k) for k in ("54 mini Inp Flex Gl", "54 mini Opt Flex Gl", "5.4 mini Batch Inp Gl",
                                    "5.4 mini Batch Opt Gl", "5.4 mini Inp Gl", "5.4 mini Opt Gl")]
    if all(fm):
        same_as_batch = (f"; e.g. gpt-5.4-mini: Standard ${fm[4]:.3g} / ${fm[5]:.3g}, Flex ${fm[0]:.3g} / ${fm[1]:.3g}, "
                         f"Global Batch ${fm[2]:.3g} / ${fm[3]:.3g} per 1M input / output tokens")
    mismatch_html = ""
    fs_path = RESULTS_DIR / "tier_support.json"
    if fs_path.exists():
        fs = json.loads(fs_path.read_text(encoding="utf-8"))
        rows = "".join(
            f"<tr><td class='l'><code>{esc(r['deployment'])}</code></td><td class='l'>{esc(r.get('model', '–'))}</td>"
            f"<td class='l'>{esc(r['requested'])}</td><td class='l'>{'<b>' if r.get('served') != r['requested'] else ''}"
            f"{esc(r.get('served') or r.get('error', '–'))}{'</b>' if r.get('served') != r['requested'] else ''}</td>"
            f"<td>{r.get('status', '–')}</td><td>{r['latency_s']:.2f} s</td></tr>"
            for r in fs.get("results", []))
        mismatch_html += (f"<h3>Direct check: requested vs served tier (<code>check_tier_support.py</code>, "
                          f"{esc(fs.get('checked_at', ''))})</h3><table class='wrap'><tr><th>Deployment</th><th>Model</th>"
                          f"<th>Requested</th><th>Served</th><th>HTTP</th><th>Latency</th></tr>{rows}</table>")
    flex_mm = [(d, a, b) for d, a, b in mismatch if a == "flex"]
    prio_mm = [(d, a, b) for d, a, b in mismatch if a == "priority"]
    if flex_mm:
        mismatch_html += ("<p class='note'><b>Requested Flex but served Standard:</b> "
                          + ", ".join(f"<code>{esc(d)}</code> requested <code>{esc(a)}</code> → served <code>{esc(b)}</code>"
                                      for d, a, b in flex_mm)
                          + ". The Flex documentation says that since 25 Sep 2026 a Flex request to an unsupported model is "
                          "rejected with HTTP 400 <code>invalid_request_error</code>; in this measurement the request on "
                          "gpt-5.4-mini was instead <b>accepted (HTTP 200) and processed on Standard</b> (billed as Standard). "
                          "Always check <code>service_tier</code> in the response (the workers record it).</p>")
    if prio_mm:
        mismatch_html += ("<p class='note'><b>Requested Priority but served Standard (downgrade):</b> "
                          + ", ".join(f"<code>{esc(d)}</code> requested <code>{esc(a)}</code> → served <code>{esc(b)}</code>"
                                      for d, a, b in prio_mm)
                          + ". Priority requests can be downgraded to Standard (ramp-up limit, peak load, long context); "
                          "they are then billed at the Standard price.</p>")

    pp_prices = [retail.get(m) for m in PRICE_METERS[("priority", "A")]]
    std_prices = [retail.get(m) for m in PRICE_METERS[("standard", "A")]]
    pp_txt = (f"gpt-5.6-sol Priority ${pp_prices[0]:.3g} / ${pp_prices[1]:.3g} / ${pp_prices[2]:.3g} vs Standard "
              f"${std_prices[0]:.3g} / ${std_prices[1]:.3g} / ${std_prices[2]:.3g} per 1M input / cached / output tokens "
              f"({pp_prices[0] / std_prices[0]:.1f}×)" if all(pp_prices) and all(std_prices) else "")

    return f"""
<p>How the service tiers show up in metering and on the bill – verified from the price list, Azure Monitor metrics and
direct API checks:</p>
<ul>
 <li><b>Priority = premium meters.</b> Priority usage is charged on dedicated <b>PP meters</b>
 (<code>5.6 sol ShortCo Inp PP Gl</code>, <code>… Cd Inp PP Gl</code>, <code>… Opt PP Gl</code>){': ' + pp_txt if pp_txt else ''}.
 Requests that are downgraded to Standard (the response then says <code>service_tier="default"</code>) are billed at the Standard
 price. Priority uses the same deployment and the same TPM quota as Standard.</li>
 <li><b>Flex = discounted meters.</b> Flex usage is charged on its own <b>Flex meters</b> (separate meter IDs from Standard),
 so in Cost analysis it can be separated by grouping/filtering on <i>Meter</i>. Flex input and output tokens cost 50 % of the
 Standard price of the same model; the cached-input discount applies on top. The public price list already contains Flex meters
 for the GPT-5.4 / 5.5 family ({esc(', '.join(flex_meters[:3]))}{' …' if len(flex_meters) > 3 else ''}){same_as_batch}.{(' The public retail price API does not list a gpt-5.6-sol Flex meter yet, but <b>Cost Management already charges it on dedicated meters</b> (' + ', '.join('<code>' + esc(m) + '</code>' for m in cm_flex56) + ') – see the table below; the Flex estimate below uses 50 % of the Standard meter.') if not has_56_flex and cm_flex56 else (' <b>No public Flex meter exists yet for gpt-5.6-sol</b>, so the Flex estimate below uses 50 % of the Standard meter.' if not has_56_flex else '')}</li>
 <li><b>Same deployment, same quota.</b> Standard, Priority and Flex requests go to the same GlobalStandard deployment and share
 its TPM/RPM quota – there is no separate "Flex deployment" or "Priority deployment" (Priority can optionally be made the
 deployment default). <i>Batch</i> in contrast needs a separate GlobalBatch deployment with its own enqueued-token quota and has
 its own Batch meters.</li>
 <li><b>Metrics dimension.</b> Azure Monitor metrics of the Foundry resource (<i>ModelRequests, AzureOpenAIRequests,
 ProcessedPromptTokens, GeneratedTokens, TokenTransaction, TimeToResponse…</i>) carry the dimensions
 <code>ServiceTierRequest</code> and <code>ServiceTierResponse</code>, so the tiers on one deployment can be charted and alerted
 on separately – and a mismatch (requested ≠ served) exposes Priority downgrades. (<i>InputTokens/OutputTokens/TotalTokens</i>
 do not have this dimension.)</li>
 <li><b>Rejected Flex requests are free.</b> A 429 "no capacity" answer is not billed; there is no automatic fallback to
 Standard – the client decides whether to retry Flex or send the request as Standard.</li>
 <li><b>Tier selection.</b> Request body <code>service_tier</code> (<code>auto</code> / <code>default</code> /
 <code>priority</code> / <code>flex</code>) or HTTP header <code>x-ms-service-tier</code> (the header wins).
 The response reports the tier that actually processed the request.</li>
</ul>
{mismatch_html}
<h3>Azure Monitor: requests and tokens per deployment and service tier</h3>
{metric_table}
<p class="sub">Source: <code>az monitor metrics list</code> on the Foundry resource, window {esc(' → '.join(ev.get('window', [])))},
queried {esc(ev.get('queried_at', '–'))}. Platform metrics can lag a few minutes and counts may differ slightly from the
worker-side measurements (e.g. the laptop smoke tests are included here).</p>
<h3>Measured tokens and estimated token cost</h3>
{cost_table}
<p class="sub">Tokens from the <code>usage</code> object of each successful response; reasoning tokens are billed as output tokens.
Prices: Azure Retail Prices API, region swedencentral, USD list price per 1M tokens.</p>
<h3>Cost Management: meters actually charged</h3>
{cm_html}
"""


# ------------------------------------------------------------------ tables
def stats_table(rows: list[tuple[str, dict, dict]]) -> str:
    """rows: (label, stats dict, extra columns dict)."""
    extra_cols = []
    for _, _, extra in rows:
        extra_cols += [c for c in extra if c not in extra_cols]
    head = "".join(f"<th>{c}</th>" for c in ["", "n", "min", "p50", "p90", "p95", "max", "mean", "stdev"]
                   + extra_cols)
    body = []
    for label, s, extra in rows:
        cells = [f"<td class='l'>{esc(label)}</td>", f"<td>{s['n']}</td>"]
        cells += [f"<td>{fmt(s[k])}</td>" for k in ("min", "p50", "p90", "p95", "max", "mean", "stdev")]
        cells += [f"<td>{esc(extra.get(c, ''))}</td>" for c in extra_cols]
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


# ------------------------------------------------------------------ heavy streaming campaign
HEAVY_PREFIX = "heavy-"
HEAVY_METRICS = [
    ("ttft_s", "Time to first token", "s"),
    ("ttlt_s", "Time to last token", "s"),
    ("output_tps", "Output tokens/s (after first token)", "tok/s"),
    ("e2e_tps", "Output tokens/s (whole request)", "tok/s"),
]


def is_heavy(run_id: str) -> bool:
    return run_id.startswith(HEAVY_PREFIX)


HEAVY_IMAGE_LIVE = datetime(2026, 9, 30, 10, 52, tzinfo=timezone.utc)  # streaming image serving all workers


def heavy_valid(r: dict) -> bool:
    # Only records produced by the streaming worker: the first heavy slot also holds leftovers from the old
    # (non-streaming) image. Successful streaming records always carry ttft_s; failures are kept after the rollout.
    if r.get("success"):
        return r.get("ttft_s") is not None
    started = dt(r.get("started_at"))
    return bool(started and started >= HEAVY_IMAGE_LIVE)


def heavy_data(records: list[dict]) -> dict:
    rs = [r for r in records if is_heavy(r["run_id"]) and r.get("pair") == "A"
          and r["mode"] in ("standard", "priority", "flex") and heavy_valid(r)]
    by = defaultdict(list)
    for r in rs:
        by[r["mode"]].append(r)
    ok = {m: {r["run_id"]: r for r in by[m] if r["success"] and r.get("ttft_s") is not None} for m in by}
    return {"records": rs, "by": by, "ok": ok}


def heavy_summary_rows(hd: dict) -> list[dict]:
    """One row per tier with p50/p90/p99 of the heavy metrics – used by the report and printed for the README."""
    rows = []
    for m in ("standard", "priority", "flex"):
        good = list(hd["ok"].get(m, {}).values())
        row = {"mode": m, "requests": len(hd["by"].get(m, [])), "ok": len(good)}
        for key, _, _ in HEAVY_METRICS:
            v = [r[key] for r in good if r.get(key) is not None]
            row[key] = {p: pct(v, p) for p in (50, 90, 99)}
        comp = [r["usage"]["completion_tokens"] for r in good if (r.get("usage") or {}).get("completion_tokens")]
        prm = [r["usage"]["prompt_tokens"] for r in good if (r.get("usage") or {}).get("prompt_tokens")]
        row["out_tok"] = (statistics.fmean(comp), min(comp), max(comp)) if comp else None
        row["in_tok"] = statistics.fmean(prm) if prm else None
        row["tiers"] = Counter(r.get("service_tier") for r in good)
        row["codes"] = Counter(a.get("status") for r in hd["by"].get(m, []) for a in r.get("attempts", [])
                               if a.get("status") != 200)
        rows.append(row)
    return rows


def heavy_section(records: list[dict]) -> str:
    hd = heavy_data(records)
    if not hd["records"]:
        return "<p>No heavy-campaign runs downloaded yet.</p>"
    runs = sorted({r["run_id"] for r in hd["records"]})
    rows = heavy_summary_rows(hd)
    first = min(dt(r["started_at"]) for r in hd["records"] if r.get("started_at"))
    last = max(dt(r["finished_at"]) for r in hd["records"] if r.get("finished_at"))

    head = ("<tr><th rowspan='2'>tier (gpt-5.6-sol)</th><th rowspan='2'>ok / requests</th>"
            + "".join(f"<th colspan='3'>{esc(label)}</th>" for _, label, _ in HEAVY_METRICS)
            + "<th rowspan='2'>output tokens avg (min–max)</th><th rowspan='2'>returned service_tier</th>"
              "<th rowspan='2'>non-200 attempts</th></tr><tr>"
            + "<th>p50</th><th>p90</th><th>p99</th>" * len(HEAVY_METRICS) + "</tr>")
    body = []
    for row in rows:
        cells = [f"<td class='l'>{LABEL[row['mode']]}</td><td>{row['ok']}/{row['requests']}</td>"]
        for key, _, unit in HEAVY_METRICS:
            cells += [f"<td>{fmt(row[key][p], unit, 2 if unit == 's' else 0)}</td>" for p in (50, 90, 99)]
        ot = row["out_tok"]
        cells.append(f"<td>{f'{ot[0]:.0f} ({ot[1]}–{ot[2]})' if ot else '–'}</td>")
        cells.append(f"<td>{esc(', '.join(f'{k}×{v}' for k, v in row['tiers'].items()) or '–')}</td>")
        cells.append(f"<td>{esc(', '.join(f'{k}×{v}' for k, v in row['codes'].items()) or 'none')}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    table = f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"

    # paired per-run ratios to Standard (same message, same moment)
    ok = hd["ok"]
    ratio_rows = []
    for m in ("priority", "flex"):
        common = sorted(set(ok.get("standard", {})) & set(ok.get(m, {})))
        for key, label, _ in HEAVY_METRICS[:3]:
            v = [ok[m][k][key] / ok["standard"][k][key] for k in common
                 if ok["standard"][k].get(key) and ok[m][k].get(key) is not None]
            ratio_rows.append(
                f"<tr><td class='l'>{LABEL[m]} ÷ Standard – {esc(label)}</td><td>{len(v)}</td>"
                + "".join(f"<td>{f'{pct(v, p):.2f}×' if v else '–'}</td>" for p in (10, 50, 90)) + "</tr>")
    ratio_table = ("<table><thead><tr><th>paired ratio (same run)</th><th>n</th><th>p10</th><th>p50</th><th>p90</th></tr>"
                   f"</thead><tbody>{''.join(ratio_rows)}</tbody></table>")

    series = {key: {f"{LABEL[m]}": [(dt(r["started_at"]), r[key]) for r in ok.get(m, {}).values()]
                    for m in ("standard", "priority", "flex")} for key, _, _ in HEAVY_METRICS}
    first_in = next((row["in_tok"] for row in rows if row["in_tok"]), None)

    return f"""
<p>Since 2026-09-30 the scheduled probe (<code>probe-job</code>, every 15 minutes) runs the <b>heavy streaming test</b>: one
request per tier with a deterministic prompt of ≈{f'{first_in:,.0f}' if first_in else '4,600'} input tokens (50 support tickets,
with a nonce at the start so prompt caching cannot help) that asks for exactly 16 structured answers, ≈1,150 output tokens.
<code>reasoning_effort="none"</code> keeps the output length stable (no hidden reasoning tokens). The response is <b>streamed</b>,
so the worker records the time to the first content token (TTFT) and to the last token (TTLT) separately.
Counting was reset for this campaign: the earlier short-prompt runs are shown in the sections below as history.</p>
<p class="sub">{len(runs)} runs · {first:%Y-%m-%d %H:%M} – {last:%Y-%m-%d %H:%M} UTC. Output tokens/s after first token =
completion tokens ÷ (TTLT − TTFT); whole request = completion tokens ÷ TTLT.
Why not 100k input / 10k output: the tokens-per-minute quota counts input + <code>max_tokens</code>, so one such request
(≈110k) exceeds the whole 100k TPM of a capacity-100 deployment shared by all three tiers, and 96 runs a day would cost
≈$200/day.</p>
{table}
<h3>Paired ratio to Standard (same message, same moment)</h3>
{ratio_table}
{svg_strip({LABEL[m]: [r["ttft_s"] for r in ok.get(m, {}).values()] for m in ("standard", "priority", "flex")},
           "Time to first token", log=True)}
{svg_strip({LABEL[m]: [r["ttlt_s"] for r in ok.get(m, {}).values()] for m in ("standard", "priority", "flex")},
           "Time to last token")}
{svg_strip({LABEL[m]: [r["output_tps"] for r in ok.get(m, {}).values() if r.get("output_tps")]
            for m in ("standard", "priority", "flex")}, "Output tokens per second (after first token)", unit="tok/s")}
{svg_timeseries(series["ttlt_s"], "Time to last token over time")}
{svg_timeseries(series["ttft_s"], "Time to first token over time", log=True)}
"""


# ------------------------------------------------------------------ report
def code_snippets() -> dict[str, str]:
    llm = (ROOT / "worker" / "app" / "llm.py").read_text(encoding="utf-8")
    batch = (ROOT / "worker" / "app" / "batch_worker.py").read_text(encoding="utf-8")
    online = (ROOT / "worker" / "app" / "online_worker.py").read_text(encoding="utf-8")

    def loc(text: str) -> int:
        return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))

    return {"llm_loc": loc(llm), "batch_loc": loc(batch), "online_loc": loc(online)}


def build(campaigns: list[str] | None, out_path: Path) -> None:
    all_records, files, batches = load(campaigns)
    if not all_records:
        raise SystemExit("no results found in results/blobs – run download_results.py first")
    heavy_html = heavy_section(all_records)
    heavy_rows = heavy_summary_rows(heavy_data(all_records))
    # Sections 3–7 describe the original short-prompt, non-streaming campaigns (history); heavy runs have their own section.
    records = [r for r in all_records if not is_heavy(r["run_id"])]
    files = [f for f in files if not is_heavy(f["run_id"])]
    batches = [b for b in batches if not is_heavy(b["run_id"])]
    if not records:
        raise SystemExit("only heavy runs selected – the report needs the historical campaigns as well")
    runs = sorted({r["run_id"] for r in records})
    by = defaultdict(list)
    for r in records:
        by[(r["mode"], r["pair"])].append(r)

    def lat(mode, pair, key="latency_s", ok_only=True):
        return [r[key] for r in by[(mode, pair)] if r.get(key) is not None and (r["success"] or not ok_only)]

    def extra(mode, pair):
        rs = by[(mode, pair)]
        ok = sum(1 for r in rs if r["success"])
        retried = sum(1 for r in rs if (r.get("attempt_count") or 1) > 1)
        codes = Counter(a.get("status") for r in rs for a in r.get("attempts", []) if a.get("status") != 200)
        tiers = Counter(r.get("service_tier") for r in rs if r["success"])
        comp = [r["usage"]["completion_tokens"] for r in rs if r.get("usage") and r["usage"].get("completion_tokens")]
        return {
            "requests": len(rs),
            "success": f"{ok}/{len(rs)} ({100 * ok / len(rs):.0f} %)" if rs else "–",
            "retried": retried,
            "non-200 attempts": ", ".join(f"{k}×{v}" for k, v in codes.items()) or "none",
            "returned service_tier": ", ".join(f"{k}×{v}" for k, v in tiers.items()) or "–",
            "avg output tokens": f"{statistics.fmean(comp):.0f}" if comp else "–",
        }

    a_std, a_pri, a_flex = lat("standard", "A"), lat("priority", "A"), lat("flex", "A")
    b_std = lat("standard", "B")

    # --- Pair A three-tier analysis: matched runs, paired per-prompt ratios, throughput
    ok_a = {m: {(r["run_id"], r["prompt_id"]): r for r in by[(m, "A")] if r["success"]}
            for m in ("standard", "priority", "flex")}
    tri_keys = sorted(set(ok_a["standard"]) & set(ok_a["priority"]) & set(ok_a["flex"]))
    tri_runs = sorted({k[0] for k in tri_keys})
    matched = {m: [ok_a[m][k]["latency_s"] for k in tri_keys] for m in ok_a}

    def paired(m: str) -> list[float]:
        keys = sorted(set(ok_a["standard"]) & set(ok_a[m]))
        return [ok_a[m][k]["latency_s"] / ok_a["standard"][k]["latency_s"] for k in keys
                if ok_a["standard"][k]["latency_s"]]

    pr_pri, pr_flex = paired("priority"), paired("flex")

    def tps(r: dict) -> float | None:
        c = (r.get("usage") or {}).get("completion_tokens")
        return c / r["latency_s"] if c and r.get("latency_s") else None

    tps_rows = []
    tps_vals = {}
    for m in ("standard", "priority", "flex"):
        v = [x for x in (tps(r) for r in ok_a[m].values()) if x is not None]
        tps_vals[m] = v
        if m == "priority":
            served = [x for x in (tps(r) for r in ok_a[m].values() if r.get("service_tier") in ("priority", "auto"))
                      if x is not None]
        else:
            served = v
        s = stats(v)
        above = sum(1 for x in served if x > PRIORITY_TPS_TARGET)
        tps_rows.append(
            f"<tr><td class='l'>{LABEL[m]}</td><td>{s['n']}</td><td>{fmt(s['min'], 'tok/s', 0)}</td>"
            f"<td>{fmt(s['p50'], 'tok/s', 0)}</td><td>{fmt(s['p90'], 'tok/s', 0)}</td><td>{fmt(s['max'], 'tok/s', 0)}</td>"
            f"<td>{f'{100 * above / len(served):.0f} % ({above}/{len(served)})' if served else '–'}</td></tr>")
    tps_table = ("<table><thead><tr><th>tier (gpt-5.6-sol)</th><th>n</th><th>min</th><th>p50</th><th>p90</th><th>max</th>"
                 f"<th>requests &gt; {PRIORITY_TPS_TARGET} tok/s</th></tr></thead><tbody>" + "".join(tps_rows)
                 + "</tbody></table>")
    pri_rs = by[("priority", "A")]
    pri_ok = [r for r in pri_rs if r["success"]]
    pri_served = sum(1 for r in pri_ok if r.get("service_tier") in ("priority", "auto"))
    pri_down = [r for r in pri_ok if r.get("service_tier") not in ("priority", "auto")]
    b_batch_job = sorted({(b["run_id"], r["latency_s"]) for b in batches for r in b["records"]
                          if r.get("latency_s") is not None})
    b_batch = [v for _, v in b_batch_job]
    b_batch_total = [b["records"][0]["total_s"] for b in batches if b["records"]]
    b_batch_e2e = [b["records"][0]["end_to_end_s"] for b in batches if b["records"] and b["records"][0].get("end_to_end_s")]
    b_std_run = []  # per-run wall time to finish all prompts for Standard pair B
    for run in runs:
        rs = [r for r in by[("standard", "B")] if r["run_id"] == run and r["success"]]
        if rs:
            b_std_run.append((max(dt(r["finished_at"]) for r in rs) - min(dt(r["started_at"]) for r in rs))
                             .total_seconds())

    # pickup / cold start
    # worker-priority was created at 11:38 UTC on 2026-09-29; messages queued before that waited for a worker
    # that did not exist yet, so their pickup delay is a deployment artifact, not a cold start.
    priority_worker_created = datetime(2026, 9, 29, 11, 38, tzinfo=timezone.utc)
    pickup = defaultdict(list)
    cold = Counter()
    pickup_excluded = Counter()
    seen = set()
    for f in files:
        key = (f["run_id"], f["mode"])
        if key in seen:
            continue
        seen.add(key)
        m = f.get("message", {})
        if m.get("pickup_delay_s") is not None:
            sched = m.get("scheduled_enqueue_at") or m.get("enqueued_at")
            if f["mode"] == "priority" and sched and dt(sched) < priority_worker_created:
                pickup_excluded[f["mode"]] += 1
                continue
            pickup[f["mode"]].append(m["pickup_delay_s"])
            cold[(f["mode"], bool(m.get("cold_start")))] += 1

    def ratio(a, b):
        return f"{a / b:.2f}×" if a and b else "–"

    s_a_std, s_a_pri, s_a_flex = stats(a_std), stats(a_pri), stats(a_flex)
    s_b_std, s_b_batch = stats(b_std), stats(b_batch)
    flex_rs = by[("flex", "A")]
    flex_ok = sum(1 for r in flex_rs if r["success"])
    flex_429 = sum(1 for r in flex_rs for a in r.get("attempts", []) if a.get("status") == 429)
    first_ts = min(dt(r["started_at"]) for r in records if r.get("started_at"))
    last_ts = max(dt(r["finished_at"]) for r in records if r.get("finished_at"))
    batch_states = Counter(b.get("batch", {}).get("status") for b in batches)
    snippets = code_snippets()
    deployments = sorted({(r["pair"], r["mode"], r["deployment"], r.get("model") or "") for r in records})

    kpis = f"""
<div class="kpis">
 <div class="kpi"><div class="k">Runs</div><div class="v">{len(runs)}</div>
   <div class="s">{first_ts:%Y-%m-%d %H:%M} – {last_ts:%Y-%m-%d %H:%M} UTC · {len(tri_runs)} runs with all three tiers</div></div>
 <div class="kpi priority"><div class="k">Priority median latency vs Standard</div>
   <div class="v">{f"{statistics.median(pr_pri):.2f}×" if pr_pri else "–"}</div>
   <div class="s">median of paired per-prompt ratios (n={len(pr_pri)}) · p50 {fmt(s_a_pri['p50'])} vs {fmt(s_a_std['p50'])}</div></div>
 <div class="kpi priority"><div class="k">Priority actually served as Priority</div>
   <div class="v">{f"{100 * pri_served / len(pri_ok):.0f} %" if pri_ok else "–"}</div>
   <div class="s">{pri_served}/{len(pri_ok)} responses · {len(pri_down)} downgraded to Standard · price 2× Standard</div></div>
 <div class="kpi flex"><div class="k">Flex median latency vs Standard</div>
   <div class="v">{f"{statistics.median(pr_flex):.2f}×" if pr_flex else "–"}</div>
   <div class="s">median of paired ratios (n={len(pr_flex)}) · p50 {fmt(s_a_flex['p50'])} vs {fmt(s_a_std['p50'])} · p90 {fmt(s_a_flex['p90'])} vs {fmt(s_a_std['p90'])}</div></div>
 <div class="kpi flex"><div class="k">Flex success rate</div>
   <div class="v">{(100 * flex_ok / len(flex_rs)) if flex_rs else 0:.0f} %</div>
   <div class="s">{flex_ok}/{len(flex_rs)} requests · {flex_429}× HTTP 429 seen · price 0.5× Standard</div></div>
 <div class="kpi batch"><div class="k">Batch job turnaround (reference)</div>
   <div class="v">{fmt(s_b_batch['p50'])}</div>
   <div class="s">vs {fmt(statistics.median(b_std_run) if b_std_run else None)} for the same prompts on Standard · jobs: {', '.join(f'{k}×{v}' for k, v in batch_states.items())}</div></div>
</div>"""

    css = """
body{font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;color:#1f2937;background:#f8fafc}
main{max-width:1000px;margin:0 auto;padding:24px 28px 60px;background:#fff}
h1{font-size:28px;margin:8px 0 4px} h2{margin-top:40px;border-bottom:2px solid #e5e7eb;padding-bottom:6px}
h3{margin-top:26px} .sub{color:#6b7280;margin-top:0}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0}
.kpi{border:1px solid #e5e7eb;border-left:5px solid #2563eb;border-radius:8px;padding:12px}
.kpi.flex{border-left-color:#f59e0b}.kpi.batch{border-left-color:#10b981}.kpi.priority{border-left-color:#7c3aed}
.kpi .k{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.03em}.kpi .v{font-size:26px;font-weight:700}
.kpi .s{font-size:12px;color:#4b5563}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0 18px}
th,td{border-bottom:1px solid #e5e7eb;padding:5px 7px;text-align:right;white-space:nowrap} th{background:#f3f4f6}
td.l,th:first-child{text-align:left} .wrap td{white-space:normal;text-align:left}
.chart{width:100%;height:auto;margin:6px 0} .ct{font-weight:600;font-size:13px}
.tick{font-size:11px;fill:#6b7280;text-anchor:middle}.ytick{font-size:11px;fill:#6b7280;text-anchor:end}
.lbl{font-size:12px;text-anchor:end}.lg{font-size:12px}.grid{stroke:#eef0f3}.axis{stroke:#9ca3af}
.legend{font-size:12px;color:#6b7280;margin-top:-4px}
pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow:auto;font-size:12.5px;line-height:1.45}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px} .hl{background:#854d0e;color:#fff;padding:0 3px;border-radius:3px}
.note{background:#fffbeb;border:1px solid #fde68a;padding:10px 14px;border-radius:8px;font-size:14px}
.arch{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;font-size:13px;margin:12px 0}
.box{border:1px solid #cbd5e1;border-radius:8px;padding:8px 10px;background:#f8fafc;flex:1;min-width:140px}
.box b{display:block;margin-bottom:3px} .arrow{align-self:center;font-size:20px;color:#94a3b8}
details{margin:10px 0} summary{cursor:pointer;font-weight:600}
@media print{body{background:#fff} main{padding:0}}
"""

    dep_rows = "".join(f"<tr><td class='l'>{esc(p)}</td><td class='l'>{esc(LABEL[m])}</td><td class='l'>{esc(d)}</td>"
                       f"<td class='l'>{esc(mo)}</td></tr>" for p, m, d, mo in deployments)

    pair_a_table = stats_table([
        ("Standard – latency of successful call", s_a_std, extra("standard", "A")),
        ("Priority – latency of successful call", s_a_pri, extra("priority", "A")),
        ("Flex – latency of successful call", s_a_flex, extra("flex", "A")),
    ])
    pair_a_matched = stats_table([
        (f"{LABEL[m]} – matched prompts", stats(matched[m]), {}) for m in ("standard", "priority", "flex")
    ])
    ratio_table = stats_table([
        ("Priority ÷ Standard (same prompt, same run)", stats(pr_pri), {}),
        ("Flex ÷ Standard (same prompt, same run)", stats(pr_flex), {}),
    ]).replace(" s</td>", "×</td>")
    pair_a_total = stats_table([
        ("Standard – incl. retries/backoff", stats(lat("standard", "A", "total_s", False)), {}),
        ("Priority – incl. retries/backoff", stats(lat("priority", "A", "total_s", False)), {}),
        ("Flex – incl. retries/backoff", stats(lat("flex", "A", "total_s", False)), {}),
    ])
    pair_b_table = stats_table([
        ("Standard – per request latency", s_b_std, extra("standard", "B")),
        ("Standard – all prompts of a run, wall time", stats(b_std_run), {}),
        ("Batch – job created → completed (service)", s_b_batch, {}),
        ("Batch – submit → worker noticed completion", stats(b_batch_total), {}),
        ("Batch – message visible → results stored", stats(b_batch_e2e), {}),
    ])
    std_b_runs = sorted({f["run_id"] for f in files if f["mode"] == "standard" and f.get("pair") == "B"})
    batch_runs = {b["run_id"] for b in batches}
    missing_batch = [r for r in std_b_runs if r not in batch_runs]
    batch_gap_html = ""
    if missing_batch:
        batch_gap_html = (
            f"<p class='note'><b>Batch results missing for {len(missing_batch)} of {len(std_b_runs)} runs</b> "
            f"({esc(missing_batch[0])} … {esc(missing_batch[-1])}). From about 10:00 UTC on 2026-09-29 the Batch control plane "
            "in Sweden Central degraded: the Files upload returned 408/504 and later <code>POST /batches</code> answered with "
            "<b>504 Gateway Time-out after 60 s even though the batch was created</b> server-side. The original worker retried the "
            "message, got the same timeout, and dead-lettered it, leaving untracked (orphaned) batch jobs. Standard, Priority and "
            "Flex on the same resource were not affected. The worker now looks up an existing batch by "
            "<code>metadata.run_id</code> before creating one and after a failed create. Because job creation is not "
            "idempotent, a Batch client needs this extra care that a synchronous Flex call does not.</p>")
    pickup_rows = []
    for mode in ("standard", "priority", "flex", "batch"):
        s = stats(pickup[mode])
        extra = {"cold starts": cold[(mode, True)], "warm": cold[(mode, False)]}
        if pickup_excluded[mode]:
            extra["excluded (queued before worker existed)"] = pickup_excluded[mode]
        pickup_rows.append((f"{LABEL[mode]} worker", s, extra))
    pickup_table = stats_table(pickup_rows)

    ts_a = {
        "Standard gpt-5.6-sol": [(dt(r["started_at"]), r["latency_s"]) for r in by[("standard", "A")] if r["success"]],
        "Priority gpt-5.6-sol": [(dt(r["started_at"]), r["latency_s"]) for r in by[("priority", "A")] if r["success"]],
        "Flex gpt-5.6-sol": [(dt(r["started_at"]), r["latency_s"]) for r in by[("flex", "A")] if r["success"]],
    }

    per_run_rows = []
    for run in runs:
        def med(mode, pair):
            v = [r["latency_s"] for r in by[(mode, pair)] if r["run_id"] == run and r["success"]]
            return fmt(statistics.median(v)) if v else "–"

        def okc(mode, pair):
            rs = [r for r in by[(mode, pair)] if r["run_id"] == run]
            return f"{sum(r['success'] for r in rs)}/{len(rs)}" if rs else "–"
        bj = [b for b in batches if b["run_id"] == run]
        bstat = bj[0]["batch"]["status"] if bj else "pending"
        blat = fmt(bj[0]["records"][0]["latency_s"]) if bj and bj[0]["records"] else "–"
        start = min((dt(r["started_at"]) for r in records if r["run_id"] == run and r.get("started_at")), default=None)
        per_run_rows.append(
            f"<tr><td class='l'>{esc(run)}</td><td>{start:%d.%m. %H:%M} </td><td>{med('standard', 'A')}</td>"
            f"<td>{okc('standard', 'A')}</td><td>{med('priority', 'A')}</td><td>{okc('priority', 'A')}</td>"
            f"<td>{med('flex', 'A')}</td><td>{okc('flex', 'A')}</td>"
            f"<td>{med('standard', 'B')}</td><td>{okc('standard', 'B')}</td><td>{esc(bstat)}</td><td>{blat}</td></tr>")

    raw_rows = []
    for r in sorted(records, key=lambda r: (r["run_id"], r["mode"], r["pair"], r["prompt_id"])):
        u = r.get("usage") or {}
        raw_rows.append(
            f"<tr><td class='l'>{esc(r['run_id'])}</td><td class='l'>{esc(LABEL[r['mode']])}</td><td>{esc(r['pair'])}</td>"
            f"<td class='l'>{esc(r['prompt_id'].split('-')[-1])}</td><td>{'✔' if r['success'] else '✘'}</td>"
            f"<td>{fmt(r.get('latency_s'))}</td><td>{fmt(r.get('total_s'))}</td><td>{r.get('attempt_count', '–')}</td>"
            f"<td>{esc(r.get('service_tier') or '–')}</td><td>{u.get('prompt_tokens', '–')}</td>"
            f"<td>{u.get('completion_tokens', '–')}</td><td>{esc((r.get('headers') or {}).get('x-ms-region', ''))}</td></tr>")

    tod_table, tod_chart, tod_week = time_of_day(by, batches)

    dns_seen = defaultdict(lambda: [set(), set()])  # (mode, host) -> [ips, replicas]
    for f in files:
        w = f.get("worker", {})
        for d in (w.get("dns") or {}).values():
            k = (f["mode"], d.get("host"))
            dns_seen[k][0].update(d.get("ips") or [])
            dns_seen[k][1].add(w.get("replica"))
    dns_rows = "".join(
        f"<tr><td class='l'>{esc(LABEL[m])}</td><td class='l'>{esc(h)}</td><td class='l'>{esc(', '.join(sorted(ips)))}</td>"
        f"<td>{len(reps)}</td><td class='l'>{'private ✔' if all(i.startswith('10.') for i in ips) else 'PUBLIC ✘'}</td></tr>"
        for (m, h), (ips, reps) in sorted(dns_seen.items()))
    evidence_path = RESULTS_DIR / "network_evidence.json"
    storage_rows = ""
    if evidence_path.exists():
        ev = json.loads(evidence_path.read_text(encoding="utf-8"))
        storage_rows = "".join(
            f"<tr><td class='l'>{esc(r.get('source'))}</td><td class='l'>{esc(r.get('CallerIpAddress'))}</td>"
            f"<td class='l'>{esc(r.get('AuthenticationType'))}</td><td class='l'>{esc(r.get('StatusText'))}</td>"
            f"<td>{int(r.get('requests', 0)):,}</td></tr>" for r in ev.get("storage", []))
        evidence_note = f"Storage access log aggregated from Log Analytics (StorageBlobLogs), queried {esc(ev.get('queried_at'))}."
    else:
        evidence_note = "Run <code>scripts/collect_network_evidence.py</code> to include Storage access-log evidence."

    body = f"""
<main>
<h1>Azure OpenAI service tiers: Standard vs Priority vs Flex</h1>
<p class="sub">Real measurements from a scale-to-zero Azure Container Apps application (Batch API as a historical reference)
 · generated {datetime.now():%Y-%m-%d %H:%M} · {len(records)} measured requests across {len(runs)} runs</p>
{kpis}

<h2>1. What was tested</h2>
<p>Every test run is one Service Bus message sent to the topic <code>llm-tests</code> – either from a laptop
(<code>send_tests.py</code>, campaign <code>main</code>: 5 prompts per run) or by the scheduled Container Apps Job <code>probe-job</code>
(campaign <code>probe</code>: 1 short prompt every 15 minutes until 2026-09-30; since then campaign <code>heavy</code>: one
≈4.6k-input / ≈1.15k-output streaming request per tier every 15 minutes, see section 3). The topic fans it out to
four subscriptions, each consumed by its own Container App that scales from zero (KEDA <code>azure-servicebus</code> scaler with the
managed identity). All workers run the same container image; only <code>WORKER_MODE</code> differs.
Results land in a Blob Storage account reachable from the apps only through its private endpoint.</p>
<div class="arch">
 <div class="box"><b>Laptop / probe job</b>send_tests.py<br>probe-job (cron */15)<br>(Entra ID)</div><div class="arrow">→</div>
 <div class="box"><b>Service Bus topic</b>llm-tests<br>subs: standard · priority · flex · batch<br>queue: batch-status</div><div class="arrow">→</div>
 <div class="box"><b>Container Apps (VNet)</b>worker-standard<br>worker-priority<br>worker-flex<br>worker-batch<br>min 0 / max 1 replica</div><div class="arrow">→</div>
 <div class="box"><b>Foundry (private endpoint)</b>gpt-5.6-sol GlobalStandard<br>gpt-5.4-mini GlobalStandard<br>gpt-5.4-mini GlobalBatch</div><div class="arrow">→</div>
 <div class="box"><b>Blob Storage (private endpoint)</b>results/&lt;run&gt;/*.json</div>
</div>
<p>Each comparison uses one model for all its tiers:</p>
<ul>
 <li><b>Main comparison – Standard vs Priority vs Flex</b> on <b>gpt-5.6-sol</b> (the only model that supports Flex; it also
 supports Priority). One GlobalStandard deployment, same code – only the <code>service_tier</code> request parameter differs
 (<code>default</code> / <code>priority</code> / <code>flex</code>). All three workers receive the same message at the same moment.</li>
 <li><b>Historical reference – Standard vs Batch</b> on <b>gpt-5.4-mini</b> (gpt-5.6-sol has no Global Batch deployment type).</li>
</ul>
<p class="sub">Priority was added to the running experiment later than Standard/Flex, so section 4 also shows a comparison restricted
to <b>matched prompts</b> (same run, same prompt, all three tiers succeeded) and paired per-prompt ratios, which remove the
time-of-day and prompt-mix bias.</p>
<table class="wrap"><thead><tr><th>Pair</th><th>Mode</th><th>Deployment</th><th>Model returned by the API</th></tr></thead>
<tbody>{dep_rows}</tbody></table>
<p>All authentication is Microsoft Entra ID: one user-assigned managed identity for all workers (Service Bus, Storage, Foundry,
ACR pull, KEDA scaler) and the operator's Azure CLI login on the laptop. Storage and Foundry have local (key) auth disabled.</p>

<h2>2. Impact on the application code</h2>
<div class="cols">
<div><h3>Standard → Priority / Flex: one parameter</h3>
<pre>client.chat.completions.create(
    model=deployment,
    messages=[{{"role": "user", "content": prompt}}],
    <span class="hl">service_tier="priority"</span>,   # or "flex"; was "default"
    reasoning_effort="low",
    max_completion_tokens=1000,
)
# Flex: + client timeout raised to 900 s
#       + retry/backoff on HTTP 429 (no capacity)
# Priority: check response.service_tier – may be "default"
#           (downgraded, billed as Standard)</pre>
<p>The Standard, Priority and Flex workers share <b>the same function</b> (<code>worker/app/llm.py</code>, {snippets['llm_loc']} lines incl.
retry logic). Synchronous request/response is preserved. Priority needs nothing else (it is a latency premium on the same
deployment and quota); Flex needs tolerance for longer and more variable latency and occasional 429s, which asynchronous,
queue-driven processing handles naturally.</p></div>
<div><h3>Standard → Batch: different architecture</h3>
<pre># 1. build JSONL, model = GlobalBatch deployment per line
# 2. upload file   files.create(purpose="batch")
# 3. wait until the file is "processed"
# 4. batches.create(input_file_id, endpoint,
#                   completion_window="24h")
# 5. persist job state (blob) – the worker may scale to 0
# 6. schedule a poll message (Service Bus, +60 s) …
# 7. … repeat until completed/failed/expired
# 8. download output + error files, match custom_id</pre>
<p>Batch needs its own worker (<code>worker/app/batch_worker.py</code>, {snippets['batch_loc']} lines), a separate
<b>GlobalBatch deployment</b>, durable job state, a polling mechanism (extra queue <code>batch-status</code> + second KEDA rule)
and result correlation. Results come back as a file, not as a response.</p></div>
</div>

<h2>3. Heavy streaming test – TTFT, TTLT, tokens/s (current campaign)</h2>
{heavy_html}

<h2>4. Short-prompt history – Standard vs Priority vs Flex (gpt-5.6-sol)</h2>
<p class="note">History: the original campaigns up to 2026-09-30 (short prompts, ~85 output tokens, non-streaming). Kept for
reference; the current measurements are in section 3.</p>
<p><b>Latency</b> is the wall-clock time of the successful HTTP call (non-streaming, full response) as seen by the worker in
Azure (same region as the model). Requests in one run are sequential per worker; all tiers start at the same moment (same
message), so they see the same time of day.</p>
<h3>All measured requests</h3>
{pair_a_table}
{svg_strip({"Standard gpt-5.6-sol": a_std, "Priority gpt-5.6-sol": a_pri, "Flex gpt-5.6-sol": a_flex},
           "Request latency, successful calls", log=True)}
{svg_timeseries(ts_a, "Latency over time (each dot = one request)", log=True)}
<h3>Matched prompts only ({len(tri_keys)} prompts in {len(tri_runs)} runs where all three tiers succeeded)</h3>
{pair_a_matched}
<h3>Paired ratio to Standard (same run, same prompt)</h3>
<p class="sub">Ratio &lt; 1× = faster than Standard. Independent of prompt length and time of day.</p>
{ratio_table}
<h3>Generation speed (output tokens ÷ request latency)</h3>
<p class="sub">Output tokens incl. reasoning divided by the full non-streaming request time – a <i>lower bound</i> of the
token-generation speed (includes time to first token and network). Microsoft's Priority latency target for gpt-5.6-sol is
&gt; {PRIORITY_TPS_TARGET} tokens/s (p50 per 5-min window, 99 % of windows); for Priority only responses actually served as
Priority are counted in the last column. Caveat: the test answers are short (~85 output tokens), so the fixed
time to first token dominates and none of the tiers can reach {PRIORITY_TPS_TARGET} tok/s by this measure; the numbers
are meant for comparing the tiers with each other, not for checking the SLA-style target (that needs streaming and long
outputs).</p>
{tps_table}
{svg_strip({f"{LABEL[m]} gpt-5.6-sol": tps_vals[m] for m in ("standard", "priority", "flex")},
           "Output tokens per second (per request)", unit="tok/s", log=True)}
<h3>Including retries (what the application waited in total)</h3>
{pair_a_total}
{f'''<p class="note"><b>Priority downgrades:</b> {len(pri_down)} of {len(pri_ok)} successful Priority requests came back with
<code>service_tier="{esc(pri_down[0].get("service_tier"))}"</code> (processed and billed as Standard): {", ".join(esc(r["run_id"]) for r in pri_down[:10])}{" …" if len(pri_down) > 10 else ""}.</p>''' if pri_down else ""}

<h2>5. Historical reference – Standard vs Batch (gpt-5.4-mini)</h2>
<p>The Batch API is the older way to get a 50 % discount. For Batch there is no per-request latency – the unit is a job. The job's
service-side duration (<code>created_at → completed_at</code>) applies to every request in it. The same prompts were sent to the
Standard deployment of the same model for comparison.</p>
{pair_b_table}
{batch_gap_html}
{svg_strip({"Standard per request": b_std, "Standard whole run": b_std_run, "Batch job": b_batch,
            "Batch end-to-end": b_batch_e2e}, "Standard vs Batch turnaround", log=True)}
{svg_batch_timeline(batches)}

<h2>6. Time of day, day and night</h2>
<p>Flex runs on spare, preemptible capacity, so latency and the chance of HTTP 429 depend on overall load in the region; Priority
should stay fast even at peak (but may be downgraded to Standard at peak).
Besides the main campaign, a scheduled Container Apps Job (<code>probe-job</code>, cron <code>*/15 * * * *</code>) sent one short
prompt every 15 minutes around the clock (this section covers the short-prompt history; heavy runs are in section 3). Buckets are {BUCKET_H} hours in UTC; Prague local time (CEST, UTC+2) is in brackets.
Batch jobs are bucketed by the hour they were created.</p>
{tod_table}
{tod_chart}
<h3>Weekday vs weekend</h3>
{tod_week}

<h2>7. Scale-from-zero behaviour</h2>
<p>Pickup delay = time between the message becoming visible on Service Bus and a worker receiving it. A cold start means the
replica process started after the message became visible (KEDA had to scale the app from 0 → 1).</p>
{pickup_table}
{svg_strip({f"{LABEL[m]} worker": pickup[m] for m in ("standard", "priority", "flex", "batch")}, "Message pickup delay")}

<h2>8. Per-run overview</h2>
<details{' open' if len(runs) <= 30 else ''}><summary>{len(runs)} runs</summary>
<table><thead><tr><th>Run</th><th>start (UTC)</th><th>Std p50</th><th>ok</th><th>Prio p50</th><th>ok</th><th>Flex p50</th><th>ok</th>
<th>Std B p50</th><th>ok</th><th>Batch</th><th>Batch job</th></tr></thead><tbody>{''.join(per_run_rows)}</tbody></table>
</details>

<h2>9. Metering and billing</h2>
{billing_section(records)}

<h2>10. Security and private networking evidence</h2>
<p>Every worker resolves the Storage and Foundry host names at start-up and stores the result with its output. Private
IPs (10.60.2.x = private endpoint subnet) prove that traffic from Container Apps goes through the private endpoints and the
private DNS zones linked to the VNet. No keys or connection strings exist anywhere – key auth is disabled on Storage,
Foundry and Service Bus, and every call is authenticated with the user-assigned managed identity (OAuth).</p>
<table><thead><tr><th>worker</th><th>host</th><th>resolved to</th><th>replicas</th><th>path</th></tr></thead>
<tbody>{dns_rows}</tbody></table>
{f'''<table><thead><tr><th>source</th><th>caller IP</th><th>auth type</th><th>status</th><th>requests</th></tr></thead>
<tbody>{storage_rows}</tbody></table>''' if storage_rows else ''}
<p class="sub">{evidence_note}</p>

<h2>11. Conclusions</h2>
{conclusions(s_a_std, s_a_pri, s_a_flex, pr_pri, pr_flex, pri_served, len(pri_ok), tps_vals, flex_ok, len(flex_rs),
             flex_429, s_b_std, s_b_batch, b_std_run, pickup)}

<details><summary>All {len(records)} measured requests</summary>
<table><thead><tr><th>run</th><th>mode</th><th>pair</th><th>prompt</th><th>ok</th><th>latency</th><th>total</th><th>attempts</th>
<th>tier</th><th>in tok</th><th>out tok</th><th>region</th></tr></thead><tbody>{''.join(raw_rows)}</tbody></table>
</details>
<p class="sub">Source data: {len(files)} JSON result files in <code>results/blobs</code>. Report generated by
<code>scripts/build_report.py</code>.</p>
</main>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                        f"<title>Azure OpenAI service tiers: Standard vs Priority vs Flex</title><style>{css}</style></head>"
                        f"<body>{body}</body></html>", encoding="utf-8")
    print(f"report written to {out_path} ({len(records)} history records, {len(runs)} history runs)")
    print("heavy campaign (pair A, gpt-5.6-sol):")
    for row in heavy_rows:
        line = [f"  {LABEL[row['mode']]:<9} ok {row['ok']}/{row['requests']}"]
        for key, _, unit in HEAVY_METRICS:
            v = row[key]
            line.append(key + " " + "/".join(f"{v[p]:.2f}" if v[p] is not None else "-" for p in (50, 90, 99)))
        ot = row["out_tok"]
        line.append(f"out {ot[0]:.0f} ({ot[1]}-{ot[2]})" if ot else "out -")
        line.append(f"in {row['in_tok']:.0f}" if row["in_tok"] else "in -")
        line.append("tiers " + ",".join(f"{k}x{v}" for k, v in row["tiers"].items()))
        line.append("non200 " + (",".join(f"{k}x{v}" for k, v in row["codes"].items()) or "none"))
        print(" | ".join(line))


def conclusions(s_a_std, s_a_pri, s_a_flex, pr_pri, pr_flex, pri_served, pri_n, tps_vals, flex_ok, flex_n, flex_429,
                s_b_std, s_b_batch, b_std_run, pickup) -> str:
    items = []
    if s_a_std["p50"] and s_a_pri["p50"]:
        t_std, t_pri = pct(tps_vals.get("standard", []), 50), pct(tps_vals.get("priority", []), 50)
        items.append(
            f"<li><b>Priority</b> median latency was {fmt(s_a_pri['p50'])} vs {fmt(s_a_std['p50'])} on Standard "
            f"(median paired ratio {statistics.median(pr_pri):.2f}× over {len(pr_pri)} prompts), p90 {fmt(s_a_pri['p90'])} vs "
            f"{fmt(s_a_std['p90'])}. Median generation speed {fmt(t_pri, 'tok/s', 0)} vs {fmt(t_std, 'tok/s', 0)}. "
            f"{pri_served}/{pri_n} responses were actually served as Priority. Priority costs 2× the Standard token price "
            f"and needs no code change beyond <code>service_tier=\"priority\"</code> (or a deployment-level default).</li>"
            if pr_pri else
            f"<li><b>Priority</b> median latency {fmt(s_a_pri['p50'])} vs {fmt(s_a_std['p50'])} on Standard.</li>")
    if s_a_std["p50"] and s_a_flex["p50"]:
        items.append(
            f"<li><b>Flex</b> median latency was {fmt(s_a_flex['p50'])} vs {fmt(s_a_std['p50'])} on Standard "
            f"({f'median paired ratio {statistics.median(pr_flex):.2f}×' if pr_flex else f'{s_a_flex['p50'] / s_a_std['p50']:.2f}×'}), "
            f"p90 {fmt(s_a_flex['p90'])} vs {fmt(s_a_std['p90'])}, "
            f"max {fmt(s_a_flex['max'])} vs {fmt(s_a_std['max'])}. {flex_ok}/{flex_n} Flex requests succeeded "
            f"({flex_429} HTTP 429 responses were retried).</li>")
    items.append("<li><b>Neither Priority nor Flex needs an architectural change</b>: same deployment, same quota, same SDK call, "
                 "one extra parameter. For Flex the code must use a long timeout and handle 429 with backoff – both belong in any "
                 "robust client anyway – so it fits asynchronous, queue-driven workloads where a response in seconds-to-minutes is "
                 "acceptable. For Priority the code should read <code>service_tier</code> of the response to detect downgrades.</li>")
    if s_b_batch["p50"]:
        std_run = statistics.median(b_std_run) if b_std_run else None
        items.append(
            f"<li><b>Batch (reference)</b> job turnaround had a median of {fmt(s_b_batch['p50'])} (min {fmt(s_b_batch['min'])}, "
            f"max {fmt(s_b_batch['max'])}) versus {fmt(std_run)} for the same prompts on Standard. Batch requires a different "
            f"application design (files, job state, polling, separate GlobalBatch deployment); for the same 50 % discount Flex "
            f"keeps the synchronous API.</li>")
    ev_path = RESULTS_DIR / "billing_evidence.json"
    charged = []
    if ev_path.exists():
        charged = [r.get("Meter", "") for r in (json.loads(ev_path.read_text(encoding="utf-8")).get("cost") or {}).get("rows") or []]
    rows_ev = (json.loads(ev_path.read_text(encoding="utf-8")).get("cost") or {}).get("rows") or [] if ev_path.exists() else []
    pp_seen = sorted({m for m in charged if " PP " in f" {m} " and ("5.6" in m or "56" in m)})
    flex_seen = sorted({m for m in charged if is_flex_meter(m)})
    seen_txt = ("Meters seen in Cost Management: "
                + (f"Priority {', '.join(pp_seen)}" if pp_seen else "no PP meter yet")
                + "; " + (f"Flex {', '.join(flex_seen)}" if flex_seen else "no Flex meter yet (usage appears with a delay of up to a day)")
                + ".")

    def unit(pred):
        q = sum(r.get("UsageQuantity") or 0 for r in rows_ev if pred(r.get("Meter", "")))
        c = sum(r.get("Cost") or 0 for r in rows_ev if pred(r.get("Meter", "")))
        return c / q if q else None
    is56 = lambda m: "5.6 sol" in m or "56sol" in m
    u_std = unit(lambda m: is56(m) and " Opt Std " in f" {m} ")
    u_pp = unit(lambda m: is56(m) and " Opt PP " in f" {m} ")
    u_fl = unit(lambda m: is56(m) and " Opt " in m and is_flex_meter(m))
    implied = ""
    if u_std and u_pp and u_fl:
        implied = (f" Implied output price from the actual charges (cost ÷ quantity): Standard ${u_std:.1f}, "
                   f"Priority ${u_pp:.1f} ({u_pp / u_std:.2f}×), Flex ${u_fl:.1f} ({u_fl / u_std:.2f}×) per 1M tokens.")
    items.append("<li><b>Billing</b>: Priority is charged on dedicated PP meters (2× the Standard token price) and Flex on "
                 "dedicated Flex meters (0.5×); the public retail price API does not list the gpt-5.6-sol Flex meter yet, but "
                 "Cost Management already charges it. All tiers use the same deployment "
                 "and quota, and Azure Monitor separates the traffic with the "
                 "<code>ServiceTierRequest/ServiceTierResponse</code> dimensions. Rejected (429) Flex requests are not billed and "
                 f"downgraded Priority requests are billed as Standard. Batch has its own deployment, quota and Batch meters. "
                 f"{esc(seen_txt)}{esc(implied)}</li>")
    pk = [v for m in pickup.values() for v in m]
    if pk:
        items.append(f"<li><b>Scale to zero</b> worked for all workers: median message pickup delay {fmt(pct(pk, 50))}, "
                     f"max {fmt(max(pk))}. For Flex and Batch this overhead is negligible compared with the model latency.</li>")
    return "<ul>" + "".join(items) + "</ul>"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", action="append", help="include only runs of this campaign (repeatable)")
    ap.add_argument("--out", default=str(RESULTS_DIR / "report.html"))
    args = ap.parse_args()
    build(args.campaign, Path(args.out))


if __name__ == "__main__":
    main()

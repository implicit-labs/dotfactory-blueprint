"""Self-contained, accessible rendering for an execution waterfall."""

from __future__ import annotations

import html
import re
from typing import Any


def _text(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _anchor(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", str(value))


def _depths(items: list[dict[str, Any]]) -> dict[str, int]:
    parents = {
        str(item["span_id"]): item.get("parent_span_id")
        for item in items if item.get("span_id") and item["kind"] == "span"
    }
    result: dict[str, int] = {}

    def visit(span_id: str, seen: set[str] | None = None) -> int:
        if span_id in result:
            return result[span_id]
        seen = set(seen or ())
        if span_id in seen:
            return 0
        seen.add(span_id)
        parent = parents.get(span_id)
        result[span_id] = 0 if not parent else visit(str(parent), seen) + 1
        return result[span_id]

    for span_id in parents:
        visit(span_id)
    return result


def render_waterfall_html(
    waterfall: dict[str, Any], summary: dict[str, Any],
    error_groups: list[dict[str, Any]] | None = None,
) -> str:
    items = list(waterfall["items"])
    depths = _depths(items)
    groups = error_groups or []
    error_anchors = {
        str(item["fingerprint"]): "error-" + _anchor(item["fingerprint"])
        for item in groups
    }
    trace_anchors = {}
    rows = []
    for item in items:
        row_anchor = "trace-" + _anchor(item["record_id"])
        trace_anchors.setdefault(int(item["seq"]), row_anchor)
        error = item.get("error") or {}
        if error:
            trace_anchors[int(item["seq"])] = row_anchor
        error_code = _text(error.get("code", ""))
        if error.get("fingerprint") in error_anchors:
            error_code = (
                f'<a href="#{error_anchors[error["fingerprint"]]}">'
                f"{error_code}</a>"
            )
        duration = (
            "open" if item["kind"] == "span" and item["ended_at"] is None
            else "-" if item["duration_ms"] is None
            else f"{item['duration_ms']} ms"
        )
        span_id = str(item.get("span_id") or "")
        depth = depths.get(span_id, 0)
        if item["kind"] != "span" and item.get("parent_span_id"):
            depth = depths.get(str(item["parent_span_id"]), 0) + 1
        operation = (
            f'<span class=indent style="--depth:{depth}"></span>'
            f"{_text(item['name'])}"
        )
        rows.append(
            f'<tr id="{row_anchor}" data-status="{_text(item["status"])}">'
            f'<td data-label="Seq">{item["seq"]}</td>'
            f'<td data-label="Domain"><span class=domain>{_text(item["domain"])}</span></td>'
            f'<td data-label="Phase">{_text(item["phase"])}</td>'
            f'<td data-label="Operation">{operation}</td>'
            f'<td data-label="Status"><span class=status>{_text(item["status"])}</span></td>'
            f'<td data-label="Duration">{_text(duration)}</td>'
            f'<td data-label="Error">{error_code}</td>'
            "</tr>"
        )
    errors = []
    for item in groups:
        occurrences = "".join(
            f'<li><a href="#{trace_anchors.get(int(occurrence["trace_seq"]), "waterfall")}">'
            f'Trace sequence {occurrence["trace_seq"]}</a></li>'
            for occurrence in item["occurrences"]
        )
        reasons = ", ".join(
            str(reason) for reason in item.get("completeness", {}).get("reasons", [])
        ) or "none"
        errors.append(
            f'<details class=error id="{error_anchors[str(item["fingerprint"])]}">'
            f'<summary><strong>{_text(item["code"])}</strong> '
            f'<span class=meta>×{item["occurrence_count"]}</span></summary>'
            f'<p>{_text(item["message"])}</p>'
            '<dl class=error-facts>'
            f'<div><dt>Where</dt><dd>{_text(item["category"])}</dd></div>'
            f'<div><dt>Fingerprint</dt><dd><code>{_text(item["fingerprint"])}</code></dd></div>'
            f'<div><dt>Retryable</dt><dd>{"yes" if item["retryable"] else "no"}</dd></div>'
            f'<div><dt>Side effects ambiguous</dt><dd>{"yes" if item["ambiguous_side_effect"] else "no"}</dd></div>'
            f'<div><dt>Capture complete</dt><dd>{"yes" if item.get("capture_complete", True) else "no"}</dd></div>'
            f'<div><dt>Capture gaps</dt><dd>{_text(reasons)}</dd></div>'
            '</dl>'
            f'<p><strong>Safe next action:</strong> {_text(item["safe_remedy"])}</p>'
            f'<ul class=occurrences>{occurrences}</ul></details>'
        )
    error_html = "".join(errors) or "<p>No errors recorded.</p>"
    links = "".join(
        f'<li><a href="{_text(item["url"])}">{_text(item["kind"])}</a></li>'
        for item in summary.get("links", [])
    ) or "<li>No external evidence recorded.</li>"
    complete = bool(waterfall["completeness"]["complete"])
    warning = "" if complete else (
        '<section class=warning role=status><strong>Incomplete observation</strong>'
        f'<p>{_text(", ".join(waterfall["completeness"]["reasons"]))}</p></section>'
    )
    return """<!doctype html>
<html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#0c0d10;--panel:#15171c;--line:#2b2e36;--text:#f4f5f7;--muted:#a9afbd;--accent:#8bd5ff;--bad:#ff9b9b;--warn:#ffd479}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1200px;margin:auto;padding:32px}} h1{{font-size:24px;margin:0 0 8px}} a{{color:var(--accent)}} .meta,small{{color:var(--muted)}}
.facts{{display:flex;gap:24px;flex-wrap:wrap;margin:20px 0}} .fact{{background:var(--panel);padding:12px 16px;border:1px solid var(--line);border-radius:10px}}
.table-wrap{{border:1px solid var(--line);border-radius:10px}} table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}} th{{position:sticky;top:0;background:var(--panel)}}
.indent{{display:inline-block;width:calc(var(--depth) * 18px)}} .domain{{color:var(--accent)}}
.error{{border-left:3px solid var(--bad);padding:12px 16px;margin:16px 0;background:var(--panel)}} summary{{cursor:pointer}}
.error-facts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}} .error-facts div{{min-width:0}} dt{{color:var(--muted)}} dd{{margin:2px 0;overflow-wrap:anywhere}}
.warning{{border:1px solid var(--warn);padding:12px 16px;border-radius:10px;color:var(--warn)}}
.sr-only{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
@media(max-width:640px){{main{{padding:18px}}.facts{{gap:8px}}.fact{{flex:1 1 44%}}.table-wrap{{border:0}}table,tbody,tr,td{{display:block;width:100%}}thead{{display:none}}tr{{margin:0 0 12px;border:1px solid var(--line);border-radius:10px;background:var(--panel);overflow:hidden}}td{{display:grid;grid-template-columns:88px minmax(0,1fr);gap:8px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}td::before{{content:attr(data-label);color:var(--muted)}}.indent{{width:calc(var(--depth) * 10px)}}code{{overflow-wrap:anywhere}}}}
</style>
<main><h1>{title}</h1><p class=meta>Trace {trace_id} · sequence {from_seq}-{through_seq}</p>
{warning}<section class=facts aria-label="Run facts"><div class=fact><strong>State</strong><br>{state}</div>
<div class=fact><strong>Records</strong><br>{count}</div><div class=fact><strong>Ordering</strong><br>{ordering}</div><div class=fact><strong>Capture</strong><br>{capture}</div></section>
<h2 id=waterfall>Waterfall</h2><div class=table-wrap><table><caption class=sr-only>Execution hierarchy and timing</caption><thead><tr><th>Seq</th><th>Domain</th><th>Phase</th><th>Operation</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead>
<tbody>{rows}</tbody></table></div><h2>Errors</h2>{errors}<h2>Evidence</h2><ul>{links}</ul></main></html>""".format(
        title=_text(summary["headline"]), trace_id=_text(waterfall["trace_id"]),
        from_seq=waterfall["from_trace_seq"], through_seq=waterfall["through_trace_seq"],
        state=_text(summary["current_state"]), count=waterfall["record_count"],
        ordering=_text(waterfall["ordering_quality"]),
        capture="complete" if complete else "incomplete", rows="".join(rows),
        errors=error_html, links=links, warning=warning,
    )

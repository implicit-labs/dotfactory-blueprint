"""Self-contained, accessible rendering for an execution waterfall."""

from __future__ import annotations

import html
from typing import Any


def _text(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_waterfall_html(
    waterfall: dict[str, Any], summary: dict[str, Any],
) -> str:
    rows = []
    for item in waterfall["items"]:
        error = item.get("error") or {}
        duration = (
            "open" if item["kind"] == "span" and item["ended_at"] is None
            else "-" if item["duration_ms"] is None
            else f"{item['duration_ms']} ms"
        )
        rows.append(
            "<tr>"
            f"<td>{item['seq']}</td>"
            f"<td><span class=domain>{_text(item['domain'])}</span></td>"
            f"<td>{_text(item['phase'])}</td>"
            f"<td>{_text(item['name'])}</td>"
            f"<td><span class=status>{_text(item['status'])}</span></td>"
            f"<td>{_text(duration)}</td>"
            f"<td>{_text(error.get('code', ''))}</td>"
            "</tr>"
        )
    errors = []
    for item in summary["errors"]:
        errors.append(
            "<article class=error>"
            f"<h3>{_text(item['code'])} <small>x{item['occurrence_count']}</small></h3>"
            f"<p>{_text(item['message'])}</p>"
            f"<p><strong>Next:</strong> {_text(item['safe_remedy'])}</p>"
            f"<p class=meta>Trace {item['first_trace_seq']} to {item['last_trace_seq']}</p>"
            "</article>"
        )
    error_html = "".join(errors) or "<p>No errors recorded.</p>"
    return """<!doctype html>
<html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#0c0d10;--panel:#15171c;--line:#2b2e36;--text:#f4f5f7;--muted:#a9afbd;--accent:#8bd5ff;--bad:#ff9b9b}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1200px;margin:auto;padding:32px}} h1{{font-size:24px;margin:0 0 8px}} .meta,small{{color:var(--muted)}}
.facts{{display:flex;gap:24px;flex-wrap:wrap;margin:20px 0}} .fact{{background:var(--panel);padding:12px 16px;border:1px solid var(--line);border-radius:10px}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:10px}} table{{width:100%;border-collapse:collapse;min-width:760px}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}} th{{position:sticky;top:0;background:var(--panel)}}
.domain{{color:var(--accent)}} .error{{border-left:3px solid var(--bad);padding:2px 16px;margin:16px 0;background:var(--panel)}}
@media(max-width:640px){{main{{padding:18px}}.facts{{gap:8px}}}}
</style>
<main><h1>{title}</h1><p class=meta>Trace {trace_id} · sequence {from_seq}-{through_seq}</p>
<section class=facts aria-label="Run facts"><div class=fact><strong>State</strong><br>{state}</div>
<div class=fact><strong>Records</strong><br>{count}</div><div class=fact><strong>Ordering</strong><br>{ordering}</div></section>
<h2>Waterfall</h2><div class=table-wrap><table><thead><tr><th>Seq</th><th>Domain</th><th>Phase</th><th>Operation</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead>
<tbody>{rows}</tbody></table></div><h2>Errors</h2>{errors}</main></html>""".format(
        title=_text(summary["headline"]), trace_id=_text(waterfall["trace_id"]),
        from_seq=waterfall["from_trace_seq"], through_seq=waterfall["through_trace_seq"],
        state=_text(summary["current_state"]), count=waterfall["record_count"],
        ordering=_text(waterfall["ordering_quality"]), rows="".join(rows),
        errors=error_html,
    )

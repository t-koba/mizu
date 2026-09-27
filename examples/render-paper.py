#!/usr/bin/env python3
"""Render the latest published artifact as a static HTML paper.

OPERATOR-RUN PRESENTATION POLICY. This script is never executed by harness
roles: run it explicitly after `mizu report`, then distribute the HTML through
an access-controlled channel (never a public web server). Model-authored
Markdown is escaped; only recorded evidence and the stylesheet below shape
the page. Usage:

    python3 examples/render-paper.py /path/to/state/mizu/projects/DEMO

Inputs (all already recorded by `mizu report`, i.e. src/mizu/report.py):
`artifacts/latest.json` -> artifact pointer; `artifacts/<id>/artifact.md` ->
staged document; `artifacts/<id>/evidence.json` -> snapshot, verification,
skipped paths. Output `index.html` is written beside the entry and is
deliberately outside the artifact digest: a disposable local render.
"""
import html
import json
import sys
from pathlib import Path

STYLE = """
:root { color-scheme: light dark; font-family: ui-serif, Georgia, serif; }
body { max-width: 1080px; margin: auto; padding: clamp(18px,4vw,48px); line-height:1.65; }
header { border-top:4px solid currentColor; border-bottom:1px solid; margin-bottom:24px; }
.brand { font:700 14px ui-monospace,monospace; letter-spacing:.24em; margin-top:12px; }
h1 { font-size:clamp(28px,5vw,48px); line-height:1.2; margin:.5em 0; }
h2 { font-size:22px; line-height:1.3; } h3 { font-size:17px; }
small, .meta { opacity:.72; font:12px/1.5 ui-monospace,monospace; }
main { min-width:0; border-top:1px solid; padding-top:12px; }
p,pre,code { overflow-wrap:anywhere; white-space:pre-wrap; }
pre { font:12px/1.6 ui-monospace,monospace; padding:12px; border:1px solid; }
footer { margin-top:32px; border-top:1px solid; }
@media print { body { max-width:none; padding:0; } }
"""


def main(root):
    project = Path(root)
    latest = json.loads((project / "artifacts/latest.json").read_text())
    entry = project / "artifacts" / latest["artifact"]
    markdown = (entry / "artifact.md").read_text()
    evidence = json.loads((entry / "evidence.json").read_text())
    escaped = html.escape
    lines = markdown.splitlines()
    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else "Project artifact"
    body = "\n".join(lines[1:]).strip()
    verification = (evidence.get("verification") or {})
    tested = "not verified" if not verification else ("passed" if verification.get("passed") else "failed / changed during verification")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Mizu &middot; {escaped(title)}</title><style>{STYLE}</style></head><body>
<header><p class="brand">MIZU / PROJECT JOURNAL</p><h1>{escaped(title)}</h1>
<p class="meta">Snapshot {escaped(str(evidence.get('snapshot')))}<br>As of {escaped(str(evidence.get('published_at')))}</p></header>
<p class="meta">Verification: {escaped(tested)}. The prose below is agent-authored commentary, not independent proof.</p>
<main><pre>{escaped(body)}</pre></main><section><h2>Recorded evidence</h2>
<pre>{escaped(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre></section>
<footer><p class="meta">Mizu &middot; Static artifact &middot; No scripts, tracking, remote fonts or external assets.</p></footer>
</body></html>"""
    out = entry / "index.html"
    out.write_text(document)
    print(str(out))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: render-paper.py PROJECT_ROOT")
    main(sys.argv[1])

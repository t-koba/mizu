#!/usr/bin/env python3
"""View the latest published dashboard payload as bare escaped text in one file.

MECHANICAL VIEWER, NOT A DASHBOARD DESIGN. Run it explicitly after
`mizu dashboard`; it is never executed by harness roles. Real dashboards
are built per project from zero against `dashboard/latest.json` (and
`mizu usage` for token facts) — this file exists only so an operator can
eyeball the raw facts in any browser. It makes no presentation choices:
the payload is dumped as escaped text. Single file, inline style,
restrictive CSP, no scripts, no external assets. Usage:

    mizu dashboard demo
    python3 examples/render-dashboard.py /path/to/state/mizu/projects/demo
"""
import html
import json
import sys
from pathlib import Path

STYLE = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { max-width: 760px; margin: auto; padding: clamp(14px,4vw,32px); line-height:1.6; }
pre { overflow-wrap:anywhere; white-space:pre-wrap; font:12px/1.6 ui-monospace,monospace; }
.meta { opacity:.72; font:12px/1.5 ui-monospace,monospace; }
"""


def main(root):
    project = Path(root)
    latest = json.loads((project / "dashboard/latest.json").read_text())
    payload = json.loads((project / "dashboard" / (latest["dashboard"] + ".json")).read_text())
    name = html.escape(str(payload.get("project", "unknown")))
    body = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Mizu dashboard &middot; {name}</title><style>{STYLE}</style></head><body>
<p class="meta">Mizu &middot; raw dashboard facts &middot; design your own per project.</p>
<pre>{body}</pre>
</body></html>"""
    out = project / "dashboard" / "index.html"
    out.write_text(document)
    print(str(out))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: render-dashboard.py PROJECT_ROOT")
    main(sys.argv[1])

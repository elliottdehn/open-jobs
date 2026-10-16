"""FastAPI application factory for toolsHub.

Wires together all routers.

The search page carries its CSS and JS inline rather than linking separate
assets. That is required, not incidental: the `html` command writes
work/search.html as a single self-contained file, matching what
tools/jobs.py produces, and that artifact has to open from disk with no
server. Splitting the assets out would break both file:// and parity with
tools/, so there is nothing for a /static mount to serve.

Routes:
  GET  /              → page.py     (rendered search page, cache-backed)
  POST /event         → events.py   (append label to interactions.jsonl)
  POST /enrich        → enrich.py   (proxy to backend /enrich)
  GET  /enrich/budget → enrich.py   (proxy to backend /enrich/budget)
"""
from fastapi import FastAPI

from app.routers import enrich, events, page

app = FastAPI(title="toolsHub", docs_url=None, redoc_url=None)

# Routers
app.include_router(page.router)
app.include_router(events.router)
app.include_router(enrich.router)

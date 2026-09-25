"""FastAPI application factory for toolsHub.

Wires together all routers and serves static files.

Routes:
  GET  /              → page.py     (rendered search page, cache-backed)
  POST /event         → events.py   (append label to interactions.jsonl)
  POST /enrich        → enrich.py   (proxy to backend /enrich)
  GET  /enrich/budget → enrich.py   (proxy to backend /enrich/budget)
  GET  /static/*      → StaticFiles (style.css, worker.js, app.js)
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import enrich, events, page

_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="toolsHub", docs_url=None, redoc_url=None)

# Static assets (CSS, worker, JS)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# Routers
app.include_router(page.router)
app.include_router(events.router)
app.include_router(enrich.router)

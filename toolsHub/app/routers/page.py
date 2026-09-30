"""GET / — serve the rendered search page.

Reads the payload from cache (rebuilding if jobs.parquet or
interactions.jsonl have changed) and renders search.html via
render_payload().  All blocking I/O runs off the event loop via
asyncio.to_thread().
"""
import asyncio

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.cache import get_payload
from app.services.html import render_payload

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    payload = await asyncio.to_thread(get_payload)
    html = await asyncio.to_thread(render_payload, payload)
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})

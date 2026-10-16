"""Enrich proxy routes — forwards /enrich and /enrich/budget to the backend worker.

The browser-side code calls OPEN_JOBS_API/enrich directly by default, so these
routes are only exercised when the client is pointed at the local server
(e.g. OPEN_JOBS_API not set and window.location is localhost).  They allow the
FastAPI server to act as a transparent pass-through, which avoids CORS issues
and lets the CLI --enrich command and the browser share one code path.
"""
import asyncio
import json
import urllib.error
import urllib.request

from fastapi import APIRouter, Request, Response

from app.config import BASE, UA

router = APIRouter()


def _post_backend(body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"{BASE}/enrich",
        data=body,
        headers={**UA, "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get_budget() -> tuple[int, bytes]:
    req = urllib.request.Request(f"{BASE}/enrich/budget", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@router.post("/enrich")
async def proxy_enrich(request: Request) -> Response:
    body = await request.body()
    status, data = await asyncio.to_thread(_post_backend, body)
    return Response(content=data, status_code=status, media_type="application/json")


@router.get("/enrich/budget")
async def proxy_budget() -> Response:
    status, data = await asyncio.to_thread(_get_budget)
    return Response(content=data, status_code=status, media_type="application/json")

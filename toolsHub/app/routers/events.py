"""POST /event — append to interactions.jsonl, invalidate cache, trigger expand.

Mirrors the do_POST handler in tools/jobs.py::cmd_serve():
  - Reads the raw JSON body.
  - Appends it (with a trailing newline) to work/interactions.jsonl.
  - Invalidates the payload cache so the next page load reflects the new label.
  - If the event is type=label with value=1, kicks off the background expand
    thread via expand.trigger().
"""
import json

from fastapi import APIRouter, Request, Response

from app.cache import invalidate
from app.config import WORK

router = APIRouter()

_INTERACTIONS = WORK / "interactions.jsonl"


@router.post("/event", status_code=204)
async def post_event(request: Request) -> Response:
    body = await request.body()
    if not body:
        return Response(status_code=204)

    # Append to interactions.jsonl (same as tools/jobs.py)
    with _INTERACTIONS.open("ab") as f:
        f.write(body.rstrip(b"\n") + b"\n")

    # Invalidate payload cache so the next page load picks up the new label
    invalidate()

    # Trigger background group expansion on yes-labels
    try:
        e = json.loads(body)
    except Exception:
        e = {}
    if e.get("type") == "label" and e.get("value") == 1 and e.get("key"):
        from app.services.expand import trigger
        trigger(e["key"])

    return Response(status_code=204)

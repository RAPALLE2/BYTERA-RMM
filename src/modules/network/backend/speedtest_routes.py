# routers/speedtest_routes.py
# ---------------------------
# Endpoints for the speed test app in the frontend (Ookla-like):
#   GET  /api/speedtest/ping      -> mini response for latency/jitter measurement
#   GET  /api/speedtest/download  -> streams random data (?mb=..., max 64)
#   POST /api/speedtest/upload    -> accepts a body, only counts bytes
#
# Measured is browser <-> RMM server. All endpoints require login (bearer token
# as everywhere) and send no-store, so that neither browser nor proxies cache
# the test data and distort the result.

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from app.auth import get_current_user

router = APIRouter()

_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}

_CHUNK = 1024 * 1024          # 1 MiB Bloecke
_MAX_MB = 64                  # Obergrenze pro Download-Request (Client loopt)


@router.get("/api/speedtest/ping")
def speedtest_ping(user: dict = Depends(get_current_user)):
    # Deliberately tiny: the response time should be almost only network latency.
    return Response(content="pong", media_type="text/plain", headers=_NO_STORE)


@router.get("/api/speedtest/download")
async def speedtest_download(mb: int = 16, user: dict = Depends(get_current_user)):
    mb = max(1, min(int(mb), _MAX_MB))

    def gen():
        # os.urandom instead of zeros: incompressible, so transparent
        # compression (proxy/VPN) does not flatter the measurement.
        block = os.urandom(_CHUNK)
        for _ in range(mb):
            yield block

    headers = dict(_NO_STORE)
    headers["Content-Length"] = str(mb * _CHUNK)
    headers["Content-Type"] = "application/octet-stream"
    return StreamingResponse(gen(), headers=headers)


@router.post("/api/speedtest/upload")
async def speedtest_upload(request: Request, user: dict = Depends(get_current_user)):
    # Body nur zaehlen, nie speichern.
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
    return Response(content=str(received), media_type="text/plain", headers=_NO_STORE)

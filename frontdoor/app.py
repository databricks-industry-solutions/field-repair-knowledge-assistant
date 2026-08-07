"""FastAPI entry point for the FIS AI Knowledge Agent front door.

Mirrors app/app.py (04.1): mounts the /api router, exposes /api/health, and serves
the built React SPA (frontend/dist) with an api-prefix-guarded catch-all so deep
links resolve to index.html without shadowing the API.
"""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

try:
    # Deployed: uvicorn `app:app` with cwd=frontdoor/ (mirrors the 04.1 app).
    from server.routes import chat
except ModuleNotFoundError:
    # Imported as a package (e.g. tests: `from frontdoor.app import app`).
    from frontdoor.server.routes import chat

app = FastAPI(title="FIS AI Knowledge Agent")

app.include_router(chat.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built React frontend (frontend/dist) if present.
_frontend = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(_frontend):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            return {"error": "not found"}
        return FileResponse(os.path.join(_frontend, "index.html"))

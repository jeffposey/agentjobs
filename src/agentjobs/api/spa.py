"""Serve the built React single-page application without shadowing the Jinja UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def default_frontend_dist() -> Path:
    """Return the source-checkout build directory used until wheel packaging lands."""
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def register_spa(app: FastAPI, dist_dir: Optional[Path] = None) -> None:
    """Mount hashed assets and return the SPA shell for every other `/app` path.

    The asset mount is registered before the catch-all. This keeps a request for a
    hashed JavaScript or CSS file from receiving ``index.html``, while a browser hard
    refresh at any client-side route still receives the shell React needs to start.
    """
    dist = (dist_dir or default_frontend_dist()).resolve()
    app.mount(
        "/app/assets",
        StaticFiles(directory=dist / "assets", check_dir=False),
        name="react-assets",
    )

    async def shell() -> FileResponse:
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="React frontend is not built; run `npm run build` in frontend/.",
            )
        return FileResponse(index)

    app.add_api_route("/app", shell, methods=["GET"], include_in_schema=False)
    app.add_api_route("/app/{path:path}", shell, methods=["GET"], include_in_schema=False)

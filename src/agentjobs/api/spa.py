"""Serve the built React single-page application without shadowing the Jinja UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def default_frontend_dist() -> Path:
    """Return the bundle stored inside the importable AgentJobs package."""
    return Path(__file__).resolve().parents[1] / "frontend_dist"


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
    app.mount(
        "/app/icons",
        StaticFiles(directory=dist / "icons", check_dir=False),
        name="react-icons",
    )

    async def manifest() -> FileResponse:
        path = dist / "manifest.webmanifest"
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "React PWA manifest is missing from the package; run `npm run build` "
                    "in frontend/ for local development or build a release wheel."
                ),
            )
        return FileResponse(
            path,
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    async def service_worker() -> FileResponse:
        path = dist / "sw.js"
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "React service worker is missing from the package; run `npm run build` "
                    "in frontend/ for local development or build a release wheel."
                ),
            )
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/app/",
            },
        )

    app.add_api_route(
        "/app/manifest.webmanifest", manifest, methods=["GET"], include_in_schema=False
    )
    app.add_api_route("/app/sw.js", service_worker, methods=["GET"], include_in_schema=False)

    async def shell() -> FileResponse:
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "React frontend bundle is missing from the package; run `npm run build` "
                    "in frontend/ for local development or build a release wheel."
                ),
            )
        return FileResponse(index)

    app.add_api_route("/app", shell, methods=["GET"], include_in_schema=False)
    app.add_api_route("/app/{path:path}", shell, methods=["GET"], include_in_schema=False)

from pathlib import Path

import httpx
import pytest

from research_agent.sources.api import create_app


@pytest.mark.anyio
async def test_source_api_upload_read_and_project_boundary(tmp_path: Path) -> None:
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/projects/project-a/sources", files={"file": ("brief.txt", b"API evidence")})
        assert response.status_code == 200
        payload = response.json()
        source_id = payload["source"]["source_id"]
        listed = await client.get("/api/projects/project-a/sources")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["source_id"] == source_id
        denied = await client.get(f"/api/projects/project-b/sources/{source_id}")
        assert denied.status_code == 404


@pytest.mark.anyio
async def test_batch_upload_rejects_traversal_without_path_access(tmp_path: Path) -> None:
    app = create_app(tmp_path / "sources")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/projects/project-a/source-batches", files=[("files", ("../secret.txt", b"no"))])
        assert response.status_code == 200
        assert response.json()["items"][0]["error"]

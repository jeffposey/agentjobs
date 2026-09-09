"""The identifier that says a bundle and a server were built from the same contract.

The incident behind these, on 2026-08-17: a server process started at 20:37, the
``related`` field was added to the task detail response at 21:30, and the frontend
bundle was rebuilt at 23:25 against a regenerated client where that field is a required
array. The running process kept answering the older shape with a 200. The client trusted
its own generated types, read ``.length`` off a missing array, and the page went blank.

The trap these tests exist to keep anyone from falling into is that the two identifiers
``/api/version`` already carried -- the package version and the task-record schema
version -- **were identical on both sides throughout**. Any check that compares them
passes its own tests and still misses this, which is why the reproduction below fixes
both and moves only the contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentjobs.api.contract import canonical_document, contract_digest, live_contract_digest
from agentjobs.api.main import app
from agentjobs.api.spa import bundle_id

ROOT = Path(__file__).resolve().parents[1]


def _detail_response_schema(document: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = document["components"]["schemas"]["TaskDetailResponse"]
    return schema


class TestTheDigestTracksTheContract:
    def test_the_same_document_always_digests_the_same(self) -> None:
        schema = app.openapi()

        assert contract_digest(schema) == contract_digest(json.loads(json.dumps(schema)))

    def test_the_incident_moves_it_while_version_and_schema_version_do_not(self) -> None:
        """Reproduces 2026-08-17 in the one dimension that actually differed."""
        before = app.openapi()
        after = json.loads(json.dumps(before))
        properties = _detail_response_schema(after)["properties"]
        properties.pop("related", None)

        # Exactly what a comparison of the old two identifiers would have seen.
        assert after["info"]["version"] == before["info"]["version"]
        assert contract_digest(after) != contract_digest(before)

    def test_a_change_that_leaves_the_document_alone_leaves_it_alone(self) -> None:
        """The property that makes the warning worth believing.

        A digest that moved on every commit would be a warning nobody reads, which is
        why the git commit was rejected as the mechanism. Re-rendering the same
        application must produce the same answer.
        """
        assert contract_digest(app.openapi()) == contract_digest(app.openapi())

    def test_it_is_computed_from_the_process_rather_than_from_a_file(self) -> None:
        """A stale file on disk cannot make a running server claim to be current."""
        other = FastAPI(title="something else")

        assert live_contract_digest(other) != live_contract_digest(app)

    def test_the_checked_in_document_is_what_the_application_serves(self) -> None:
        """The bundle's digest is stamped from this file, so it has to be current.

        `scripts/check.py`'s `api` stage enforces this too; asserting it here as well
        means the pytest stage names the cause rather than leaving a frontend banner to
        report a mismatch nobody introduced on purpose.
        """
        on_disk = (ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8")

        assert contract_digest(app.openapi()) == contract_digest(json.loads(on_disk))
        assert canonical_document(app.openapi()) == on_disk


class TestTheVersionEndpointReportsIt:
    def test_it_answers_the_digest_of_the_contract_it_is_serving(self) -> None:
        with TestClient(app) as client:
            body = client.get("/api/version").json()

        assert body["api_digest"] == contract_digest(app.openapi())

    def test_the_bundle_the_frontend_compiled_in_is_the_same_digest(self) -> None:
        """The two halves of the comparison, checked against each other.

        If the generator and the server ever computed this differently, every install
        would show a permanent skew banner and the feature would be turned off within a
        day. This is the assertion that fails first instead.
        """
        module = (ROOT / "frontend" / "src" / "api" / "apiDigest.ts").read_text(encoding="utf-8")
        stamped = module.rsplit('"', 2)[1]

        assert stamped == contract_digest(app.openapi())


class TestTheServedBundleIsReported:
    def test_it_reads_the_id_the_build_wrote(self, tmp_path: Path) -> None:
        (tmp_path / "build-info.json").write_text('{"bundle_id": "abc123"}', encoding="utf-8")

        assert bundle_id(tmp_path) == "abc123"

    def test_a_missing_or_unreadable_file_is_not_an_error(self, tmp_path: Path) -> None:
        """A clone that has never built, or a bundle built before this existed.

        Reported as "unknown" rather than as a fault, because the frontend treats
        unknown as "say nothing" -- and a detector that fails loudly when it cannot
        determine skew is worse than no detector.
        """
        assert bundle_id(tmp_path) is None

        (tmp_path / "build-info.json").write_text("not json", encoding="utf-8")
        assert bundle_id(tmp_path) is None

        (tmp_path / "build-info.json").write_text("{}", encoding="utf-8")
        assert bundle_id(tmp_path) is None

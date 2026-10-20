import json

import httpx
import pytest
from sqlalchemy import select
from test_ingest import pdf, ws  # noqa: F401

from food_record_digitizer.db import Candidate, ExtractionRun
from food_record_digitizer.extraction import extract_record
from food_record_digitizer.ingest import ingest_batch, ingest_one
from food_record_digitizer.provider import OpenAIProvider, ProviderError, strict_schema
from food_record_digitizer.schemas import Metadata, RecordContent, Value
from food_record_digitizer.validation import validate_content


def sample():
    ev = {"source_record_id": "R001", "page_number": 1, "region": "row 1", "transcription_raw": "2 eggs"}
    return {
        "record_id": "R001",
        "subject": {"raw": "S1", "normalized": "S1", "evidence": [ev]},
        "days": [{"day_id": "d1"}],
        "events": [{"event_id": "e1", "day_id": "d1"}],
        "items": [
            {
                "item_id": "i1",
                "event_id": "e1",
                "evidence": [ev],
                "description": {"raw": "eggs", "normalized": "eggs", "evidence": [ev]},
            }
        ],
        "pages": [
            {"source_record_id": "R001", "page_number": 1, "classification": "food_record", "reviewed": True}
        ],
    }


class FakeProvider:
    model = "test-model"
    base_url = "https://example.invalid/v1"
    max_output_tokens = 1000
    calls = 0
    hook = None
    failure = None

    def extract(self, record_id, pages):
        self.calls += 1
        if self.hook:
            self.hook()
        if self.failure:
            raise ProviderError(self.failure)
        data = sample()
        return {
            "content": data,
            "raw": {"synthetic": True},
            "usage": {"input_tokens": 10},
            "response_id": "test",
        }


@pytest.fixture
def ready(ws, tmp_path):  # noqa: F811
    ingest_one(
        ws.factory,
        ws.store,
        ws.project_id,
        pdf(tmp_path / "R001.pdf"),
        Metadata(record_id="R001", subject_id="S1"),
    )
    return ws


def test_extraction_cached_and_model_cannot_mark_reviewed(ready):
    provider = FakeProvider()
    first = extract_record(ready, "R001", provider)
    second = extract_record(ready, "R001", provider)
    assert first["status"] == "succeeded"
    assert second["status"] == "cached"
    assert provider.calls == 1
    with ready.factory() as session:
        candidate = session.get(Candidate, first["candidate_id"])
        assert not candidate.content["pages"][0]["reviewed"]
        assert ready.records()[0].state == "pending_review"
        assert ready.records()[0].approved_id is None


def test_failed_call_persists_and_retry_can_succeed(ready):
    provider = FakeProvider()
    provider.failure = "request_timeout_outcome_unknown"
    result = extract_record(ready, "R001", provider)
    assert result["status"] == "failed"
    assert ready.records()[0].active_run_id is None
    provider.failure = None
    assert extract_record(ready, "R001", provider)["status"] == "succeeded"
    with ready.factory() as session:
        assert len(list(session.scalars(select(ExtractionRun)))) == 2


def test_input_changed_during_call_cannot_replace_latest(ready, tmp_path):
    provider = FakeProvider()
    provider.hook = lambda: ingest_batch(
        ready.factory, ready.store, ready.project_id, [pdf(tmp_path / "R001-A1.pdf")], []
    )
    result = extract_record(ready, "R001", provider)
    assert result["status"] == "stale"
    assert ready.records()[0].input_version == 2
    assert ready.records()[0].state == "pending_extraction"
    with ready.factory() as session:
        assert session.scalar(select(Candidate)) is None


def test_page_limit_prevents_provider_call(ready):
    provider = FakeProvider()
    result = extract_record(ready, "R001", provider, max_pages=0)
    assert result["error"] == "record_page_limit_exceeded"
    assert provider.calls == 0


def test_missing_unknown_reason_and_foreign_keys_are_rejected():
    with pytest.raises(ValueError):
        Value()
    with pytest.raises(ValueError):
        Value(normalized="2")
    assert Value(missing_reason="illegible").normalized is None
    data = sample()
    data["items"][0]["event_id"] = "missing"
    with pytest.raises(ValueError):
        RecordContent.model_validate(data)


def test_evidence_and_page_coverage_are_checked():
    data = RecordContent.model_validate(sample())
    snapshot = {
        "documents": {"R001": {"role": "main", "page_count": 2}},
        "metadata": {"R001": {"subject_id": "S2"}},
    }
    codes = {i.code for i in validate_content(data, snapshot)}
    assert {"subject_conflict", "page_coverage_mismatch"} <= codes


def test_adapter_sends_images_and_strict_schema_without_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")

    def handle(request):
        assert request.headers["authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["input"][0]["content"][2]["type"] == "input_image"
        assert body["input"][0]["content"][2]["image_url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "id": "r1",
                "usage": {},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample())}]}
                ],
            },
        )

    path = tmp_path / "image.png"
    from PIL import Image

    Image.effect_noise((1800, 2400), 30).convert("RGB").save(path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = OpenAIProvider("test", client=client)
        result = provider.extract(
            "R001", [{"source_record_id": "R001", "page_number": 1, "role": "main", "path": path}]
        )
    assert result["content"]["record_id"] == "R001"
    schema = strict_schema()
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize(
    "body,code",
    [
        ({"status": "incomplete"}, "incomplete_response"),
        (
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
            "model_refusal",
        ),
    ],
)
def test_adapter_refusal_and_truncation_are_failures(monkeypatch, body, code):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
        with pytest.raises(ProviderError, match=code):
            OpenAIProvider("test", client=client).extract("R001", [])


def test_schema_error_retains_raw_response_without_candidate(ready):
    class Invalid(FakeProvider):
        def extract(self, record_id, pages):
            return {"content": {"wrong": "shape"}, "raw": {"invalid_fixture": True}, "usage": {}}

    result = extract_record(ready, "R001", Invalid())
    assert result["error"] == "schema_validation_failed"
    assert (ready.path / "runs" / result["run_id"] / "response.json").exists()
    with ready.factory() as session:
        assert session.scalar(select(Candidate)) is None


def test_ten_record_pipeline_has_no_duplicate_candidates(ws, tmp_path):  # noqa: F811
    class FixtureProvider(FakeProvider):
        def extract(self, record_id, pages):
            self.calls += 1
            payload = json.loads(json.dumps(sample()).replace("R001", record_id))
            return {"content": payload, "raw": {"synthetic": True}, "usage": {}}

    paths = [pdf(tmp_path / f"R{i:03d}.pdf") for i in range(1, 11)]
    rows = [Metadata(record_id=p.stem, subject_id="S1") for p in paths]
    ingest_batch(ws.factory, ws.store, ws.project_id, paths, rows)
    provider = FixtureProvider()
    for path in paths:
        assert extract_record(ws, path.stem, provider)["status"] == "succeeded"
    for path in paths:
        assert extract_record(ws, path.stem, provider)["status"] == "cached"
    assert provider.calls == 10
    with ws.factory() as session:
        assert len(list(session.scalars(select(Candidate)))) == 10


def test_operator_recovery_releases_abandoned_claim(ready):
    from food_record_digitizer.db import Record
    from food_record_digitizer.extraction import recover_run

    with ready.factory.begin() as session:
        record = session.scalar(select(Record))
        run = ExtractionRun(
            record_pk=record.id,
            input_version=record.input_version,
            cache_key="test",
            configuration={},
            status="running",
        )
        session.add(run)
        session.flush()
        record.active_run_id = run.id
        run_id = run.id
    assert extract_record(ready, "R001", FakeProvider())["status"] == "busy"
    recover_run(ready, run_id)
    assert ready.records()[0].active_run_id is None
    assert extract_record(ready, "R001", FakeProvider())["status"] == "succeeded"


def test_auth_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-print-this")
    response = httpx.Response(401, json={"error": {"message": "do-not-print-this"}})
    with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as client:
        with pytest.raises(ProviderError) as raised:
            OpenAIProvider("test", client=client).extract("R001", [])
        assert str(raised.value) == "api_http_401"
        assert raised.value.response is None


def test_reverting_metadata_cannot_return_a_candidate_from_an_old_revision(ready):
    provider = FakeProvider()
    first = extract_record(ready, "R001", provider)
    for subject in ("S2", "S1"):
        ingest_batch(
            ready.factory, ready.store, ready.project_id, [], [Metadata(record_id="R001", subject_id=subject)]
        )
    result = extract_record(ready, "R001", provider)
    with ready.factory() as session:
        current = session.get(Candidate, result["candidate_id"])
        original = session.get(Candidate, first["candidate_id"])
        assert current.input_version == ready.records()[0].input_version == 3
        assert original.input_version == 1
        assert current.id != original.id


def test_corrupted_source_stops_extraction_before_a_provider_call(ready):
    from food_record_digitizer.db import Record
    from food_record_digitizer.ingest import current_snapshot

    with ready.factory() as session:
        snapshot = current_snapshot(session, session.scalar(select(Record)))
    sha = snapshot["documents"]["R001"]["sha256"]
    ready.store.path(sha).write_bytes(b"altered source")
    provider = FakeProvider()
    result = extract_record(ready, "R001", provider)
    assert result["status"] == "failed"
    assert provider.calls == 0


def test_missing_attachment_scan_is_an_explicit_draft_issue(ready):
    ingest_batch(
        ready.factory, ready.store, ready.project_id, [], [Metadata(record_id="R001-A1", subject_id="S1")]
    )
    result = extract_record(ready, "R001", FakeProvider())
    with ready.factory() as session:
        candidate = session.get(Candidate, result["candidate_id"])
        assert any(
            issue["code"] == "missing_source" and issue["target"] == "R001-A1"
            for issue in candidate.content["issues"]
        )


def test_force_then_normal_run_returns_latest_candidate(ready):
    provider = FakeProvider()
    first = extract_record(ready, "R001", provider)
    second = extract_record(ready, "R001", provider, force=True)
    cached = extract_record(ready, "R001", provider)
    assert first["candidate_id"] != second["candidate_id"]
    assert cached["candidate_id"] == second["candidate_id"]
    assert provider.calls == 2


def test_regional_host_error_is_actionable_without_echoing_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-key")
    response = httpx.Response(
        401,
        json={
            "error": {
                "code": "incorrect_hostname",
                "message": "Please make your request to us.api.openai.com; key test-private-key",
            }
        },
    )
    with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as client:
        with pytest.raises(ProviderError) as raised:
            OpenAIProvider("test", client=client).extract("R001", [])
    error = raised.value
    assert str(error) == "api_http_401"
    assert error.diagnostic["provider_code"] == "incorrect_hostname"
    assert error.diagnostic["required_base_url"] == "https://us.api.openai.com/v1"
    assert "test-private-key" not in json.dumps(error.diagnostic)
    assert error.response is None


def test_unknown_provider_error_never_echoes_raw_message():
    from food_record_digitizer.provider import safe_error_diagnostic

    result = safe_error_diagnostic(
        httpx.Response(
            401,
            json={
                "error": {
                    "code": "sk-secret-value",
                    "message": "Some private message",
                }
            },
        )
    )
    assert result == {}


def test_compression_settings_version_candidates_and_preserve_cache(ready):
    provider = FakeProvider()
    provider.image_max_edge = 1600
    provider.jpeg_quality = 85
    first = extract_record(ready, "R001", provider)
    assert extract_record(ready, "R001", provider)["status"] == "cached"
    provider.image_max_edge = 2000
    second = extract_record(ready, "R001", provider)
    assert second["status"] == "succeeded"
    assert first["candidate_id"] != second["candidate_id"]
    assert provider.calls == 2
    with ready.factory() as session:
        old = session.get(Candidate, first["candidate_id"])
        assert old.provenance["image_preprocessing"]["max_edge"] == 1600
        assert (
            session.get(Candidate, second["candidate_id"]).provenance["image_preprocessing"]["max_edge"]
            == 2000
        )


def test_versioned_policy_retries_truncation_once_and_caches_complete_workflow(ready, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    budgets = []

    def handler(request):
        budget = json.loads(request.content)["max_output_tokens"]
        budgets.append(budget)
        if budget == 32000:
            return httpx.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"output_tokens": 32000, "total_tokens": 32000},
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "id": "synthetic-response",
                "usage": {"output_tokens": 10, "total_tokens": 10},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample())}]}
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider("test", client=client)
        first = extract_record(ready, "R001", provider)
        assert first["status"] == "succeeded"
        assert first["usage"]["output_tokens"] == 32010
        assert extract_record(ready, "R001", provider)["status"] == "cached"
        assert budgets == [32000, 64000]
    with ready.factory() as session:
        run = session.get(ExtractionRun, first["run_id"])
        assert [a["status"] for a in run.configuration["attempts"]] == ["failed", "succeeded"]
        assert run.configuration["execution_policy"]["max_total_output_tokens"] == 96000
    manifest_path = ready.path / "runs" / first["run_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["configuration"]["execution_policy"]["version"] == "bounded-extraction-v1"
    assert manifest["prompt"] and manifest["response_schema"]
    assert "OPENAI_API_KEY" not in manifest_path.read_text()
    with ready.factory() as session:
        run = session.get(ExtractionRun, first["run_id"])
        assert len(run.configuration["image_stats"][0]["sent_sha256"]) == 64
    assert (ready.path / "runs" / first["run_id"] / "attempts" / "1.json").exists()
    assert (ready.path / "runs" / first["run_id"] / "attempts" / "2.json").exists()


@pytest.mark.parametrize(
    "failure,expected,calls",
    [
        ("truncated", "output_budget_exhausted", 2),
        ("timeout", "request_timeout_outcome_unknown", 1),
        ("auth", "api_http_401", 1),
        ("other_incomplete", "incomplete_response", 1),
    ],
)
def test_policy_has_bounded_failures_without_uncontrolled_retries(
    ready, monkeypatch, failure, expected, calls
):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["max_output_tokens"])
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "auth":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
        reason = "max_output_tokens" if failure == "truncated" else "content_filter"
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": reason},
                "usage": {"output_tokens": 1},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = extract_record(ready, "R001", OpenAIProvider("test", client=client))
    assert result["status"] == "failed"
    assert result["error"] == expected
    assert len(seen) == calls
    assert ready.records()[0].active_run_id is None
    with ready.factory() as session:
        assert session.scalar(select(Candidate)) is None
        run = session.get(ExtractionRun, result["run_id"])
        assert len(run.configuration["attempts"]) == calls


def test_changed_policy_does_not_reuse_old_candidate(ready, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample())}]}
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = extract_record(ready, "R001", OpenAIProvider("test", client=client))
        changed = extract_record(
            ready, "R001", OpenAIProvider("test", client=client, max_output_ceiling=32000)
        )
    assert first["candidate_id"] != changed["candidate_id"]

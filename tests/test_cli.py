import json
import subprocess
import sys

from test_ingest import pdf


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "food_record_digitizer.cli", *map(str, args)], capture_output=True, text=True
    )


def test_cli_single_batch_repeat_and_draft_export(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    main = pdf(source / "R001.pdf")
    meta = source / "metadata.json"
    meta.write_text(json.dumps({"record_id": "R001", "subject_id": "001", "date": "NA"}))
    workspace = tmp_path / "work"
    assert cli("init", "--workspace", workspace, "--project", "study").returncode == 0
    assert cli("init", "--workspace", workspace, "--project", "study").returncode == 2
    first = cli("ingest-one", "--workspace", workspace, "--pdf", main, "--metadata", meta)
    assert first.returncode == 0, first.stderr
    second = cli("ingest", "--workspace", workspace, "--input", source, "--metadata", meta)
    assert json.loads(second.stdout)["unchanged"] == ["R001"]
    dry = cli("extract", "--workspace", workspace, "--pending-only", "--dry-run")
    assert json.loads(dry.stdout)["api_calls"] == 0
    assert json.loads(dry.stdout)["execution_policy"]["output_budgets"] == [32000, 64000]
    assert json.loads(dry.stdout)["execution_policy"]["request_timeout_seconds"] == 1200
    exported = tmp_path / "draft.json"
    assert cli("export-draft", "--workspace", workspace, "--output", exported).returncode == 0
    assert json.loads(exported.read_text())["research_approved"] is False
    assert cli("export-draft", "--workspace", workspace, "--output", exported).returncode == 2
    assert cli("extract", "--workspace", workspace, "--record-id", "absent", "--dry-run").returncode == 2


def test_missing_input_directory_is_not_a_successful_empty_import(tmp_path):
    meta = tmp_path / "metadata.json"
    meta.write_text("[]")
    result = cli("inspect", "--input", tmp_path / "missing", "--metadata", meta)
    assert result.returncode == 2
    assert "Input directory does not exist" in result.stderr

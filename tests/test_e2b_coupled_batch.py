from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.e2b_coupled_batch import (
    E2BCoupledBatchError,
    build_checkpoint_package,
    build_source_package,
    load_private_environment,
    load_spec,
    safe_extract,
)


def write_spec(tmp_path: Path, workers: list[dict] | None = None) -> Path:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Build all source-shaped routes.\n", encoding="utf-8")
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "template": "webanything-coupled-v1",
                "max_concurrency": 15,
                "sandbox_timeout_seconds": 21600,
                "workers": workers
                or [
                    {
                        "id": "example",
                        "url": "https://example.com/",
                        "run_dir": "runs/example",
                        "requirements_file": "requirements.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_spec_resolves_sites_and_defaults(tmp_path: Path):
    spec = load_spec(write_spec(tmp_path))
    assert spec.template == "webanything-coupled-v1"
    assert spec.max_concurrency == 15
    assert spec.jobs[0].run_dir == (tmp_path / "runs/example").resolve()
    assert spec.jobs[0].storage_state is None
    assert spec.jobs[0].model == "gpt-5.6-sol"
    assert spec.jobs[0].reasoning_effort == "xhigh"


def test_load_spec_rejects_duplicate_writers_and_credential_urls(tmp_path: Path):
    workers = [
        {
            "id": "one",
            "url": "https://user:secret@example.com/",
            "run_dir": "runs/shared",
            "requirements_file": "requirements.md",
        },
        {
            "id": "two",
            "url": "https://example.org/",
            "run_dir": "runs/shared",
            "requirements_file": "requirements.md",
        },
    ]
    with pytest.raises(E2BCoupledBatchError, match="credentials"):
        load_spec(write_spec(tmp_path, workers))
    workers[0]["url"] = "https://example.com/"
    with pytest.raises(E2BCoupledBatchError, match="duplicate run_dir"):
        load_spec(write_spec(tmp_path, workers))


def test_source_package_uses_worktree_but_excludes_dependencies_and_secrets(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "scripts").mkdir()
    (root / "scripts/run.py").write_text("version = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\nnode_modules/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore", "scripts/run.py"], check=True)
    (root / "scripts/run.py").write_text("version = 2\n", encoding="utf-8")
    (root / "new.py").write_text("new = True\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules/large.bin").write_bytes(b"large")
    package = tmp_path / "source.tar.gz"

    build_source_package(root, package)

    with tarfile.open(package, "r:gz") as archive:
        names = archive.getnames()
        assert "scripts/run.py" in names
        assert "new.py" in names
        assert ".env" not in names
        assert not any("node_modules" in name for name in names)
        assert archive.extractfile("scripts/run.py").read() == b"version = 2\n"


def test_checkpoint_package_never_contains_browser_state(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "workspace/sites/demo").mkdir(parents=True)
    (run_dir / "workspace/sites/demo/app.py").write_text("app = 1\n", encoding="utf-8")
    (run_dir / ".session").mkdir()
    (run_dir / ".session/browser-state.json").write_text("private", encoding="utf-8")
    (run_dir / "workspace/.env.local").write_text("private", encoding="utf-8")
    (run_dir / "workspace/storage-state.json").write_text("private", encoding="utf-8")
    package = tmp_path / "checkpoint.tar.gz"

    assert build_checkpoint_package(run_dir, package) is True

    with tarfile.open(package, "r:gz") as archive:
        names = archive.getnames()
        assert "workspace/sites/demo/app.py" in names
        assert not any(
            ".session" in name or "browser-state" in name or "storage-state" in name or ".env" in name
            for name in names
        )


def test_safe_extract_rejects_traversal(tmp_path: Path):
    archive_path = tmp_path / "bad.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("bad", encoding="utf-8")
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(payload, arcname="../escaped")
    with pytest.raises(E2BCoupledBatchError, match="unsafe result archive"):
        safe_extract(archive_path, tmp_path / "out")


def test_private_environment_maps_northcua_names_without_leaking_unknown_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env_file = tmp_path / "private.env"
    env_file.write_text(
        "E2B_API_KEY=e2b-test\n"
        "E2B_API_URL=https://e2b.invalid\n"
        "E2B_DOMAIN=e2b.invalid\n"
        "NORTHCUA_OPENAI_RESPONSES_API_KEY=provider-test\n"
        "NORTHCUA_OPENAI_RESPONSES_BASE_URL=https://provider.invalid/v1\n"
        "UNRELATED_SECRET=must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)

    provider = load_private_environment(env_file)

    assert provider == {
        "WEBANYTHING_RESPONSES_API_KEY": "provider-test",
        "WEBANYTHING_RESPONSES_BASE_URL": "https://provider.invalid/v1",
    }
    assert "UNRELATED_SECRET" not in os.environ

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.batch_supervisor import BatchSupervisor, SupervisorError, load_spec, worker_state


def _write_status_command(path: Path, status: str, *, field: str = "status", exit_code: int = 0) -> list[str]:
    payload = json.dumps({field: status})
    script = (
        "from pathlib import Path; "
        f"Path({str(path)!r}).write_text({payload!r}, encoding='utf-8'); "
        f"raise SystemExit({exit_code})"
    )
    return [sys.executable, "-c", script]


def _supervisor(root: Path, payload: dict, workers: list) -> BatchSupervisor:
    state_dir = root / "state"
    return BatchSupervisor(
        root=root,
        spec_payload=payload,
        workers=workers,
        state_dir=state_dir,
        status_dir=state_dir / "status",
        heartbeat_dir=state_dir / "heartbeats",
        log_dir=state_dir / "logs",
        summary_path=state_dir / "batch-summary.json",
        pid_file=state_dir / "supervisor.pid",
        lock_file=state_dir / "supervisor.lock",
        max_concurrency=1,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.02,
        shutdown_grace_seconds=0.05,
    )


class BatchSupervisorRecoveryTests(unittest.TestCase):
    def test_worker_state_accepts_sdk_trace_execution_statuses(self):
        self.assertEqual(worker_state({"execution_status": "timed_out"}), "timed_out")
        self.assertEqual(
            worker_state({"execution_status": "timed_out", "latest_execution_status": "completed"}),
            "completed",
        )

    def test_wall_timeout_switches_to_retry_command_and_records_history(self):
        with TemporaryDirectory() as directory:
            root_path = Path(directory)
            worker_status = root_path / "run.json"
            spec_path = root_path / "batch.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "root": str(root_path),
                        "max_attempts": 2,
                        "worker_timeout_seconds": 0.1,
                        "restart_delay_seconds": 0,
                        "max_restart_delay_seconds": 0,
                        "workers": [
                            {
                                "id": "recoverable",
                                "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                                "retry_command": _write_status_command(
                                    worker_status,
                                    "completed",
                                    field="latest_execution_status",
                                ),
                                "status_file": str(worker_status),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            root, payload, workers = load_spec(spec_path)
            supervisor = _supervisor(root, payload, workers)
            self.assertEqual(supervisor.run(), 0)

            summary = json.loads((root_path / "state/batch-summary.json").read_text(encoding="utf-8"))
            worker = summary["workers"][0]
            self.assertEqual(worker["state"], "succeeded")
            self.assertEqual(worker["attempt"], 2)
            self.assertEqual(worker["restart_count"], 1)
            self.assertEqual(worker["active_command_kind"], "retry")
            self.assertEqual(worker["observed_worker_state"], "completed")
            self.assertEqual(
                [item["outcome"] for item in worker["attempt_history"]],
                ["timed_out", "succeeded"],
            )
            self.assertIs(worker["attempt_history"][0]["timed_out"], True)

    def test_terminal_verifier_failure_does_not_run_retry_command(self):
        with TemporaryDirectory() as directory:
            root_path = Path(directory)
            worker_status = root_path / "terminal-result.json"
            retry_marker = root_path / "retry-ran.json"
            spec_path = root_path / "batch.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "root": str(root_path),
                        "max_attempts": 3,
                        "restart_delay_seconds": 0,
                        "max_restart_delay_seconds": 0,
                        "workers": [
                            {
                                "id": "verifier",
                                "command": _write_status_command(worker_status, "failed", exit_code=1),
                                "retry_command": _write_status_command(retry_marker, "passed"),
                                "status_file": str(worker_status),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            root, payload, workers = load_spec(spec_path)
            supervisor = _supervisor(root, payload, workers)
            self.assertEqual(supervisor.run(), 2)

            summary = json.loads((root_path / "state/batch-summary.json").read_text(encoding="utf-8"))
            worker = summary["workers"][0]
            self.assertEqual(worker["state"], "failed")
            self.assertEqual(worker["attempt"], 1)
            self.assertEqual(worker["observed_worker_state"], "failed")
            self.assertFalse(retry_marker.exists())

            restarted = _supervisor(root, payload, workers)
            self.assertEqual(restarted.run(), 2)
            restarted_summary = json.loads(
                (root_path / "state/batch-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(restarted_summary["workers"][0]["attempt"], 1)
            self.assertFalse(retry_marker.exists())

    def test_retry_command_and_timeout_must_be_well_formed(self):
        with TemporaryDirectory() as directory:
            root_path = Path(directory)
            spec_path = root_path / "batch.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "root": str(root_path),
                        "worker_timeout_seconds": 0,
                        "workers": [{"id": "bad", "command": ["true"], "retry_command": "true"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SupervisorError, "worker_timeout_seconds must be positive"):
                load_spec(spec_path)

            spec_path.write_text(
                json.dumps(
                    {
                        "root": str(root_path),
                        "workers": [{"id": "bad", "command": ["true"], "retry_command": "true"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SupervisorError, r"workers\[0\]\.retry_command"):
                load_spec(spec_path)


if __name__ == "__main__":
    unittest.main()

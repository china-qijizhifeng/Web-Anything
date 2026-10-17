#!/usr/bin/env python3
"""Run the click-through gate and publish one passing mirror to the E2B gallery."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from audit_clickthrough import AuditConfig, audit, load_scope_policy
from fidelity_contract import fidelity_evidence_state
from quality_contract import quality_evidence_state
from site_fingerprint import site_implementation_fingerprint
from verify_reviewable_workflow import verify as verify_reviewable_workflow


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MANIFEST = ROOT / "generated" / "demo-preview-manifest.json"
DEFAULT_SANDBOX = ""
DEFAULT_GALLERY_URL = ""
E2B_ENV_NAMES = {"E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GALLERY_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def default_deployment_target() -> tuple[str, str]:
    record = read_evidence(ROOT / "generated" / "e2b-preview-gallery.json")
    if record:
        sandbox = str(record.get("sandbox_id") or "").strip()
        gateway = str(record.get("gateway_url") or "").strip()
        if sandbox and gateway:
            return sandbox, gateway
    return DEFAULT_SANDBOX, DEFAULT_GALLERY_URL


def relative_workspace(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise SystemExit(f"source workspace must be inside repository root: {path}") from exc


def normalize_gallery_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_")
    if not normalized or not GALLERY_ID_RE.fullmatch(normalized):
        raise SystemExit("gallery id must contain at least one letter, digit, or underscore")
    return normalized


def update_manifest(manifest: dict, entry: dict) -> dict:
    sites = list(manifest.get("sites") or [])
    for old in sites:
        if old.get("id") == entry["id"]:
            raise SystemExit(f"gallery already contains site id {entry['id']}; use --replace-existing explicitly")
        if int(old.get("port", -1)) == int(entry["port"]):
            raise SystemExit(f"gallery already contains site port {entry['port']}")
    sites.append(entry)
    return {**manifest, "sites": sites}


def read_evidence(path: Path) -> dict | None:
    try:
        value = load_json(path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def current_evidence_failures(
    workspace: Path,
    site: str,
    source_site: str | None = None,
) -> list[str]:
    evidence_dir = workspace / "mirror_factory" / "evidence" / site
    fidelity = read_evidence(evidence_dir / "fidelity.json")
    quality = read_evidence(evidence_dir / "quality.json")
    failures: list[str] = []
    fidelity_state = fidelity_evidence_state(fidelity, root=workspace)
    quality_state = quality_evidence_state(quality, root=workspace)
    if fidelity_state != "passed_current":
        failures.append(f"fidelity evidence is {fidelity_state}")
    if quality_state != "passed_current":
        failures.append(f"quality evidence is {quality_state}")
    elif quality.get("grade") not in {"full_fidelity", "synthetic_grounded_data"}:
        failures.append(f"quality grade is not deployable: {quality.get('grade')!r}")
    implementation_site = source_site or site
    site_root = workspace / "sites" / implementation_site
    if not site_root.is_dir():
        failures.append(
            f"site implementation directory is missing: sites/{implementation_site}"
        )
        return failures
    current = site_implementation_fingerprint(site_root)
    if isinstance(fidelity, dict) and fidelity.get("site_implementation") != current:
        failures.append("fidelity evidence does not match the current site implementation")
    if isinstance(quality, dict) and quality.get("site_implementation") != current:
        failures.append("quality evidence does not match the current site implementation")
    return failures


def deploy_environment(env_file: Path | None) -> dict[str, str]:
    """Load only the E2B settings without executing the environment file."""
    environment = dict(os.environ)
    if env_file is None:
        return environment
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"E2B environment file is unavailable: {env_file}") from exc
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if name not in E2B_ENV_NAMES:
            continue
        if not ENV_NAME_RE.fullmatch(name):
            raise SystemExit(f"invalid environment assignment at {env_file}:{number}")
        try:
            values = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise SystemExit(f"invalid environment value at {env_file}:{number}") from exc
        if len(values) > 1:
            raise SystemExit(f"environment value must be one shell word at {env_file}:{number}")
        environment[name] = values[0] if values else ""
    return environment


def deployment_record_failures(
    output: Path,
    *,
    expected_sandbox: str,
    expected_gallery_url: str,
    allow_replacement: bool = False,
) -> list[str]:
    record = read_evidence(output)
    if record is None:
        return ["deployment record is missing or invalid"]
    failures = list(record.get("verification_failures") or [])
    replacement = (
        allow_replacement
        and record.get("replaced_missing_sandbox") is True
        and record.get("requested_sandbox_id") == expected_sandbox
    )
    if record.get("sandbox_id") != expected_sandbox and not replacement:
        failures.append("deployment record sandbox does not match requested sandbox")
    actual_url = str(record.get("gateway_url") or "").rstrip("/") + "/"
    requested_url = expected_gallery_url.rstrip("/") + "/"
    if actual_url != requested_url and not replacement:
        failures.append(
            f"deployment gateway mismatch: expected {requested_url}, got {actual_url}"
        )
    return failures


def run_deployer(args: argparse.Namespace, manifest: Path, output: Path) -> subprocess.CompletedProcess:
    command = [
        args.e2b_python,
        str(ROOT / "scripts" / "deploy_preview_gallery_e2b.py"),
        "--manifest", str(manifest),
        "--sandbox-id", args.sandbox_id,
        "--output", str(output),
        "--create-if-missing",
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        env=deploy_environment(args.e2b_env_file),
        text=True,
    )


def main() -> int:
    default_sandbox, default_gallery_url = default_deployment_target()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--gallery-id", help="Gallery entry id; defaults to --site.")
    parser.add_argument("--base-url", required=True, help="Running loopback mirror URL for the pre-publish audit.")
    parser.add_argument("--port", required=True, type=int, help="Stable internal E2B port.")
    parser.add_argument("--source-workspace", required=True, type=Path)
    parser.add_argument("--source-site", help="Directory under source workspace/sites; defaults to --site.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--tier", default="sdk_preview")
    parser.add_argument("--expected-text", required=True)
    parser.add_argument("--official-host", action="append", default=[])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sandbox-id", default=default_sandbox)
    parser.add_argument("--gallery-url", default=default_gallery_url)
    parser.add_argument("--min-clickthrough", type=float, default=80.0)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument(
        "--scope-policy",
        type=Path,
        help="Optional bounded editorial archive scope policy JSON.",
    )
    parser.add_argument(
        "--acceptance-profile",
        choices=("full-site", "reviewable-workflow"),
        default="full-site",
    )
    parser.add_argument("--reviewable-contract", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "generated" / "e2b-preview-gallery.json")
    parser.add_argument("--e2b-python", default=os.environ.get("WEBHARBOR_E2B_PYTHON", sys.executable))
    parser.add_argument(
        "--e2b-env-file",
        type=Path,
        default=Path(os.environ["WEBANYTHING_E2B_ENV_FILE"]) if os.environ.get("WEBANYTHING_E2B_ENV_FILE") else None,
        help="Environment file containing E2B_API_KEY, E2B_API_URL, and E2B_DOMAIN.",
    )
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    requested_gallery_id = args.gallery_id or args.site
    gallery_id = normalize_gallery_id(requested_gallery_id)
    if not args.expected_text.strip():
        raise SystemExit("--expected-text must not be empty")

    parsed = urlsplit(args.base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("--base-url must be a loopback HTTP(S) origin")
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    workspace = args.source_workspace.resolve()
    source_site = args.source_site or args.site
    site_root = workspace / "sites" / source_site
    if not site_root.is_dir():
        raise SystemExit(f"missing source site directory: {site_root}")
    if not Path(args.e2b_python).is_file():
        raise SystemExit(f"E2B Python not found: {args.e2b_python}")
    evidence_dir = workspace / "mirror_factory" / "evidence" / args.site
    output_coverage = evidence_dir / "clickthrough-coverage.json"
    if args.acceptance_profile == "reviewable-workflow":
        declaration = (
            args.reviewable_contract.resolve()
            if args.reviewable_contract
            else site_root / "reviewable_workflow.json"
        )
        output_coverage = evidence_dir / "reviewable-workflow.json"
        coverage = verify_reviewable_workflow(
            root=workspace,
            site=args.site,
            declaration_path=declaration,
            base_url=base_url,
            output=output_coverage,
        )
        if coverage["status"] != "passed":
            print(json.dumps({
                "status": "blocked",
                "reason": "reviewable workflow gate failed; gallery manifest was not changed",
                "coverage": output_coverage.as_posix(),
                "failures": coverage["failures"],
            }, ensure_ascii=False, indent=2))
            return 2
        if args.tier != "sdk_reviewable_workflow":
            raise SystemExit(
                "reviewable-workflow publication requires --tier sdk_reviewable_workflow"
            )
    else:
        evidence_failures = current_evidence_failures(workspace, args.site, source_site)
        if evidence_failures:
            print(json.dumps({
                "status": "blocked",
                "reason": "current fidelity/quality gate failed; gallery manifest was not changed",
                "failures": evidence_failures,
            }, ensure_ascii=False, indent=2))
            return 2
        config = AuditConfig(
            base_url=base_url,
            max_pages=max(1, args.max_pages),
            timeout_ms=15000,
            min_body_chars=100,
            min_clickthrough=max(0.0, min(100.0, args.min_clickthrough)),
            official_hosts=frozenset(host.casefold().strip().strip(".") for host in args.official_host),
            include_external=False,
            scope_policy=load_scope_policy(
                args.scope_policy.resolve() if args.scope_policy else None
            ),
        )
        coverage = audit(config, output_coverage, site_root)
        if coverage["status"] != "passed":
            print(json.dumps({
                "status": "blocked",
                "reason": "click-through gate failed; gallery manifest was not changed",
                "coverage": output_coverage.as_posix(),
                "next_candidates": coverage["next_candidates"],
            }, ensure_ascii=False, indent=2))
            return 2

    manifest_path = args.manifest.resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = manifest_path.with_name(manifest_path.name + ".lock")
    entry = {
        "port": args.port,
        "id": gallery_id,
        "name": args.name,
        "tier": args.tier,
        "description": (
            args.description
            if args.acceptance_profile != "reviewable-workflow"
            else "Single-workflow review build; broader site coverage remains in progress."
        ),
        "source_workspace": relative_workspace(workspace),
        "source_site": source_site,
        "expected_site": args.site,
    }
    entry["expected_text"] = args.expected_text

    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        manifest = load_json(manifest_path)
        sites = list(manifest.get("sites") or [])
        existing = next((item for item in sites if item.get("id") == gallery_id), None)
        if existing and not args.replace_existing:
            raise SystemExit(f"gallery already contains site id {gallery_id}; pass --replace-existing to update it")
        if existing:
            sites = [item for item in sites if item.get("id") != gallery_id]
            manifest = {**manifest, "sites": sites}
        candidate_manifest = update_manifest(manifest, entry)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=manifest_path.parent, delete=False) as handle:
            temp_manifest = Path(handle.name)
            json.dump(candidate_manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        deployment_output = args.output.resolve()
        deployment_output.parent.mkdir(parents=True, exist_ok=True)
        candidate_output = deployment_output.with_name(deployment_output.name + ".candidate")
        rollback_output = deployment_output.with_name(deployment_output.name + ".rollback")
        try:
            completed = run_deployer(args, temp_manifest, candidate_output)
            candidate_failures = (
                [f"deployer exited with {completed.returncode}"]
                if completed.returncode != 0
                else deployment_record_failures(
                    candidate_output,
                    expected_sandbox=args.sandbox_id,
                    expected_gallery_url=args.gallery_url,
                    allow_replacement=True,
                )
            )
            if candidate_failures:
                rollback = run_deployer(args, manifest_path, rollback_output)
                rollback_failures = (
                    [f"deployer exited with {rollback.returncode}"]
                    if rollback.returncode != 0
                    else deployment_record_failures(
                        rollback_output,
                        expected_sandbox=args.sandbox_id,
                        expected_gallery_url=args.gallery_url,
                        allow_replacement=True,
                    )
                )
                if not rollback_failures:
                    os.replace(rollback_output, deployment_output)
                    rollback_status = "restored_previous_gallery"
                else:
                    rollback_status = "rollback_failed_manual_redeploy_required"
                raise SystemExit(
                    "E2B gallery deployment failed; shared manifest was not changed; "
                    + rollback_status
                    + "; "
                    + "; ".join(candidate_failures)
                )
            try:
                deployment = load_json(candidate_output)
                os.replace(candidate_output, deployment_output)
                os.replace(temp_manifest, manifest_path)
            except BaseException:
                rollback = run_deployer(args, manifest_path, rollback_output)
                if rollback.returncode == 0 and not deployment_record_failures(
                    rollback_output,
                    expected_sandbox=args.sandbox_id,
                    expected_gallery_url=args.gallery_url,
                    allow_replacement=True,
                ):
                    os.replace(rollback_output, deployment_output)
                raise
        finally:
            temp_manifest.unlink(missing_ok=True)
            candidate_output.unlink(missing_ok=True)
            rollback_output.unlink(missing_ok=True)

    print(json.dumps({
        "status": "published",
        "site": args.site,
        "gallery_id": gallery_id,
        "requested_gallery_id": requested_gallery_id,
        "coverage": output_coverage.as_posix(),
        "gallery_manifest": manifest_path.as_posix(),
        "deployment": args.output.resolve().as_posix(),
        "gallery_url": deployment["gateway_url"],
        "site_url": deployment["gateway_url"].rstrip("/") + f"/p/{args.port}/",
        "completion_level": (
            coverage.get("completion_level")
            if args.acceptance_profile == "reviewable-workflow"
            else (coverage.get("scope_policy") or {}).get("completion_level")
        ),
        "meaningful_local_percent": (
            None
            if args.acceptance_profile == "reviewable-workflow"
            else coverage["clickthrough"]["meaningful_local_percent"]
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

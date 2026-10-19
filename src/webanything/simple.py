"""Small public entry point for the source-coupled Full/JIT pipeline."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlsplit


def config_path() -> Path:
    override = os.environ.get("WEBANYTHING_CONFIG")
    if override:
        return Path(override).expanduser().absolute()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (base / "webanything" / "provider.json").expanduser().absolute()


def validate_url(value: str, *, label: str, allow_query: bool = False) -> str:
    if not isinstance(value, str):
        raise SystemExit(f"{label} must be an absolute HTTP(S) URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit(f"{label} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise SystemExit(f"{label} must not contain credentials")
    if not allow_query and (parsed.query or parsed.fragment):
        raise SystemExit(f"{label} must not contain a query or fragment")
    return value if allow_query else value.rstrip("/")


def validate_model(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n\x00"):
        raise SystemExit("model ID is missing or invalid")
    return value.strip()


def configure(args) -> int:
    url = validate_url(args.api_url, label="API URL")
    key = os.environ.get(args.api_key_env) if args.api_key_env else getpass.getpass("API key: ")
    if not key or not key.strip() or "\n" in key or "\x00" in key:
        raise SystemExit("API key is missing or invalid")
    model = getattr(args, "model", None)
    if model is not None:
        model = validate_model(model)
    data = {"schema_version": 1, "api_url": url, "api_key": key.strip()}
    if model:
        data["model"] = model.strip()
    save_provider(data)
    return 0


def save_provider(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.environ.get("WEBANYTHING_CONFIG"):
        path.parent.chmod(0o700)
    if path.is_symlink():
        raise SystemExit("refusing to replace a symlinked provider config")
    payload = json.dumps(data) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".provider-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Provider saved to {path}")


def provider() -> tuple[str, str]:
    env_url = os.environ.get("WEBANYTHING_API_URL")
    env_key = os.environ.get("WEBANYTHING_API_KEY")
    if env_url or env_key:
        if not env_url or not env_key:
            raise SystemExit("set both WEBANYTHING_API_URL and WEBANYTHING_API_KEY")
        return validate_url(env_url, label="API URL"), env_key
    env_url = os.environ.get("WEBANYTHING_RESPONSES_BASE_URL")
    env_key = os.environ.get("WEBANYTHING_RESPONSES_API_KEY")
    if env_url or env_key:
        if not env_url or not env_key:
            raise SystemExit("set both WEBANYTHING_RESPONSES_BASE_URL and WEBANYTHING_RESPONSES_API_KEY")
        return validate_url(env_url, label="API URL"), env_key
    data = saved_provider()
    return validate_url(data.get("api_url", ""), label="API URL"), data["api_key"]


def saved_provider() -> dict:
    path = config_path()
    if not path.is_file() or path.is_symlink():
        raise SystemExit("provider is not configured; run: webanything setup")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit("provider config must be readable only by its owner (mode 0600)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit("provider config is invalid") from error
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("api_key"), str) or not data["api_key"]:
        raise SystemExit("provider config is invalid")
    return data


def selected_model() -> str:
    explicit = os.environ.get("WEBANYTHING_MODEL")
    if explicit:
        return explicit
    if (os.environ.get("WEBANYTHING_API_URL") or os.environ.get("WEBANYTHING_API_KEY")
            or os.environ.get("WEBANYTHING_RESPONSES_BASE_URL") or os.environ.get("WEBANYTHING_RESPONSES_API_KEY")):
        return "gpt-5.6-sol"
    path = config_path()
    if path.is_file() and not path.is_symlink():
        model = saved_provider().get("model")
        if isinstance(model, str) and model.strip():
            return model
    return "gpt-5.6-sol"


def agent_skill(root: Path) -> str:
    executable = shlex.quote(sys.executable)
    repo = shlex.quote(str(root))
    command = f"WEBANYTHING_ROOT={repo} {executable} -m webanything.cli"
    return f"""---
name: webanything
description: Reconstruct a public website from its URL with WebAnything. Use for Full site coverage or a JIT user task and verify the generated interactive page.
---

# WebAnything website reconstruction

When the user gives a source website URL and asks to reconstruct it, choose a scope:

- If they describe a task to perform on the site, use JIT with their task text.
- If they explicitly request broad coverage, use Full.
- If the intended scope is unclear, ask for the task or whether they want Full.

Run from a terminal (replace quoted placeholders with shell-quoted values):

```bash
{command} create 'SOURCE_URL' --mode jit --task 'USER_TASK'
{command} create 'SOURCE_URL' --mode full
```

Do not ask for or expose the API key in chat; setup has stored it separately.
The command prints a run directory and status. Report completion only when status
is `complete`. If complete, use `{command} preview 'RUN_DIR'` and show the local
preview URL. For any other status, report the actual state and the run directory.
Do not treat a screenshot or a running preview as independent verification.

When explaining a run, separate persisted observations from inferences and
unknowns. Cite run artifacts for observations. Label inferences and confidence.
For each material unknown, state which gate it affects and the bounded check
needed to resolve it. Missing evidence never proves that behavior is absent or
equivalent, and a required gate with a material unknown cannot be reported as passed.
"""


def setup(args, root: Path) -> int:
    agents = ("codex", "claude") if args.agent == "both" else (args.agent,)
    destinations = {
        "codex": Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills/webanything/SKILL.md",
        "claude": Path.home() / ".claude/skills/webanything/SKILL.md",
    }
    for agent in agents:
        path = destinations[agent]
        if path.is_symlink() or (path.exists() and (not path.is_file() or not path.read_text(encoding="utf-8").startswith("---\nname: webanything\n"))):
            raise SystemExit(f"refusing to overwrite an existing skill: {path}")
    if not args.api_url and config_path().is_file():
        existing = saved_provider()
        api_url = existing["api_url"]
        model = validate_model(args.model or existing.get("model") or input("Model ID [gpt-5.6-sol]: ").strip() or "gpt-5.6-sol")
        if model != existing.get("model"):
            existing["model"] = model
            save_provider(existing)
    else:
        api_url = args.api_url or input("Responses API URL (usually ending in /v1): ").strip()
        model = validate_model(args.model or input("Model ID [gpt-5.6-sol]: ").strip() or "gpt-5.6-sol")
        configure(Namespace(api_url=api_url, api_key_env=args.api_key_env, model=model))
    content = agent_skill(root)
    for agent in agents:
        path = destinations[agent]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(".SKILL.md.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
        print(f"{agent} skill installed: {path}")
    print("Start a new agent chat, then ask: Rebuild https://example.org for the task 'Find and open the second example'.")
    return 0


def run_identity(source_url: str, mode: str, task: str | None) -> str:
    host = urlsplit(source_url).hostname or "site"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:40] or "site"
    identity = json.dumps({"url": source_url, "mode": mode, "task": task or ""}, sort_keys=True)
    return f"{slug}-{hashlib.sha256(identity.encode()).hexdigest()[:10]}"


def create(args, root: Path) -> int:
    source_url = validate_url(args.url, label="website URL", allow_query=True)
    task = args.task.strip() if args.task else None
    if args.mode == "jit" and not task:
        raise SystemExit("JIT mode requires --task 'what the user needs to do'")
    if args.mode == "full" and task:
        raise SystemExit("Full mode does not accept --task; select --mode jit")
    site = run_identity(source_url, args.mode, task)
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else root / "runs" / site
    environment = dict(os.environ)
    environment.pop("WEBANYTHING_ENV_FILE", None)
    if not args.initialize_only:
        api_url, api_key = provider()
        environment["WEBANYTHING_RESPONSES_BASE_URL"] = api_url
        environment["WEBANYTHING_RESPONSES_API_KEY"] = api_key
    command = [sys.executable, str(root / "scripts/coupled_pipeline.py"), source_url,
               "--site", site, "--run-dir", str(run_dir), "--mode", args.mode,
               "--model", args.model or selected_model(), "--reasoning-effort", args.reasoning_effort,
               "--timeout-minutes", str(args.timeout_minutes),
               "--max-cycles", str(args.max_cycles)]
    if task:
        command.extend(["--task-query", task])
    if args.initialize_only:
        command.append("--initialize-only")
    completed = subprocess.run(command, cwd=root, env=environment, check=False)
    try:
        manifest = json.loads((run_dir / "coupled-run.json").read_text(encoding="utf-8"))
        status = manifest.get("status", "incomplete")
    except (OSError, ValueError):
        status = "incomplete"
    print(f"Site: {site}\nMode: {args.mode}\nStatus: {status}\nRun: {run_dir}")
    if status == "complete":
        print(f"Preview: webanything preview {run_dir}")
    return completed.returncode


def preview(args) -> int:
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    run_dir = Path(args.run_dir).expanduser().resolve()
    try:
        manifest = json.loads((run_dir / "coupled-run.json").read_text(encoding="utf-8"))
        site = manifest["site_id"]
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"invalid run directory: {run_dir}") from error
    candidate = run_dir / "workspace" / "sites" / site
    if not (candidate / "app.py").is_file():
        raise SystemExit(f"candidate app is missing: {candidate}")
    print(f"Local preview: http://127.0.0.1:{args.port}/", flush=True)
    return subprocess.run([sys.executable, "-m", "flask", "--app", "app:app", "run",
                           "--host", "127.0.0.1", "--port", str(args.port), "--no-reload"],
                          cwd=candidate, check=False).returncode

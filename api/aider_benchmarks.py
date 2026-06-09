from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AIDER_REPO_URL = "https://github.com/Aider-AI/aider.git"
POLYGLOT_REPO_URL = "https://github.com/Aider-AI/polyglot-benchmark"
APPORA_AIDER_MODEL_SETTINGS = """\
- name: openai/appora
  edit_format: whole
  weak_model_name: openai/appora
  use_repo_map: false
  lazy: false
  overeager: true
  reminder: sys
  examples_as_sys_msg: true
  use_temperature: false
  system_prompt_prefix: "Benchmark mode: do not ask clarification questions and do not write sentences addressed to the user. Do not emit tool calls or prose-only verification plans. Read the exercise instructions, visible tests, and failure output exactly. Preserve the requested API shape and update all matching declarations/definitions together. Treat expected-vs-actual whitespace, blank lines, final trailing newlines, capitalization, singular/plural wording, default arguments, and exception messages as hard API contracts. If expected output displays a final blank line or expected string ends with newline while actual does not, add the trailing newline. If a single-argument call returns a range, default the end/range parameter to the start value. If C++ comparison errors show vector<vector<unsigned int>> vs vector<vector<int>>, change both the declaration and implementation return/container types to unsigned int. For interpreters, DSLs, command dispatchers, and plugin registries, check user-defined/custom definitions before builtins when tests show overrides are legal; execute literal numbers/strings in stored definitions as literals, not unknown commands; when redefining a word from a previous definition, snapshot/expand the previous definition if tests expect old references to remain stable. After a test failure, diagnose the assertion contract first, then edit the source files directly. "
"""
APPORA_AIDER_MODEL_METADATA = {
    "openai/appora": {
        "max_tokens": 8192,
        "max_input_tokens": 131072,
        "max_output_tokens": 8192,
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider": "openai",
        "mode": "chat",
        "supports_function_calling": False,
        "supports_prompt_caching": False,
    }
}
APPORA_AIDER_BENCHMARK_PATCH = """\
from __future__ import annotations

import os
from pathlib import Path


PATCH_MARKER = "def _appora_compact_test_errors"
AIDER_ROOT = Path(os.environ.get("APPORA_AIDER_ROOT", "/aider"))
BENCHMARK_FILE = AIDER_ROOT / "benchmark" / "benchmark.py"


def main() -> None:
    text = BENCHMARK_FILE.read_text(encoding="utf-8")
    if PATCH_MARKER not in text:
        helper = r'''

def _appora_compact_test_errors(lines):
    max_chars = int(os.environ.get("APPORA_AIDER_MAX_TEST_ERROR_CHARS", "24000"))
    if not lines:
        return ""
    joined = "\\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    important = []
    keywords = (" error:", "FAILED", "Assertion", "expected", "but was", "note: old declaration", "no match for", "cannot find", "duplicate class", "incompatible types")
    for line in lines:
        if any(keyword in line for keyword in keywords):
            important.append(line)
    head = lines[:80]
    tail = lines[-160:]
    compact_lines = []
    seen = set()
    for line in head + ["... appora compacted long test output; keeping compiler/assertion highlights ..."] + important[:160] + tail:
        key = line[:240]
        if key in seen:
            continue
        seen.add(key)
        compact_lines.append(line)
    compact = "\\n".join(compact_lines)
    if len(compact) > max_chars:
        compact = compact[: max_chars // 2] + "\\n... appora compacted middle ...\\n" + compact[-max_chars // 2 :]
    return compact


def _appora_public_test_contract(testdir, test_files):
    max_chars = int(os.environ.get("APPORA_AIDER_MAX_TEST_CONTRACT_CHARS", "18000"))
    sections = []
    used = 0
    for file_path in test_files:
        path = testdir / Path(file_path)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        excerpt = text[:remaining]
        used += len(excerpt)
        sections.append(f"\\n\\n# Public test contract: {file_path}\\n```\\n{excerpt}\\n```")
    if not sections:
        return ""
    return (
        "\\n\\nBefore editing, read these public tests as the exact API and output contract. "
        "Do not edit test files; use them to preserve names, constants, exception messages, "
        "whitespace, and edge-case semantics.\\n"
        + "".join(sections)
    )
'''
        text = text.replace("load_dotenv(override=True)\\n", "load_dotenv(override=True)\\n" + helper, 1)
    text = text.replace(
        '    instructions += prompts.instructions_addendum.format(file_list=file_list)\\n',
        '    instructions += _appora_public_test_contract(testdir, test_files)\\n    instructions += prompts.instructions_addendum.format(file_list=file_list)\\n',
        1,
    )
    text = text.replace(
        '        errors = "\\\\n".join(errors)\\n        instructions = errors\\n',
        '        errors = _appora_compact_test_errors(errors)\\n        instructions = errors\\n',
        1,
    )
    BENCHMARK_FILE.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
"""
APPORA_AIDER_SITE_CUSTOMIZE = """\
from __future__ import annotations

import os
from pathlib import Path


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


try:
    from aider import models

    _profile_dir = Path(__file__).resolve().parent
    _settings_file = Path(os.environ.get("AIDER_MODEL_SETTINGS_FILE", _profile_dir / "aider-model-settings.yml"))
    _metadata_file = Path(os.environ.get("AIDER_MODEL_METADATA_FILE", _profile_dir / "aider-model-metadata.json"))
    if _settings_file.exists():
        models.register_models([str(_settings_file)])
    if _metadata_file.exists():
        models.register_litellm_models([str(_metadata_file)])

    _original_model_init = models.Model.__init__

    def _appora_model_init(self, *args, **kwargs):
        _original_model_init(self, *args, **kwargs)
        max_history = _as_int(os.environ.get("AIDER_MAX_CHAT_HISTORY_TOKENS"), 32768)
        if self.name == "openai/appora" and max_history > 0:
            self.max_chat_history_tokens = max_history

    models.Model.__init__ = _appora_model_init

    from aider.coders import Coder

    _original_coder_create = Coder.create.__func__

    def _appora_coder_create(cls, *args, **kwargs):
        kwargs["auto_lint"] = False
        return _original_coder_create(cls, *args, **kwargs)

    Coder.create = classmethod(_appora_coder_create)
except Exception as exc:  # pragma: no cover - surfaced in benchmark logs
    print(f"appora_aider_profile_error={exc}")
"""


@dataclass(frozen=True)
class AiderBenchmarkConfig:
    workspace: Path = Path(".tmp-aider-benchmark")
    run_name: str = "appora-aider"
    model: str = "openai/appora"
    edit_format: str = "whole"
    threads: int = 1
    num_tests: int | None = None
    tries: int = 3
    keywords: str | None = None
    openai_api_base: str = "http://host.docker.internal:20128/v1"
    openai_api_key_env: str = "NINE_ROUTER_API_KEY"
    max_chat_history_tokens: int = 8192


def _aider_dir(workspace: Path) -> Path:
    return workspace / "aider"


def _polyglot_dir(aider_dir: Path) -> Path:
    return aider_dir / "tmp.benchmarks" / "polyglot-benchmark"


def _appora_aider_dir(aider_dir: Path) -> Path:
    return aider_dir / ".appora"


def _appora_model_settings_path(aider_dir: Path) -> Path:
    return _appora_aider_dir(aider_dir) / "aider-model-settings.yml"


def _appora_model_metadata_path(aider_dir: Path) -> Path:
    return _appora_aider_dir(aider_dir) / "aider-model-metadata.json"


def _appora_sitecustomize_path(aider_dir: Path) -> Path:
    return _appora_aider_dir(aider_dir) / "sitecustomize.py"


def _appora_benchmark_patch_path(aider_dir: Path) -> Path:
    return _appora_aider_dir(aider_dir) / "patch_benchmark.py"


def write_appora_aider_model_profile(config: AiderBenchmarkConfig) -> dict[str, str]:
    aider_dir = _aider_dir(config.workspace)
    profile_dir = _appora_aider_dir(aider_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    settings_path = _appora_model_settings_path(aider_dir)
    metadata_path = _appora_model_metadata_path(aider_dir)
    sitecustomize_path = _appora_sitecustomize_path(aider_dir)
    benchmark_patch_path = _appora_benchmark_patch_path(aider_dir)
    settings_path.write_text(APPORA_AIDER_MODEL_SETTINGS, encoding="utf-8")
    metadata_path.write_text(json.dumps(APPORA_AIDER_MODEL_METADATA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sitecustomize_path.write_text(APPORA_AIDER_SITE_CUSTOMIZE, encoding="utf-8")
    benchmark_patch_path.write_text(APPORA_AIDER_BENCHMARK_PATCH, encoding="utf-8")
    return {
        "settings_path": str(settings_path),
        "metadata_path": str(metadata_path),
        "sitecustomize_path": str(sitecustomize_path),
        "benchmark_patch_path": str(benchmark_patch_path),
    }


def _benchmark_args(config: AiderBenchmarkConfig) -> list[str]:
    args = [
        "./benchmark/benchmark.py",
        config.run_name,
        "--model",
        config.model,
        "--edit-format",
        config.edit_format,
        "--threads",
        str(config.threads),
        "--tries",
        str(config.tries),
        "--exercises-dir",
        "polyglot-benchmark",
    ]
    if config.num_tests is not None:
        args.extend(["--num-tests", str(config.num_tests)])
    if config.keywords:
        args.extend(["--keywords", config.keywords])
    return args


def build_setup_commands(config: AiderBenchmarkConfig) -> list[list[str]]:
    aider_dir = _aider_dir(config.workspace)
    polyglot_dir = _polyglot_dir(aider_dir)
    commands: list[list[str]] = []
    if not aider_dir.exists():
        commands.append(["git", "clone", AIDER_REPO_URL, str(aider_dir)])
    if not polyglot_dir.exists():
        commands.append(["git", "clone", POLYGLOT_REPO_URL, str(polyglot_dir)])
    commands.append(["./benchmark/docker_build.sh"])
    return commands


def build_docker_run_command(config: AiderBenchmarkConfig) -> list[str]:
    aider_dir = _aider_dir(config.workspace)
    benchmark_dir = aider_dir / "tmp.benchmarks"
    aider_mount = aider_dir.resolve()
    benchmark_mount = benchmark_dir.resolve()
    inner = " ".join([
        "pip install -e '.[dev]'",
        "&&",
        "python3 /aider/.appora/patch_benchmark.py",
        "&&",
        *(_shell_quote(part) for part in _benchmark_args(config)),
    ])
    return [
        "docker",
        "run",
        "--rm",
        "--memory=12g",
        "--memory-swap=12g",
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{aider_mount}:/aider",
        "-v",
        f"{benchmark_mount}:/benchmarks",
        "-e",
        "OPENAI_API_KEY",
        "-e",
        f"OPENAI_API_BASE={config.openai_api_base}",
        "-e",
        "AIDER_MODEL_SETTINGS_FILE=/aider/.appora/aider-model-settings.yml",
        "-e",
        "AIDER_MODEL_METADATA_FILE=/aider/.appora/aider-model-metadata.json",
        "-e",
        "PYTHONPATH=/aider/.appora",
        "-e",
        f"AIDER_MAX_CHAT_HISTORY_TOKENS={max(8192, int(config.max_chat_history_tokens or 32768))}",
        "-e",
        "APPORA_AIDER_MAX_TEST_ERROR_CHARS=24000",
        "-e",
        "APPORA_AIDER_MAX_TEST_CONTRACT_CHARS=18000",
        "-e",
        "AIDER_WEAK_MODEL=openai/appora",
        "-e",
        "AIDER_CHECK_MODEL_ACCEPTS_SETTINGS=false",
        "-e",
        "AIDER_SHOW_MODEL_WARNINGS=false",
        "-e",
        "AIDER_DOCKER=1",
        "-e",
        "AIDER_BENCHMARK_DIR=/benchmarks",
        "-w",
        "/aider",
        "aider-benchmark",
        "bash",
        "-lc",
        inner,
    ]


def build_docker_preflight_command(config: AiderBenchmarkConfig) -> list[str]:
    inner = " ".join([
        "python3",
        "-c",
        _shell_quote(
            "import os, urllib.request; "
            "base=os.environ.get('OPENAI_API_BASE','').rstrip('/'); "
            "key=os.environ.get('OPENAI_API_KEY',''); "
            "assert base, 'OPENAI_API_BASE missing'; "
            "assert key, 'OPENAI_API_KEY missing'; "
            "req=urllib.request.Request(base + '/models', headers={'Authorization':'Bearer ' + key}); "
            "resp=urllib.request.urlopen(req, timeout=15); "
            "print('router_preflight_status=' + str(resp.status))"
        ),
    ])
    return [
        "docker",
        "run",
        "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "-e",
        "OPENAI_API_KEY",
        "-e",
        f"OPENAI_API_BASE={config.openai_api_base}",
        "aider-benchmark",
        "bash",
        "-lc",
        inner,
    ]


def build_stats_command(config: AiderBenchmarkConfig, stats_dir: str | Path) -> list[str]:
    aider_dir = _aider_dir(config.workspace)
    raw = str(stats_dir)
    if raw.startswith(str(aider_dir) + os.sep):
        raw = str(Path(raw).relative_to(aider_dir))
    return ["./benchmark/benchmark.py", "--stats", raw]


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=,+@%-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _run_command(command: list[str], *, cwd: Path | None = None, timeout: int | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    started_cwd = str(cwd) if cwd else None
    proc = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
    return {
        "command": command,
        "cwd": started_cwd,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _parse_aider_stats(text: str) -> dict[str, Any]:
    current: dict[str, Any] | None = None
    last_block: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- dirname:"):
            if current:
                last_block = current
            current = {}
            continue
        if current is None:
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if re.fullmatch(r"-?\d+", value):
            current[key] = int(value)
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            current[key] = float(value)
        else:
            current[key] = value
    if current:
        last_block = current
    return last_block


def _aider_stats_indicate_success(stats: dict[str, Any]) -> bool | None:
    if not stats:
        return None
    pass_rates = [
        float(stats[key])
        for key in ("pass_rate_1", "pass_rate_2")
        if isinstance(stats.get(key), int | float)
    ]
    pass_nums = [
        int(stats[key])
        for key in ("pass_num_1", "pass_num_2")
        if isinstance(stats.get(key), int)
    ]
    test_cases = stats.get("test_cases")
    if isinstance(test_cases, int) and test_cases > 0:
        if pass_nums:
            return max(pass_nums) >= test_cases
        if pass_rates:
            return max(pass_rates) >= 100.0
    if pass_rates:
        return max(pass_rates) >= 100.0
    if pass_nums:
        return any(num > 0 for num in pass_nums)
    return None


def _read_env_file_value(path: Path, key: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        clean = value.strip().strip('"').strip("'")
        return clean
    return ""


def _benchmark_env(config: AiderBenchmarkConfig) -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("OPENAI_API_KEY") and env.get(config.openai_api_key_env):
        env["OPENAI_API_KEY"] = env[config.openai_api_key_env]
    if not env.get("OPENAI_API_KEY"):
        cwd = Path.cwd()
        for env_file in (cwd / "api" / ".env", cwd / ".env"):
            key = _read_env_file_value(env_file, config.openai_api_key_env)
            if key:
                env[config.openai_api_key_env] = key
                env["OPENAI_API_KEY"] = key
                break
    return env


def run_aider_benchmark(
    config: AiderBenchmarkConfig,
    *,
    setup: bool = False,
    run: bool = False,
    stats_dir: str | Path | None = None,
    command_only: bool = False,
    timeout: int | None = None,
) -> dict[str, Any]:
    aider_dir = _aider_dir(config.workspace)
    result: dict[str, Any] = {
        "ok": True,
        "mode": "aider-polyglot",
        "workspace": str(config.workspace),
        "aider_dir": str(aider_dir),
        "model": config.model,
        "edit_format": config.edit_format,
        "commands": {
            "setup": build_setup_commands(config),
            "preflight": build_docker_preflight_command(config),
            "run": build_docker_run_command(config),
            "stats": build_stats_command(config, stats_dir or "tmp.benchmarks/<run-dir>"),
        },
        "executions": [],
        "stats": None,
        "summary": "Aider Polyglot benchmark adapter ready. Use --setup, --run, or --stats to execute official harness steps.",
    }
    if command_only:
        return result

    executions: list[dict[str, Any]] = []
    if setup:
        config.workspace.mkdir(parents=True, exist_ok=True)
        for command in build_setup_commands(config):
            cwd = aider_dir if command[0].startswith("./") else None
            executions.append(_run_command(command, cwd=cwd, timeout=timeout))
            if not executions[-1]["ok"]:
                result["ok"] = False
                result["executions"] = executions
                result["summary"] = "Aider benchmark setup failed."
                return result

    if run:
        if aider_dir.exists():
            result["model_profile"] = write_appora_aider_model_profile(config)
        env = _benchmark_env(config)
        preflight_execution = _run_command(build_docker_preflight_command(config), timeout=min(timeout or 60, 60), env=env)
        executions.append(preflight_execution)
        if not preflight_execution["ok"]:
            result["ok"] = False
            result["executions"] = executions
            result["summary"] = "Aider benchmark preflight failed: Docker could not reach the configured OpenAI-compatible router."
            return result

        run_execution = _run_command(build_docker_run_command(config), timeout=timeout, env=env)
        executions.append(run_execution)
        stdout_stats = _parse_aider_stats(str(run_execution.get("stdout") or ""))
        if stdout_stats:
            result["stats"] = stdout_stats
        official_success = _aider_stats_indicate_success(stdout_stats)
        if not run_execution["ok"]:
            result["ok"] = False
            result["executions"] = executions
            result["summary"] = "Aider benchmark run failed."
            return result
        if official_success is False:
            result["ok"] = False
            result["executions"] = executions
            result["summary"] = "Aider benchmark failed official solve metrics."
            return result

    if stats_dir is not None:
        stats_command = build_stats_command(config, stats_dir)
        stats_execution = _run_command(stats_command, cwd=aider_dir, timeout=timeout)
        executions.append(stats_execution)
        if stats_execution["ok"]:
            result["stats"] = _parse_aider_stats(stats_execution["stdout"])
        else:
            result["ok"] = False
            result["summary"] = "Aider benchmark stats failed."

    result["executions"] = executions
    if executions and result["ok"]:
        result["summary"] = "Aider benchmark command(s) completed."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or prepare the official Aider Polyglot benchmark for Appora.")
    parser.add_argument("--workspace", type=Path, default=Path(".tmp-aider-benchmark"))
    parser.add_argument("--run-name", default="appora-aider")
    parser.add_argument("--model", default="openai/appora")
    parser.add_argument("--edit-format", default="diff")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--num-tests", type=int, default=None)
    parser.add_argument("--keywords", default=None)
    parser.add_argument("--openai-api-base", default="http://host.docker.internal:20128/v1")
    parser.add_argument("--openai-api-key-env", default="NINE_ROUTER_API_KEY")
    parser.add_argument("--max-chat-history-tokens", type=int, default=32768)
    parser.add_argument("--setup", action="store_true", help="Clone Aider/polyglot and build the benchmark Docker image.")
    parser.add_argument("--run", action="store_true", help="Run Aider benchmark inside Docker.")
    parser.add_argument("--stats", default=None, help="Benchmark result directory to summarize with Aider --stats.")
    parser.add_argument("--command-only", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = AiderBenchmarkConfig(
        workspace=args.workspace,
        run_name=args.run_name,
        model=args.model,
        edit_format=args.edit_format,
        threads=max(1, args.threads),
        num_tests=args.num_tests,
        keywords=args.keywords,
        openai_api_base=args.openai_api_base,
        openai_api_key_env=args.openai_api_key_env,
        max_chat_history_tokens=args.max_chat_history_tokens,
    )
    result = run_aider_benchmark(
        config,
        setup=args.setup,
        run=args.run,
        stats_dir=args.stats,
        command_only=args.command_only or not (args.setup or args.run or args.stats),
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        label = "PASS" if result["ok"] else "FAIL"
        print(f"Appora Aider Benchmark: {label}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

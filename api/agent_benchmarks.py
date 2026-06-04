from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import queue
import signal
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api import main as main_mod
from api import settings as settings_mod
from api.agent_observability import build_agent_observability
from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE
from api.hybrid import build_hybrid_seed
from api.oauth_runtime import NINE_ROUTER_PROVIDER, auth_snapshot, test_nine_router_route
from api.project_templates import render_project_template


@dataclass(frozen=True)
class AgentBenchmarkScenario:
    id: str
    prompt: str
    template_id: str
    project_root: str
    active_file: str
    open_files: tuple[str, ...]
    min_score: int = 70


AGENT_BENCHMARK_SCENARIOS: tuple[AgentBenchmarkScenario, ...] = (
    AgentBenchmarkScenario(
        id="task_tracker_ui",
        prompt=(
            "Bikin dashboard task tracker profesional untuk tim produk. "
            "Harus ada daftar task, prioritas, owner, status progress, ringkasan metrik, "
            "state kosong yang masuk akal, dan jalankan validasi/build kalau perlu."
        ),
        template_id="blank",
        project_root="benchmark-task-tracker-ui",
        active_file="src/App.tsx",
        open_files=("src/App.tsx", "src/app.css", "package.json"),
        min_score=80,
    ),
    AgentBenchmarkScenario(
        id="preview_blank_repair",
        prompt=(
            "Preview halaman ini blank putih. Cari penyebabnya, perbaiki sampai tampil, "
            "dan validasi dengan build atau preview audit."
        ),
        template_id="portfolio",
        project_root="benchmark-preview-blank-repair",
        active_file="src/App.tsx",
        open_files=("src/App.tsx", "src/pages/Home.tsx", "src/app.css", "package.json"),
        min_score=80,
    ),
    AgentBenchmarkScenario(
        id="nontechnical_landing",
        prompt=(
            "Aku user awam, bikinin landing page jasa laundry premium yang kelihatan siap produksi. "
            "Copy jangan kebanyakan, tombol jelas, ada harga, testimoni, dan booking CTA."
        ),
        template_id="landing-pricing",
        project_root="benchmark-nontechnical-landing",
        active_file="src/pages/Home.tsx",
        open_files=("src/pages/Home.tsx", "src/app.css", "package.json"),
        min_score=75,
    ),
)


class AgentBenchmarkTimeout(RuntimeError):
    pass


class _ScenarioTimeout:
    def __init__(self, seconds: int | None) -> None:
        self.seconds = int(seconds or 0)
        self._previous_handler: Any = None
        self._previous_timer: tuple[float, float] | None = None
        self.enabled = False

    def __enter__(self) -> "_ScenarioTimeout":
        if self.seconds <= 0 or threading.current_thread() is not threading.main_thread():
            return self
        self.enabled = True
        self._previous_handler = signal.getsignal(signal.SIGALRM)
        self._previous_timer = signal.getitimer(signal.ITIMER_REAL)

        def _raise_timeout(_signum: int, _frame: Any) -> None:
            raise AgentBenchmarkTimeout(f"Scenario exceeded {self.seconds}s timeout")

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, float(self.seconds))
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self.enabled:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if self._previous_handler is not None:
                signal.signal(signal.SIGALRM, self._previous_handler)
            if self._previous_timer and self._previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, self._previous_timer[0], self._previous_timer[1])
        return False


def _scenario_catalog(scenario_ids: set[str] | None = None) -> list[AgentBenchmarkScenario]:
    scenarios = list(AGENT_BENCHMARK_SCENARIOS)
    if scenario_ids is not None:
        scenarios = [scenario for scenario in scenarios if scenario.id in scenario_ids]
    unknown = sorted((scenario_ids or set()) - {scenario.id for scenario in AGENT_BENCHMARK_SCENARIOS})
    if unknown:
        raise ValueError(f"Unknown benchmark scenario(s): {', '.join(unknown)}")
    return scenarios


def _write_benchmark_project(workspace: Path, scenario: AgentBenchmarkScenario) -> Path:
    project_dir = workspace / scenario.project_root
    if project_dir.exists():
        shutil.rmtree(project_dir)
    files = render_project_template(
        template_id=scenario.template_id,
        project_root=scenario.project_root,
        project_name=scenario.id.replace("_", " ").title(),
    )
    if not files:
        seeded = build_hybrid_seed(
            project_root=scenario.project_root,
            project_name=scenario.id.replace("_", " ").title(),
            instruction=scenario.prompt,
        )
        prefix = f"{scenario.project_root.strip('/')}/"
        files = {
            (path[len(prefix):] if path.startswith(prefix) else path): content
            for path, content in seeded.items()
        }
    project_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        target = project_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project_dir


def _score_live_result(result: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    changes = result.get("changes") if isinstance(result.get("changes"), list) else []
    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    verifier_failures = trace.get("verification") if isinstance(trace.get("verification"), list) else []
    failed_verification = [item for item in verifier_failures if isinstance(item, dict) and not item.get("ok", True)]

    score = 0
    if changes:
        score += 25
    if actions:
        score += 15
    if execution.get("ok") is True:
        score += 35
        if changes or actions:
            score += 5
    elif execution.get("ok") is False:
        score -= 25
    if events:
        score += 10
    if any(item.get("event") in {"tool_call", "command_start"} for item in events):
        score += 10
    if not failed_verification:
        score += 5
    score = max(0, min(100, score))

    return {
        "score": score,
        "metrics": {
            "changes": len(changes),
            "actions": len(actions),
            "execution_ok": execution.get("ok"),
            "event_count": len(events),
            "tool_event_count": sum(1 for item in events if item.get("event") in {"tool_call", "tool_output", "command_start", "command_output"}),
            "failed_verification": len(failed_verification),
            "task_state": trace.get("task_state") if isinstance(trace.get("task_state"), dict) else {},
        },
    }


def _safe_route_test_payload(route: dict[str, Any]) -> dict[str, Any]:
    allowed = {"ok", "status", "summary", "model", "resolved_model", "response"}
    return {key: route.get(key) for key in allowed if key in route}


def _nine_router_benchmark_readiness(*, check_route: bool = False, model: str | None = None) -> dict[str, Any]:
    selected_model = (model or settings_mod.settings.nine_router_model or "free-forever").strip() or "free-forever"
    snapshot = auth_snapshot()
    status = snapshot.get(NINE_ROUTER_PROVIDER) if isinstance(snapshot, dict) else {}
    status = status if isinstance(status, dict) else {}
    connected = bool(status.get("connected"))
    source = status.get("source")
    readiness: dict[str, Any] = {
        "ok": connected,
        "provider": NINE_ROUTER_PROVIDER,
        "connected": connected,
        "model": selected_model,
        "source": source,
        "auth_type": status.get("auth_type"),
        "managed_free": bool(status.get("managed_free")),
        "base_url": status.get("base_url") or settings_mod.settings.nine_router_base_url,
        "route_test": None,
        "summary": (
            "9Router ready for live benchmark."
            if connected
            else "9Router belum connected. Isi endpoint/API key 9Router atau aktifkan Appora managed free router sebelum live benchmark."
        ),
    }
    if connected and check_route:
        route = test_nine_router_route(model=selected_model)
        readiness["route_test"] = _safe_route_test_payload(route if isinstance(route, dict) else {})
        readiness["ok"] = bool(readiness["route_test"].get("ok"))
        readiness["summary"] = str(readiness["route_test"].get("summary") or readiness["summary"])
    return readiness


def _run_live_scenario(workspace: Path, scenario: AgentBenchmarkScenario, *, timeout_seconds: int | None = 180) -> dict[str, Any]:
    _write_benchmark_project(workspace, scenario)
    session_id = f"benchmark-{scenario.id}-{int(time.time() * 1000)}"
    user_id = f"benchmark-user-{scenario.id}"
    STATE["sessions"][session_id] = {
        "workspace": str(workspace),
        "runners": {},
        "agent_jobs": {},
        "oauth_pending": {},
        "google_user": None,
        "hydrated_projects": set(),
    }
    session_token = CURRENT_SESSION_ID.set(session_id)
    user_token = CURRENT_USER_ID.set(user_id)
    events: list[dict[str, Any]] = []
    started = time.monotonic()

    def emit(event: str, data: dict[str, Any]) -> None:
        if event not in {"status", "delta", "tool_call", "tool_output", "command_start", "command_output", "done", "error"}:
            return
        payload = dict(data or {})
        events.append(
            {
                "t": round(time.monotonic() - started, 3),
                "event": event,
                "phase": payload.get("phase"),
                "message": payload.get("message") or payload.get("text") or payload.get("summary"),
            }
        )

    try:
        req = main_mod.AgentReq(
            input=scenario.prompt,
            project_root=scenario.project_root,
            build_mode="full-agent",
            active_file=scenario.active_file,
            open_files=list(scenario.open_files),
            auto_execute=True,
            stream=False,
        )
        with _ScenarioTimeout(timeout_seconds):
            result = main_mod._run_agent_impl(req, event_cb=emit)
        observability = result.get("observability") if isinstance(result, dict) and isinstance(result.get("observability"), dict) else build_agent_observability(
            [{"event_type": item.get("event"), "payload": {"phase": item.get("phase"), "message": item.get("message")}, "created_at": item.get("t")} for item in events],
            result=result if isinstance(result, dict) else {},
        )
        scored = _score_live_result(result if isinstance(result, dict) else {}, events)
        ok = scored["score"] >= scenario.min_score and scored["metrics"]["execution_ok"] is True
        return {
            "id": scenario.id,
            "ok": ok,
            "status": "passed" if ok else "failed",
            "score": scored["score"],
            "min_score": scenario.min_score,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "project_root": scenario.project_root,
            "metrics": scored["metrics"],
            "observability": observability,
            "events": events[-20:],
            "spoken_excerpt": str((result or {}).get("spoken") or "")[:500] if isinstance(result, dict) else "",
        }
    except Exception as exc:
        error_text = str(exc)
        is_timeout = isinstance(exc, AgentBenchmarkTimeout) or "Scenario exceeded" in error_text
        return {
            "id": scenario.id,
            "ok": False,
            "status": "timeout" if is_timeout else "error",
            "score": 0,
            "min_score": scenario.min_score,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "project_root": scenario.project_root,
            "metrics": {
                "changes": 0,
                "actions": 0,
                "execution_ok": False,
                "event_count": len(events),
                "tool_event_count": sum(1 for item in events if item.get("event") in {"tool_call", "tool_output", "command_start", "command_output"}),
                "failed_verification": 1,
                "task_state": {},
            },
            "observability": build_agent_observability(
                [{"event_type": item.get("event"), "payload": {"phase": item.get("phase"), "message": item.get("message")}, "created_at": item.get("t")} for item in events],
                result={"execution": {"ok": False}, "trace": {"verification": [{"ok": False, "check": "benchmark-exception", "message": str(exc)[:1000]}]}},
            ),
            "events": events[-20:],
            "error": error_text[:1000],
            "timeout_seconds": timeout_seconds if is_timeout else None,
        }
    finally:
        CURRENT_USER_ID.reset(user_token)
        CURRENT_SESSION_ID.reset(session_token)


def _scenario_timeout_result(scenario: AgentBenchmarkScenario, *, timeout_seconds: int | None, duration_ms: int = 0, error: str | None = None) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "ok": False,
        "status": "timeout",
        "score": 0,
        "min_score": scenario.min_score,
        "duration_ms": duration_ms,
        "project_root": scenario.project_root,
        "metrics": {
            "changes": 0,
            "actions": 0,
            "execution_ok": False,
            "event_count": 0,
            "tool_event_count": 0,
            "failed_verification": 1,
            "task_state": {},
        },
        "observability": {
            "ok": False,
            "summary": {
                "event_count": 0,
                "tool_call_count": 0,
                "tool_output_count": 0,
                "command_count": 0,
                "failed_command_count": 0,
                "changes": 0,
                "actions": 0,
                "execution_ok": False,
                "duration_ms": duration_ms,
            },
            "timeline": [],
            "commands": [],
            "failure_points": [{"kind": "timeout", "phase": "benchmark", "detail": error or f"Scenario exceeded {timeout_seconds}s timeout"}],
        },
        "events": [{"t": round(duration_ms / 1000, 3), "event": "error", "phase": "benchmark", "message": error or f"Scenario exceeded {timeout_seconds}s timeout"}],
        "error": error or f"Scenario exceeded {timeout_seconds}s timeout",
        "timeout_seconds": timeout_seconds,
    }


def _live_scenario_worker(workspace_raw: str, scenario: AgentBenchmarkScenario, timeout_seconds: int | None, out_queue: Any) -> None:
    try:
        result = _run_live_scenario(Path(workspace_raw), scenario, timeout_seconds=timeout_seconds)
        out_queue.put({"ok": True, "result": result})
    except BaseException as exc:
        out_queue.put({"ok": False, "error": str(exc)[:1000]})


def _run_live_scenario_isolated(workspace: Path, scenario: AgentBenchmarkScenario, *, timeout_seconds: int | None = 180) -> dict[str, Any]:
    if not timeout_seconds or timeout_seconds <= 0:
        return _run_live_scenario(workspace, scenario, timeout_seconds=timeout_seconds)
    try:
        ctx = mp.get_context("fork")
    except ValueError:
        return _run_live_scenario(workspace, scenario, timeout_seconds=timeout_seconds)
    out_queue: Any = ctx.Queue(maxsize=1)
    started = time.monotonic()
    proc = ctx.Process(target=_live_scenario_worker, args=(str(workspace), scenario, timeout_seconds, out_queue), daemon=True)
    proc.start()
    proc.join(float(timeout_seconds) + 2.0)
    duration_ms = int((time.monotonic() - started) * 1000)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
        return _scenario_timeout_result(scenario, timeout_seconds=timeout_seconds, duration_ms=duration_ms)
    try:
        payload = out_queue.get_nowait()
    except queue.Empty:
        if proc.exitcode == 0:
            return _scenario_timeout_result(scenario, timeout_seconds=timeout_seconds, duration_ms=duration_ms, error="Scenario worker exited without a result")
        return _scenario_timeout_result(scenario, timeout_seconds=timeout_seconds, duration_ms=duration_ms, error=f"Scenario worker exited with code {proc.exitcode}")
    if isinstance(payload, dict) and payload.get("ok") and isinstance(payload.get("result"), dict):
        return payload["result"]
    return _scenario_timeout_result(
        scenario,
        timeout_seconds=timeout_seconds,
        duration_ms=duration_ms,
        error=str((payload or {}).get("error") or "Scenario worker failed"),
    )


def run_agent_benchmark_suite(
    *,
    live: bool = False,
    scenario_ids: list[str] | tuple[str, ...] | None = None,
    workspace_root: Path | None = None,
    check_route: bool = False,
    require_ready: bool = True,
    output_path: Path | None = None,
    max_scenarios: int | None = None,
    scenario_timeout_seconds: int | None = 180,
    isolate_scenarios: bool = True,
) -> dict[str, Any]:
    selected = _scenario_catalog(set(scenario_ids) if scenario_ids else None)
    if max_scenarios is not None and max_scenarios > 0:
        selected = selected[:max_scenarios]
    if not live:
        scenarios = [
            {
                "id": scenario.id,
                "status": "ready",
                "prompt": scenario.prompt,
                "template_id": scenario.template_id,
                "project_root": scenario.project_root,
                "min_score": scenario.min_score,
            }
            for scenario in selected
        ]
        result = {"ok": True, "mode": "dry-run", "scenarios": scenarios}
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    readiness = _nine_router_benchmark_readiness(check_route=check_route)
    started_at = int(time.time())
    if require_ready and not readiness.get("ok"):
        result = {
            "ok": False,
            "mode": "live",
            "blocked": True,
            "readiness": readiness,
            "started_at": started_at,
            "completed_at": int(time.time()),
            "scenarios": [],
            "summary": "Live benchmark ditahan karena 9Router belum siap. Tidak ada LLM call agent yang dijalankan.",
        }
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    scenario_runner = _run_live_scenario_isolated if isolate_scenarios else _run_live_scenario
    if workspace_root is not None:
        workspace_root.mkdir(parents=True, exist_ok=True)
        results = [scenario_runner(workspace_root, scenario, timeout_seconds=scenario_timeout_seconds) for scenario in selected]
    else:
        with tempfile.TemporaryDirectory(prefix="appora-agent-benchmark-") as tmp:
            workspace = Path(tmp)
            results = [scenario_runner(workspace, scenario, timeout_seconds=scenario_timeout_seconds) for scenario in selected]
    result = {
        "ok": all(item.get("ok") for item in results),
        "mode": "live",
        "blocked": False,
        "readiness": readiness,
        "scenario_timeout_seconds": scenario_timeout_seconds,
        "started_at": started_at,
        "completed_at": int(time.time()),
        "scenarios": results,
        "summary": f"Ran {len(results)} live benchmark scenario(s) through 9Router readiness gate.",
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Appora live agent benchmark scenarios.")
    parser.add_argument("--live", action="store_true", help="Run real agent calls. Without this, only prints benchmark catalog.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--scenario", action="append", dest="scenarios", help="Scenario id to run. Can be passed more than once.")
    parser.add_argument("--workspace", type=Path, default=None, help="Workspace directory for live benchmark projects.")
    parser.add_argument("--output", type=Path, default=None, help="Write benchmark report JSON to this path.")
    parser.add_argument("--max-scenarios", type=int, default=None, help="Run at most this many selected scenarios.")
    parser.add_argument("--scenario-timeout", type=int, default=180, help="Max seconds per live scenario before marking it failed.")
    parser.add_argument("--check-route", action="store_true", help="Run a tiny 9Router /chat/completions preflight before live benchmark.")
    parser.add_argument("--skip-route-test", action="store_true", help="Skip live 9Router route preflight even when --live is used.")
    parser.add_argument("--allow-unready", action="store_true", help="Run scenarios even if 9Router readiness check fails.")
    args = parser.parse_args()

    result = run_agent_benchmark_suite(
        live=args.live,
        scenario_ids=args.scenarios,
        workspace_root=args.workspace,
        check_route=args.check_route or (args.live and not args.skip_route_test),
        require_ready=not args.allow_unready,
        output_path=args.output,
        max_scenarios=args.max_scenarios,
        scenario_timeout_seconds=args.scenario_timeout,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        label = "PASS" if result["ok"] else "FAIL"
        print(f"Appora Agent Benchmark: {label} ({result['mode']})")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request

from api.aider_benchmarks import AiderBenchmarkConfig, _benchmark_env, _parse_aider_stats, _run_command, build_docker_preflight_command, build_docker_run_command, build_setup_commands, run_aider_benchmark, write_appora_aider_model_profile
from api.agent_intent import AgentIntent, classify_agent_intent
from api.agent_benchmarks import AGENT_BENCHMARK_SCENARIOS, _score_live_result, run_agent_benchmark_suite
from api.agent_evals import run_appora_contract_eval, run_memory_failure_recall_eval, validate_template_registry
from api.agent_observability import build_agent_observability
from api.agent_planner import build_long_horizon_plan, update_long_horizon_progress
from api import agent as agent_mod
from api import agent_runtime as agent_runtime_mod
from api import main as main_mod
from api.agent_mcp import MCPServerInfo, MCPToolCallResult, MCPToolInfo, discover_mcp_servers, execute_mcp_tool, suggest_mcp_actions
from api.agent_memory import get_agent_memory_overview, remember_agent_run, retrieve_agent_memory
from api.agent_runtime import AgentDriver, AgentRunController, _autonomous_continue_node, _compact_no_work_context, _deep_preflight_node, _draft_node, _finalize_node, _intent_with_active_work_context, _is_no_work_recovery, _looks_like_plan_only_reply, _max_tool_loops_for_run, _plan_node, _remember_project_work_state, _route_after_strict_retry, _route_after_verify, _should_finalize_to_emergency_fallback, _should_run_deep_preflight, _should_run_readonly_scout, _should_run_refinement, _strict_agentic_retry_node, _verify_node, get_agent_mode_profile, prepare_agent_context, run_agent_pipeline
from api.agent_editing import assess_edit_strategy
from api.agent_skills import build_validation_plan, detect_project_stack, resolve_agent_skills
from api.agent_tools import execute_local_tool
from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE
from api.auth.identity import AuthenticatedUser
from api.fs import safe_join
from api.hybrid import build_hybrid_seed
from api.projects.templates import list_project_templates, render_project_template
from api.projects.store import ProjectCreateReq, ProjectDuplicateReq, create_project, duplicate_project, list_projects, save_project_snapshot
from api.main import ApplyManyReq, WriteOp, _browser_preview_audit_ready, _build_preview_audit_result, _build_quality_checks, _command_policy_decision, _extract_preview_snapshot_from_html, _filter_visual_text_overflow_nodes, _preflight_apply_many, _run_agent_browser_preview_audit, _sha256_text, agent_capabilities, fs_apply_many, supabase_rag_status
from api.preferences.store import UserPreferencesRecord
from api.preferences.router import build_preferences_router
from api.storage.secrets import get_provider_secret, has_provider_secret
from api.settings import load_settings
from api.config.router import SettingsUpdateReq, build_settings_router
from api.storage.supabase import upsert_profile
from api.oauth_runtime import CURRENT_PROFILE_ID, list_models as list_provider_models, provider_catalog


class AgentIntentRegressionTests(unittest.TestCase):
    def test_intent_boundaries(self) -> None:
        cases = [
            ("fix navbar spacing and add loading state", "command", True, True),
            ("audit code agent dari graph rag dan lain lain laporin ke gw", "inspection", False, False),
            ("jelasin flow graph agent ini", "inspection", False, False),
            ("gimana statusnya bro?", "conversation", False, False),
            ("review lalu perbaiki auth flow ini", "command", True, True),
            ("button button juga norak banget ya", "command", True, True),
            ("rombak UI preview biar senada", "command", True, True),
            ("preview blank putih", "command", True, True),
            ("kenapa preview blank putih?", "command", True, True),
            ("Aku user awam, bikinin landing page jasa laundry premium yang siap produksi", "command", True, True),
            ("Debug the existing retry helper in src/retry.py only. It currently attempts one fewer time than max_attempts.", "command", True, True),
        ]
        for prompt, expected_kind, should_write, should_tools in cases:
            with self.subTest(prompt=prompt):
                intent = classify_agent_intent(prompt, build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
                self.assertEqual(intent.kind, expected_kind)
                self.assertEqual(intent.should_write_files, should_write)
                self.assertEqual(intent.should_run_tools, should_tools)

    def test_short_greetings_stay_conversational(self) -> None:
        for prompt in ["hi", "hello", "hei", "hai", "halo", "p", "bro", "gas", "lanjut"]:
            with self.subTest(prompt=prompt):
                intent = classify_agent_intent(prompt, build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
                self.assertEqual(intent.kind, "conversation")
                self.assertFalse(intent.should_write_files)

    def test_questions_about_agent_do_not_trigger_file_writes(self) -> None:
        cases = [
            "agent udah bisa bedain mana interaksi mana intruksi?",
            "perbedaan new workspace sama new project itu apa dah",
            "kenapa provider free masih kena billing?",
            "apa maksudnya mcp tools?",
            "gimana cara jalaninnya?",
        ]
        for prompt in cases:
            with self.subTest(prompt=prompt):
                intent = classify_agent_intent(prompt, build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
                self.assertEqual(intent.kind, "conversation")
                self.assertFalse(intent.should_write_files)

    def test_followup_only_becomes_command_when_it_has_work_object(self) -> None:
        intent = classify_agent_intent("gas fix navbar spacing", build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
        self.assertEqual(intent.kind, "command")
        self.assertTrue(intent.should_write_files)

        vague = classify_agent_intent("gas", build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
        self.assertEqual(vague.kind, "conversation")
        self.assertFalse(vague.should_write_files)

    def test_elongated_continuation_followup_can_continue_build_work(self) -> None:
        for prompt in ["lanjuttt", "terusss", "next"]:
            with self.subTest(prompt=prompt):
                intent = classify_agent_intent(prompt, build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
                self.assertIn(intent.kind, {"command", "mixed"})
                self.assertTrue(intent.should_write_files)

    def test_active_work_state_promotes_short_continuation(self) -> None:
        session_id = "test-session-active-continuation"
        user_id = "test-user-active-continuation"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        user_token = CURRENT_USER_ID.set(user_id)
        try:
            base = classify_agent_intent("lanjut", build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
            self.assertEqual(base.kind, "conversation")
            self.assertFalse(base.should_write_files)

            inherited, context = _intent_with_active_work_context(base, text="lanjut", project_root="demo", build_mode="full-agent")
            self.assertEqual(inherited.kind, "conversation")
            self.assertFalse(inherited.should_write_files)
            self.assertIsNone(context)

            write_intent = classify_agent_intent(
                "buat UI settings 9Router",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
            )
            _remember_project_work_state(
                project_root="demo",
                build_mode="full-agent",
                user_input="buat UI settings 9Router",
                spoken="Saya sudah mulai ubah settings.",
                changes=[{"path": "demo/src/App.tsx", "new_content": "export default function App() { return null }"}],
                actions=[{"type": "shell", "command": "npm run build"}],
                intent=write_intent,
                task_state={
                    "goal": "buat UI settings 9Router",
                    "status": "blocked",
                    "next_action": "Repair verifier failure: relative-imports-resolve",
                    "blocking_checks": ["relative-imports-resolve"],
                    "nodes": [
                        {"id": "01-scope", "stage": "scope", "title": "Define task boundary", "status": "done", "detail": "Scope task."},
                        {"id": "02-verify", "stage": "verify", "title": "Plan validation", "status": "blocked", "detail": "Import masih missing."},
                    ],
                },
            )

            inherited, context = _intent_with_active_work_context(base, text="lanjut", project_root="demo", build_mode="full-agent")
            self.assertEqual(inherited.kind, "command")
            self.assertTrue(inherited.should_write_files)
            self.assertTrue(inherited.should_run_tools)
            self.assertIn("ACTIVE WORK CONTINUATION", context or "")
            self.assertIn("buat UI settings 9Router", context or "")
            self.assertIn("demo/src/App.tsx", context or "")
            self.assertIn("Task status: blocked", context or "")
            self.assertIn("Next action: Repair verifier failure: relative-imports-resolve", context or "")
            self.assertIn("verify=blocked", context or "")

            _remember_project_work_state(
                project_root="demo",
                build_mode="full-agent",
                user_input="buat UI settings 9Router",
                spoken="Backend sudah apply dan validasi selesai.",
                changes=[{"path": "demo/src/App.tsx", "new_content": "export default function App() { return null }"}],
                actions=[{"type": "shell", "command": "npm run build"}],
                intent=write_intent,
                task_state={
                    "goal": "buat UI settings 9Router",
                    "status": "blocked",
                    "next_action": "Fix preview warning",
                    "blocking_checks": ["preview-warning"],
                },
                completion_report={
                    "ok": False,
                    "state": "blocked",
                    "summary": "Blocked: preview still failing.",
                    "criteria": [
                        {"label": "apply", "status": "passed", "detail": "applied=true count=1"},
                        {"label": "preview", "status": "failed", "detail": "blocking=1 warnings=0"},
                    ],
                    "residual_risks": ["Preview audit masih blocking."],
                },
                failure_analysis={
                    "current_signature": "abc123",
                    "primary_failure": "preview audit failed: mobile-overflow",
                    "suggested_next_move": "Fix responsive overflow then rerun preview audit.",
                    "evidence_excerpt": "Element .toolbar overflows mobile viewport by 96px.",
                    "failures": [
                        {
                            "kind": "preview_audit",
                            "category": "mobile-overflow",
                            "marker": "toolbar overflow",
                            "excerpt": "Element .toolbar overflows mobile viewport by 96px.",
                        },
                    ],
                    "repeated_failure": False,
                },
            )

            inherited, context = _intent_with_active_work_context(base, text="next", project_root="demo", build_mode="full-agent")
            self.assertEqual(inherited.kind, "command")
            self.assertIn("Last execution state: blocked", context or "")
            self.assertIn("Last execution summary: Blocked: preview still failing.", context or "")
            self.assertIn("Completion criteria: apply=passed, preview=failed", context or "")
            self.assertIn("Residual risks: Preview audit masih blocking.", context or "")
            self.assertIn("Last failure: preview audit failed: mobile-overflow", context or "")
            self.assertIn("Suggested next move: Fix responsive overflow then rerun preview audit.", context or "")
            self.assertIn("Failure evidence excerpt: Element .toolbar overflows mobile viewport by 96px.", context or "")
            self.assertIn("Failure evidence pack: preview_audit:toolbar overflow", context or "")
            self.assertIn("Continuation directive: first inspect or rerun the evidence", context or "")
            self.assertIn("last failing criterion (preview)", context or "")
            self.assertIn("Objective: Fix responsive overflow then rerun preview audit.", context or "")
        finally:
            CURRENT_USER_ID.reset(user_token)
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_persisted_active_work_state_promotes_short_continuation_after_session_state_loss(self) -> None:
        session_id = "test-session-persisted-active-continuation"
        user_id = "test-user-persisted-active-continuation"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        user_token = CURRENT_USER_ID.set(user_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws_root = Path(tmp)
                project_dir = ws_root / "demo"
                project_dir.mkdir(parents=True)
                remember_agent_run(
                    ws_root,
                    project_root="demo",
                    build_mode="full-agent",
                    interaction_kind="command",
                    user_input="Perbaiki preview blank sampai audit lolos",
                    spoken="Build sudah lolos tapi preview audit masih blocking.",
                    changes=[{"path": "demo/src/App.tsx", "new_content": "export default function App(){return <main>Ready</main>}"}],
                    actions=[{"type": "shell", "command": "npm run build"}],
                    task_state={
                        "goal": "Perbaiki preview blank sampai audit lolos",
                        "status": "blocked",
                        "next_action": "Repair verifier failure: preview-audit",
                        "blocking_checks": ["preview-audit"],
                        "horizon": {
                            "enabled": True,
                            "status": "blocked",
                            "complexity": "high",
                            "current_checkpoint": "preview",
                            "checkpoints": [
                                {"id": "context", "title": "Audit current app", "status": "done"},
                                {"id": "preview", "title": "Repair preview evidence", "status": "blocked"},
                                {"id": "validation", "title": "Run validation", "status": "pending"},
                            ],
                            "completion_criteria": [
                                "Root route renders visible product UI",
                                "Preview audit reports no blank screen",
                            ],
                        },
                        "nodes": [
                            {"id": "01-act", "stage": "act", "title": "Patch render path", "status": "done"},
                            {"id": "02-verify", "stage": "verify", "title": "Preview audit", "status": "blocked"},
                        ],
                    },
                    completion_report={
                        "ok": False,
                        "state": "blocked",
                        "summary": "Blocked: preview audit still reports blank screen.",
                        "criteria": [
                            {"label": "validation", "status": "passed"},
                            {"label": "preview", "status": "failed"},
                        ],
                    },
                    failure_analysis={
                        "primary_failure": "preview audit failed: blank screen",
                        "suggested_next_move": "Inspect root route and repair visible content.",
                    },
                    execution_outcome={
                        "ok": False,
                        "state": "blocked",
                        "summary": "Blocked: preview audit still reports blank screen.",
                        "validation_ok": True,
                        "preview_ok": False,
                        "repair_passes": 1,
                        "validation_commands": ["npm run build"],
                    },
                )

                STATE.get("sessions", {}).pop(session_id, None)
                base = classify_agent_intent("lanjut", build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
                inherited, context = _intent_with_active_work_context(
                    base,
                    text="lanjut",
                    project_root="demo",
                    build_mode="full-agent",
                    ws_root=ws_root,
                )

            self.assertEqual(inherited.kind, "command")
            self.assertTrue(inherited.should_write_files)
            self.assertIn("ACTIVE WORK CONTINUATION", context or "")
            self.assertIn("Perbaiki preview blank sampai audit lolos", context or "")
            self.assertIn("Task status: blocked", context or "")
            self.assertIn("Long-horizon checkpoints: context=done, preview=blocked, validation=pending", context or "")
            self.assertIn("Long-horizon completion criteria: Root route renders visible product UI | Preview audit reports no blank screen", context or "")
            self.assertIn("Last execution summary: Blocked: preview audit still reports blank screen.", context or "")
            self.assertIn("Suggested next move: Inspect root route and repair visible content.", context or "")
        finally:
            CURRENT_USER_ID.reset(user_token)
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_supabase_job_memory_promotes_short_continuation_when_project_profile_is_empty(self) -> None:
        session_id = "test-session-supabase-job-continuation"
        user_id = "test-user-supabase-job-continuation"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        user_token = CURRENT_USER_ID.set(user_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws_root = Path(tmp)
                (ws_root / "demo").mkdir(parents=True)
                job_row = {
                    "id": "job-1",
                    "project_root": "demo",
                    "build_mode": "full-agent",
                    "input": "Fix build failure without repeating CSS-only repair",
                    "result": {
                        "spoken": "Build masih gagal setelah repair CSS-only.",
                        "changes": [{"path": "demo/src/app.css", "new_content": "body{opacity:1}\n"}],
                        "actions": [{"type": "shell", "command": "npm run build"}],
                        "trace": {
                            "task_state": {
                                "goal": "Fix build failure without repeating CSS-only repair",
                                "status": "blocked",
                                "next_action": "Inspect root route and change implementation shape.",
                                "blocking_checks": ["preview-audit"],
                            },
                        },
                        "execution": {
                            "completion_report": {
                                "ok": False,
                                "state": "blocked",
                                "summary": "Blocked: CSS-only repair repeated the broken approach.",
                                "criteria": [{"label": "preview", "status": "failed"}],
                            },
                            "failure_analysis": {
                                "primary_failure": "preview audit failed after CSS-only repair",
                                "suggested_next_move": "Patch src/App.tsx render path instead of editing CSS again.",
                                "repeated_failure": True,
                            },
                        },
                    },
                }

                base = classify_agent_intent("lanjut", build_mode="full-agent", active_file="src/App.tsx", open_files=["src/App.tsx"])
                with patch("api.agent_memory.has_supabase", return_value=True), \
                    patch("api.agent_memory.get_latest_agent_job_result", return_value=job_row):
                    inherited, context = _intent_with_active_work_context(
                        base,
                        text="lanjut",
                        project_root="demo",
                        build_mode="full-agent",
                        ws_root=ws_root,
                    )

            self.assertEqual(inherited.kind, "command")
            self.assertIn("Fix build failure without repeating CSS-only repair", context or "")
            self.assertIn("Last execution summary: Blocked: CSS-only repair repeated the broken approach.", context or "")
            self.assertIn("Suggested next move: Patch src/App.tsx render path instead of editing CSS again.", context or "")
        finally:
            CURRENT_USER_ID.reset(user_token)
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)


class AgentRuntimeContextRegressionTests(unittest.TestCase):
    def test_tooling_node_promotes_applied_tool_changes_into_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Old</main> }\n", encoding="utf-8")

            req = SimpleNamespace(
                input="replace old with ready",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                auto_execute=False,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "input": "replace old with ready",
                "context": ctx,
                "actions": [
                    {
                        "type": "tool",
                        "tool": "search_replace_apply",
                        "arguments": {
                            "path": "demo/src/App.tsx",
                            "search": "Old",
                            "replace": "Ready",
                        },
                    }
                ],
                "tool_iterations": 0,
            }

            result = agent_runtime_mod._execute_tooling_node(state)

        self.assertIn("changes", result)
        self.assertIn("Ready", result["changes"][0]["new_content"])

    def test_agent_driver_runs_pipeline_without_legacy_static_graph(self) -> None:
        self.assertFalse(hasattr(agent_runtime_mod, "_AGENT_GRAPH"))
        self.assertIsInstance(AgentDriver(max_steps=12), AgentDriver)
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return <main>Old</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="ubah teks utama jadi New",
                project_root="demo",
                build_mode="hybrid",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                auto_execute=False,
                asset_paths=[],
            )
            suggestion = SimpleNamespace(
                spoken="Teks utama sudah diubah.",
                log="provider=test",
                changes=[{"path": "src/App.tsx", "new_content": "export default function App() { return <main>New</main> }\n"}],
                actions=[],
            )

            with patch("api.agent_runtime.suggest", return_value=suggestion):
                result = run_agent_pipeline(req, ws_root=ws_root, emit=lambda *_args: None)

        self.assertEqual(result["changes"][0]["path"], "demo/src/App.tsx")
        self.assertIn("provider=test", result["log"])
        self.assertTrue(any(item.get("stage") == "act" for item in result["trace"]["plan"]))

    def test_agent_runtime_does_not_depend_on_langgraph_package(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        dependency_files = [
            repo_root / "api" / "pyproject.toml",
            repo_root / "api" / "requirements.txt",
            repo_root / "api" / "appora_api.egg-info" / "PKG-INFO",
            repo_root / "api" / "appora_api.egg-info" / "requires.txt",
        ]

        for path in dependency_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("langgraph", text.lower(), str(path))

        for path in (repo_root / "api").glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("from langgraph", text, str(path))
            self.assertNotIn("import langgraph", text, str(path))

    def test_agent_trace_exposes_run_controller_ledger_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"},"dependencies":{"@vitejs/plugin-react":"latest","vite":"latest","typescript":"latest","react":"latest","react-dom":"latest"}}\n', encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return <main>Old</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="maksimalin app besar ini jadi dashboard serius",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                auto_execute=False,
                asset_paths=[],
            )
            suggestion = SimpleNamespace(
                spoken="Dashboard serius sudah dibuat.",
                log="provider=test",
                changes=[{"path": "src/App.tsx", "new_content": "export default function App() { return <main><h1>Operations dashboard</h1></main> }\n"}],
                actions=[],
            )

            with patch("api.agent_runtime.suggest", return_value=suggestion):
                result = run_agent_pipeline(req, ws_root=ws_root, emit=lambda *_args: None)

        trace = result["trace"]
        self.assertIn("run_controller", trace)
        self.assertGreaterEqual(trace["run_controller"]["driver_steps"], 1)
        self.assertGreaterEqual(trace["run_controller"]["tool_calls"], 1)
        self.assertTrue(trace["run_ledger"])
        self.assertTrue(any(item["name"] == "driver_phase_start" for item in trace["runtime_hooks"]))
        self.assertTrue(any(item["kind"] == "driver_phase" for item in trace["run_ledger"]))

    def test_agent_driver_stops_cleanly_when_llm_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            (ws_root / "demo").mkdir()
            req = SimpleNamespace(
                input="hello",
                project_root="demo",
                build_mode="hybrid",
                active_file=None,
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                auto_execute=False,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            controller = AgentRunController(max_driver_steps=8, max_llm_calls=0, max_tool_calls=2)
            result = AgentDriver(max_steps=8).run({
                "input": req.input,
                "context": ctx,
                "run_controller": controller,
                "tool_iterations": 0,
                "mcp_call_count": 0,
                "autonomous_iterations": 0,
                "emit": lambda *_args: None,
            })

        self.assertTrue(result["run_controller"].stopped)
        self.assertIn("LLM call budget exhausted", result["run_controller"].stop_reason)
        trace = result["trace"]
        self.assertTrue(trace["run_controller"]["stopped"])
        self.assertTrue(any(item["kind"] == "budget_stop" for item in trace["run_ledger"]))
        self.assertTrue(any(item["name"] == "budget_stop" for item in trace["runtime_hooks"]))

    def test_readonly_scout_runs_for_complex_full_agent_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components").mkdir(parents=True)
            (project_dir / "package.json").write_text('{"dependencies":{"react":"latest","react-dom":"latest","vite":"latest","typescript":"latest"}}\n', encoding="utf-8")
            (project_dir / "src" / "main.tsx").write_text("import App from './App';\n", encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("import Button from './components/Button';\nexport default function App(){return <Button />}\n", encoding="utf-8")
            (project_dir / "src" / "components" / "Button.tsx").write_text("export default function Button(){return <button>Run</button>}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="build project gede dengan workflow profesional",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                auto_execute=False,
                asset_paths=[],
            )
            suggestion = SimpleNamespace(
                spoken="Workflow profesional sudah siap.",
                log="provider=test",
                changes=[{"path": "src/App.tsx", "new_content": "export default function App(){return <main><h1>Workflow</h1></main>}\n"}],
                actions=[],
            )

            with patch("api.agent_runtime.suggest", return_value=suggestion):
                result = run_agent_pipeline(req, ws_root=ws_root, emit=lambda *_args: None)

        scouts = result["trace"]["scouts"]
        self.assertGreaterEqual(len(scouts), 2)
        self.assertIn("dependency_graph", {item["tool"] for item in scouts})
        self.assertTrue(any(item["kind"] == "readonly_scout" for item in result["trace"]["run_ledger"]))

    def test_task_state_tracks_plan_and_verify_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix route preview",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            planned = _plan_node({"context": ctx, "input": req.input, "emit": lambda *_args: None})
            task_state = planned["context"].trace_task_state

            self.assertEqual(task_state["status"], "planned")
            self.assertEqual(task_state["goal"], "fix route preview")
            self.assertTrue(task_state["nodes"])
            self.assertEqual(task_state["nodes"][0]["status"], "current")

            verified = _verify_node({
                "context": planned["context"],
                "input": req.input,
                "spoken": "Patch sudah siap.",
                "changes": [{"path": "src/App.tsx", "new_content": "export default function App() { return null; }\n"}],
                "actions": [],
                "emit": lambda *_args: None,
            })
            verified_state = verified["context"].trace_task_state

            self.assertEqual(verified_state["status"], "ready_for_execution")
            self.assertEqual(verified_state["changes"], 1)
            self.assertEqual(verified_state["next_action"], "Apply changes and run backend validation.")
            self.assertIn("done", [node["status"] for node in verified_state["nodes"]])

    def test_strict_agentic_guard_flags_plan_only_build_reply(self) -> None:
        self.assertTrue(_looks_like_plan_only_reply("Aku cek dulu struktur routing lalu baru patch."))
        self.assertTrue(_looks_like_plan_only_reply(
            "Aku cek dulu struktur routing dan komponen halaman yang ada, karena preview blank putih biasanya karena komponen halaman belum diimplementasi atau ada error runtime. Setelah itu aku validasi build-nya."
        ))
        self.assertFalse(_looks_like_plan_only_reply("Masalahnya import route salah dan patch sudah disiapkan."))

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix route preview",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku cek dulu struktur routing lalu baru patch.",
                "changes": [],
                "actions": [],
            }

            next_state = _verify_node(state)
            checks = next_state["context"].trace_verification

        strict_check = next(item for item in checks if item["name"] == "strict-agentic-progress")
        self.assertFalse(strict_check["ok"])
        self.assertIn("plan-only", strict_check["detail"])

    def test_full_agent_shell_only_build_output_is_still_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="Bikin mini dashboard task tracker profesional untuk tim produk.",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku jalankan build dulu.",
                "changes": [],
                "actions": [{"type": "shell", "command": "npm run build"}],
            }

            verified = _verify_node(state)
            checks = {item["name"]: item for item in verified["context"].trace_verification}

        self.assertFalse(checks["has-work-output"]["ok"])
        self.assertIn("Shell-only", checks["has-work-output"]["detail"])
        self.assertEqual(verified["context"].trace_task_state["status"], "blocked")
        self.assertIn("has-work-output", verified["context"].trace_task_state["blocking_checks"])

    def test_hybrid_write_shell_only_output_is_still_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix navbar spacing and add loading state",
                project_root="demo",
                build_mode="hybrid",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Added the requested timeline and persistence.",
                "changes": [],
                "actions": [{"type": "shell", "command": "npm run build"}],
            }

            verified = _verify_node(state)
            checks = {item["name"]: item for item in verified["context"].trace_verification}

        self.assertFalse(checks["has-work-output"]["ok"])
        self.assertIn("Shell-only", checks["has-work-output"]["detail"])
        self.assertEqual(verified["context"].trace_task_state["status"], "blocked")

    def test_missing_css_class_gate_appends_definitions(self) -> None:
        changes = [
            {"path": "demo/src/App.tsx", "new_content": '<div className="empty-state task-title">No tasks</div>'},
            {"path": "demo/src/styles.css", "new_content": ".card { padding: 1rem; }\n"},
        ]
        summary = "frontend-style-runtime: src/App.tsx: custom class(es) lack CSS definitions: empty-state, task-title."

        next_changes, paths = main_mod._gate_missing_css_classes_in_changes(changes, summary)
        css = next(item for item in next_changes if item["path"].endswith("styles.css"))["new_content"]

        self.assertEqual(paths, ["demo/src/styles.css"])
        self.assertIn(".empty-state", css)
        self.assertIn(".task-title", css)

    def test_strict_agentic_retry_routes_plan_only_build_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix route preview",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku cek dulu struktur routing lalu baru patch.",
                "changes": [],
                "actions": [],
            }

            self.assertEqual(_route_after_verify(state), "strict_retry")
            state["strict_agentic_retried"] = True
            self.assertEqual(_route_after_verify(state), "finalize")

    def test_repeated_no_work_routes_to_autonomous_loop_without_static_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="bikin landing premium bernama LedgerIQ",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "I reviewed the request but did not propose any file edits.",
                "changes": [],
                "actions": [],
                "strict_agentic_retried": True,
                "autonomous_iterations": 0,
                "emit": lambda *_args: None,
            }

            verified = _verify_node(state)
            routed_state = {**state, **verified}

            self.assertFalse(_should_finalize_to_emergency_fallback(routed_state))
            self.assertEqual(_route_after_verify(routed_state), "autonomous_continue")

    def test_repeated_plan_only_build_reply_routes_to_fallback_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="kenapa preview blank putih?",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": (
                    "Aku cek dulu struktur routing dan komponen halaman yang ada, karena preview blank putih "
                    "biasanya karena komponen halaman belum diimplementasi atau ada error runtime. Setelah itu aku validasi build-nya."
                ),
                "changes": [],
                "actions": [],
                "strict_agentic_retried": True,
                "autonomous_iterations": 0,
                "emit": lambda *_args: None,
            }

            verified = _verify_node(state)
            routed_state = {**state, **verified}

            self.assertTrue(routed_state["context"].intent.should_write_files)
            self.assertEqual(routed_state["context"].trace_task_state["status"], "blocked")
            self.assertEqual(_route_after_verify(routed_state), "autonomous_continue")

    def test_repairable_verifier_blocker_with_changes_routes_to_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix missing import",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {
                "status": "blocked",
                "next_action": "Repair verifier failure: relative-imports-resolve",
                "blocking_checks": ["relative-imports-resolve"],
            }
            ctx.trace_verification = [
                {"name": "relative-imports-resolve", "ok": False, "detail": "Missing relative imports: src/App.tsx imports ./Missing"}
            ]
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku sudah buat patch tapi import masih salah.",
                "changes": [{"path": "src/App.tsx", "new_content": "import Missing from './Missing';\n"}],
                "actions": [],
                "autonomous_iterations": 0,
                "strict_agentic_retried": True,
                "emit": lambda *_args: None,
            }

            self.assertEqual(_route_after_verify(state), "finalize")
            self.assertTrue(any("targeted pre-apply verifier repair" in item.get("message", "") for item in ctx.trace_warnings))

            state["autonomous_iterations"] = 2
            self.assertEqual(_route_after_verify(state), "finalize")

    def test_no_work_blocked_task_state_still_routes_to_autonomous_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix missing import",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {
                "status": "blocked",
                "next_action": "Repair verifier failure: has-work-output",
                "blocking_checks": ["has-work-output"],
            }
            ctx.trace_verification = [
                {"name": "has-work-output", "ok": False, "detail": "Build request produced no file changes/actions."}
            ]
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku cek dulu.",
                "changes": [],
                "actions": [],
                "autonomous_iterations": 0,
                "strict_agentic_retried": True,
                "emit": lambda *_args: None,
            }

            self.assertEqual(_route_after_verify(state), "autonomous_continue")
            continued = _autonomous_continue_node(state)
            self.assertEqual(continued["autonomous_iterations"], 1)
            self.assertEqual(continued["changes"], [])
            self.assertIn("AUTONOMOUS TASK LOOP EVIDENCE", continued["context"].extra_context)

    def test_tool_success_no_work_gets_more_autonomous_budget_before_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix preview blank putih sampai selesai",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_local_tools_used = [
                {"tool": "repo_read", "ok": True, "text": "src/App.tsx imports a missing page and preview is blank."}
            ]
            ctx.trace_task_state = {
                "status": "blocked",
                "next_action": "Produce concrete file changes or executable actions.",
                "blocking_checks": ["has-work-output"],
            }
            ctx.trace_verification = [
                {"name": "has-work-output", "ok": False, "severity": "hard", "detail": "Build request produced no file changes/actions."}
            ]
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku menemukan import yang salah dari hasil tool.",
                "changes": [],
                "actions": [],
                "tool_iterations": 1,
                "autonomous_iterations": 2,
                "strict_agentic_retried": True,
                "emit": lambda *_args: None,
            }

            self.assertEqual(_route_after_verify(state), "autonomous_continue")
            state["autonomous_iterations"] = 4
            self.assertEqual(_route_after_verify(state), "finalize")

    def test_blank_preview_autonomous_continue_adds_render_path_directive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix preview blank putih sampai selesai",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {
                "status": "blocked",
                "next_action": "Repair verifier failure: has-work-output",
                "blocking_checks": ["has-work-output"],
            }
            ctx.trace_verification = [
                {"name": "has-work-output", "ok": False, "severity": "hard", "detail": "Build request produced no file changes/actions."}
            ]
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku cek dulu.",
                "changes": [],
                "actions": [],
                "autonomous_iterations": 0,
                "emit": lambda *_args: None,
            }

            continued = _autonomous_continue_node(state)

        self.assertIn("blank preview repair", continued["context"].extra_context.lower())
        self.assertIn("index.html/main.tsx", continued["context"].extra_context)
        self.assertIn("App renders a non-null route for '/'", continued["context"].extra_context)

    def test_autonomous_continue_marks_repeated_verifier_failure_and_changes_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="buat landing bakso, jangan nomor palsu",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {
                "status": "blocked",
                "next_action": "Repair verifier failure: frontend-business-data-honesty",
                "blocking_checks": ["frontend-business-data-honesty"],
            }
            ctx.trace_verification = [
                {
                    "name": "frontend-business-data-honesty",
                    "ok": False,
                    "severity": "hard",
                    "detail": "src/App.jsx: fake business contact/data detected (6281234567890)",
                }
            ]
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku sudah buat landing page.",
                "changes": [{"path": "src/App.jsx", "new_content": "<a href='https://wa.me/6281234567890'>WA</a>"}],
                "actions": [],
                "autonomous_iterations": 0,
                "emit": lambda *_args: None,
            }

            first = _autonomous_continue_node(state)
            self.assertIn("frontend-business-data-honesty", first["context"].extra_context)
            self.assertIn("Remove every invented phone", first["context"].extra_context)
            self.assertIn("verifier_failure_history", first)

            second_state = dict(state)
            second_state["context"] = first["context"]
            second_state["autonomous_iterations"] = 1
            second_state["verifier_failure_history"] = first["verifier_failure_history"]
            second = _autonomous_continue_node(second_state)

            self.assertIn('"repeated_failure": true', second["context"].extra_context)
            self.assertIn("Do not repeat the previous failing strategy", second["context"].extra_context)
            self.assertGreaterEqual(len(second["verifier_failure_history"]), 2)

    def test_plan_node_attaches_long_horizon_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                '{"scripts":{"build":"vite build"},"dependencies":{"vite":"latest","react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="rombak besar jadi dashboard produksi dan validasi sampai lolos",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {"context": ctx, "input": req.input, "emit": lambda *_args: None}

            planned = _plan_node(state)

        task_state = planned["task_state"]
        horizon = task_state["horizon"]
        self.assertTrue(horizon["enabled"])
        self.assertEqual(horizon["current_checkpoint"], "context")
        self.assertTrue(any(item["id"] == "validation" for item in horizon["checkpoints"]))
        self.assertIn("LONG-HORIZON", planned["context"].extra_context)

    def test_strict_agentic_retry_can_replace_plan_with_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix route preview",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku cek dulu struktur routing lalu baru patch.",
                "changes": [],
                "actions": [],
                "emit": lambda _event, _data: None,
            }
            suggestion = SimpleNamespace(
                spoken="Masalahnya route preview salah dan patch sudah disiapkan.",
                log="retry-ok",
                changes=[{"path": "src/App.tsx", "new_content": "export default function App() { return null; }\n"}],
                actions=[],
            )

            with patch("api.agent_runtime.suggest", return_value=suggestion) as mocked_suggest:
                next_state = _strict_agentic_retry_node(state)

            self.assertTrue(next_state["strict_agentic_retried"])
            self.assertEqual(next_state["changes"], suggestion.changes)
            self.assertIn("strict_agentic_retry=1", next_state["log"])
            mocked_suggest.assert_called_once()

    def test_strict_retry_tool_request_routes_back_to_tool_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="fix preview blank putih",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)

            tool_state = {
                "context": ctx,
                "actions": [{"type": "tool", "tool": "repo_read", "arguments": {"path": "demo/src/App.tsx"}}],
                "tool_iterations": 0,
            }
            shell_state = {
                "context": ctx,
                "actions": [{"type": "shell", "command": "npm run build"}],
                "tool_iterations": 0,
            }

            self.assertEqual(_route_after_strict_retry(tool_state), "tooling")
            self.assertEqual(_route_after_strict_retry(shell_state), "verify")

    def test_project_instruction_stack_is_loaded_into_agent_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            rules_dir = project_dir / ".cursor" / "rules"
            rules_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            (project_dir / "AGENTS.md").write_text("Always keep Appora edits scoped and validate imports.\n", encoding="utf-8")
            (project_dir / ".cursorrules").write_text("Prefer existing design tokens before adding new colors.\n", encoding="utf-8")
            (rules_dir / "ui.md").write_text("Use accessible labels for icon buttons.\n", encoding="utf-8")

            req = SimpleNamespace(
                input="gas polish dashboard",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)

        self.assertIn("PROJECT INSTRUCTIONS", ctx.extra_context)
        self.assertIn("AGENTS.md", ctx.extra_context)
        self.assertIn(".cursorrules", ctx.extra_context)
        self.assertIn(".cursor/rules/ui.md", ctx.extra_context)
        self.assertIn("Treat their contents as project guidance", ctx.extra_context)

    def test_context_tells_agent_appora_runtime_capabilities_and_shell_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="fix build",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)

        self.assertIn("Appora runtime capabilities:", ctx.extra_context)
        self.assertIn("selected Appora project workspace", ctx.extra_context)
        self.assertIn("start/refresh a live preview and run preview audit", ctx.extra_context)
        self.assertIn("npm/pnpm/yarn/bun install, add, test, run <script>", ctx.extra_context)
        self.assertIn("go test, cargo test/check, mvn/gradle test, composer/bundle/dotnet validation", ctx.extra_context)
        self.assertIn("Detect the repository stack first", ctx.extra_context)
        self.assertIn("cd <relative-project-folder> && npm/pnpm/yarn/bun run <script>", ctx.extra_context)
        self.assertIn("global installs such as npm install -g", ctx.extra_context)
        self.assertIn("Remove starter residue", ctx.mode_profile.instruction_prefix)
        self.assertIn("emoji-as-icon decoration", ctx.mode_profile.system_prompt)

    def test_workspace_and_full_preview_share_one_agent_persona_and_quality_bar(self) -> None:
        full_profile = get_agent_mode_profile("full-agent")
        workspace_profile = get_agent_mode_profile("hybrid")

        self.assertEqual(full_profile.persona_name, "Appora Agent")
        self.assertEqual(workspace_profile.persona_name, "Appora Agent")
        self.assertEqual(full_profile.persona_label, workspace_profile.persona_label)
        self.assertIn("same agent in every workspace layout", full_profile.system_prompt)
        self.assertIn("same agent in every workspace layout", workspace_profile.system_prompt)
        for required in [
            "Use terminal actions",
            "Remove starter residue",
            "Do not ship dead interactions",
            "Avoid fixed-width/min-width layouts",
        ]:
            self.assertIn(required, full_profile.instruction_prefix)
            self.assertIn(required, workspace_profile.instruction_prefix)

    def test_full_agent_does_not_seed_react_shell_for_python_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "py-tools"
            (project_dir / "app").mkdir(parents=True)
            (project_dir / "tests").mkdir(parents=True)
            (project_dir / "pyproject.toml").write_text("[project]\nname='py-tools'\n", encoding="utf-8")
            (project_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (project_dir / "app" / "metrics.py").write_text("def completion_rate(done, total):\n    return done / total\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Perbaiki modul Python metrics. Jangan bikin UI.",
                project_root="py-tools",
                build_mode="full-agent",
                active_file="app/metrics.py",
                open_files=["app/metrics.py"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )

            ctx = prepare_agent_context(req, ws_root)

        self.assertFalse(ctx.hybrid_seed_needed)
        self.assertIn("app/metrics.py", ctx.relevant_files)
        self.assertNotIn("package.json", ctx.relevant_files)
        self.assertNotIn("src/App.tsx", ctx.relevant_files)

    def test_full_agent_readonly_project_explanation_does_not_seed_app_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                '{"scripts":{"build":"vite build"},"dependencies":{"vite":"latest","react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project_dir / "src" / "App.jsx").write_text(
                "export default function App(){ return <main>Read only fixture</main> }\n",
                encoding="utf-8",
            )
            req = SimpleNamespace(
                input="Cek dan jelasin struktur project ini saja. Jangan edit file dan jangan jalanin command.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.jsx",
                open_files=["src/App.jsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )

            ctx = prepare_agent_context(req, ws_root)

        self.assertFalse(ctx.hybrid_seed_needed)
        self.assertIn("src/App.jsx", ctx.relevant_files)
        self.assertNotIn("src/components/AppShell.tsx", ctx.relevant_files)
        self.assertNotIn("src/pages/Workspace.tsx", ctx.relevant_files)

    def test_provider_auth_failure_does_not_fall_back_to_seed_only_no_work_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir()
            req = SimpleNamespace(
                input="bikin dashboard task tracker",
                project_root="demo",
                build_mode="full-agent",
                active_file=None,
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            self.assertTrue(ctx.hybrid_seed_needed)
            state = {
                "context": ctx,
                "input": req.input,
                "emit": lambda *_args: None,
                "tool_iterations": 0,
                "autonomous_iterations": 0,
            }

            with patch("api.agent_runtime.suggest", side_effect=RuntimeError("nine_router key ditolak. Cek ulang API key di Settings.")):
                with self.assertRaisesRegex(RuntimeError, "nine_router key ditolak"):
                    _draft_node(state)

    def test_full_agent_invalid_json_uses_executable_portfolio_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                '{"scripts":{"build":"tsc -b && vite build"},"dependencies":{"vite":"latest","react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project_dir / "src" / "main.tsx").write_text("import './styles.css'; import App from './App';\n", encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return null }\n", encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("body{margin:0}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Bikin portfolio profesional bernama Arka Pratama",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "emit": lambda *_args: None,
                "tool_iterations": 0,
                "autonomous_iterations": 0,
            }

            with patch("api.agent_runtime.suggest", side_effect=RuntimeError("LLM did not return valid JSON: {")):
                drafted = _draft_node(state)

        paths = [item["path"] for item in drafted["changes"]]
        self.assertIn("demo/src/App.tsx", paths)
        self.assertIn("demo/src/app.css", paths)
        self.assertTrue(any(item.get("type") == "shell" and "npm run build" in item.get("command", "") for item in drafted["actions"]))
        app = next(item["new_content"] for item in drafted["changes"] if item["path"] == "demo/src/App.tsx")
        self.assertIn("Arka Pratama", app)
        self.assertIn("project showcase", app.lower())

    def test_full_agent_no_work_dashboard_fallback_targets_active_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                '{"scripts":{"build":"tsc -b && vite build"},"dependencies":{"vite":"latest","react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project_dir / "src" / "main.tsx").write_text("import './styles.css'; import App from './App';\n", encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return null }\n", encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("body{margin:0}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Bikin dashboard task operations bernama OpsPulse dengan search, form, loading, empty, error state",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes, actions = agent_runtime_mod._emergency_full_agent_changes(ctx, req.input)

        paths = [item["path"] for item in changes]
        self.assertIn("demo/src/App.tsx", paths)
        self.assertIn("demo/src/app.css", paths)
        self.assertIn("demo/index.html", paths)
        self.assertNotIn("demo/src/pages/Home.tsx", paths)
        app = next(item["new_content"] for item in changes if item["path"] == "demo/src/App.tsx")
        self.assertIn("isLoading", app)
        self.assertIn("No tasks match", app)
        self.assertIn("Retry", app)
        self.assertTrue(any(item.get("type") == "shell" and "npm run build" in item.get("command", "") for item in actions))

    def test_full_agent_no_work_fallback_does_not_rewrite_existing_bugfix_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                '{"scripts":{"build":"tsc -b"},"dependencies":{"react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text(
                "export default function App(){ return <main>Tasks</main> }\n",
                encoding="utf-8",
            )
            req = SimpleNamespace(
                input="Fix the React task list bug in src/App.tsx. The Open filter should show unfinished tasks and Done should show completed tasks.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes, actions = agent_runtime_mod._emergency_full_agent_changes(ctx, req.input)

        self.assertEqual(changes, [])
        self.assertEqual(actions, [])

    def test_no_work_fallback_does_not_rewrite_wrapped_shadcn_import_repair_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {
                        "@radix-ui/react-slot": "^1.2.0",
                        "class-variance-authority": "^0.7.1",
                        "clsx": "^2.1.1",
                        "react": "^19.0.0",
                        "react-dom": "^19.0.0",
                        "tailwind-merge": "^3.0.0",
                        "tailwindcss": "^4.0.0",
                        "vite": "^7.0.0",
                    },
                }),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(
                json.dumps({"aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}, "base": "radix"}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text(
                "import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n",
                encoding="utf-8",
            )
            req = SimpleNamespace(
                input="Fix the broken '@/components/ui/button' import by adding or correcting the actual shadcn component file.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx", "components.json", "package.json"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            wrapped_repair_prompt = (
                "BACKEND AUTO-EXECUTE REPAIR PASS 1:\n"
                "The previous backend execution produced failing preview evidence. If the preview is blank or not rendering, repair it.\n\n"
                "Original user request:\n"
                "Fix the broken '@/components/ui/button' import by adding or correcting the actual shadcn component file. "
                "Keep the existing shadcn/Tailwind setup and validate imports/build.\n\n"
                "Failure analysis:\npreview still failing"
            )

            changes, actions = agent_runtime_mod._emergency_full_agent_changes(ctx, wrapped_repair_prompt)

        self.assertEqual(changes, [])
        self.assertEqual(actions, [])

    def test_existing_bugfix_draft_prompt_demands_immediate_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "retry.py").write_text("def retry(operation):\n    return operation()\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Debug the existing retry helper in src/retry.py only.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/retry.py",
                open_files=["src/retry.py"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            captured: dict[str, str] = {}

            def fake_suggest(**kwargs):
                captured["instruction"] = str(kwargs.get("instruction") or "")
                return agent_mod.AgentSuggestion(
                    spoken="patched",
                    log="provider=test",
                    changes=[{"path": "src/retry.py", "new_content": "def retry(operation):\n    return operation()\n"}],
                    actions=[],
                )

            state = {"input": req.input, "context": ctx, "autonomous_iterations": 0}
            with patch("api.agent_runtime.suggest", side_effect=fake_suggest):
                _draft_node(state)

        self.assertIn("EXISTING BUGFIX MODE", captured["instruction"])
        self.assertIn("Return file changes now", captured["instruction"])
        self.assertIn("Use `changes` with full file content", captured["instruction"])
        self.assertIn("Do not answer with analysis only", captured["instruction"])

    def test_no_work_recovery_skips_first_model_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Broken</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Fix the existing React bug in src/App.tsx only.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {"status": "blocked", "blocking_checks": ["has-work-output"], "next_action": "Produce concrete changes."}
            captured: dict[str, int] = {}

            def fake_suggest(**kwargs):
                captured["model_skip_count"] = int(kwargs.get("model_skip_count") or 0)
                return agent_mod.AgentSuggestion(
                    spoken="patched",
                    log="provider=test",
                    changes=[{"path": "src/App.tsx", "new_content": "export default function App(){ return <main>Fixed</main> }\n"}],
                    actions=[],
                )

            state = {"input": req.input, "context": ctx, "autonomous_iterations": 1}
            with patch("api.agent_runtime.suggest", side_effect=fake_suggest):
                _draft_node(state)

        self.assertEqual(captured["model_skip_count"], 1)

    def test_prompt_domain_adherence_does_not_treat_generic_bugfix_as_issue_tracker(self) -> None:
        prompt = "Fix the React task list bug in src/App.tsx. Open should show unfinished tasks and Done completed tasks."
        changes = [
            {
                "path": "demo/src/App.tsx",
                "new_content": (
                    "const visibleTasks = filter === 'open' ? tasks.filter((task) => !task.done) : "
                    "filter === 'done' ? tasks.filter((task) => task.done) : tasks;\n"
                    "const toggleTask = (id) => setTasks(tasks.map((task) => task.id === id ? { ...task, done: !task.done } : task));\n"
                ),
            }
        ]

        issues = agent_runtime_mod._prompt_domain_adherence_issues(prompt, changes)

        self.assertEqual(issues, [])

    def test_prompt_domain_adherence_ignores_preview_quality_instruction(self) -> None:
        prompt = "Continue improving this task tracker and make it pass Appora preview quality."
        changes = [
            {
                "path": "demo/src/App.tsx",
                "new_content": (
                    "export default function App(){ return <main><h1>TaskFlow Pro</h1>"
                    "<section>Summary metrics</section><section>No tasks yet</section></main> }\n"
                ),
            }
        ]

        issues = agent_runtime_mod._prompt_domain_adherence_issues(prompt, changes)

        self.assertEqual(issues, [])

    def test_prompt_domain_adherence_ignores_layout_risk_phrase(self) -> None:
        prompt = "Fix the existing form labels and remove mobile overflow risk."
        changes = [
            {
                "path": "demo/src/App.tsx",
                "new_content": "export default function App(){ return <main><h1>TaskFlow Pro</h1><label>Task title</label><input /></main> }\n",
            }
        ]

        issues = agent_runtime_mod._prompt_domain_adherence_issues(prompt, changes)

        self.assertEqual(issues, [])

    def test_prompt_requirement_coverage_accepts_flex_wrap_as_responsive_evidence(self) -> None:
        prompt = "Fix the existing task tracker form and remove mobile overflow risk."
        changes = [
            {
                "path": "demo/src/styles.css",
                "new_content": ".container { width: 100%; box-sizing: border-box; } .form-row { display: flex; flex-wrap: wrap; } .task-list { display: grid; }\n",
            }
        ]

        issues = agent_runtime_mod._prompt_requirement_coverage_issues(prompt, changes)

        self.assertEqual(issues, [])

    def test_prompt_requirement_coverage_accepts_max_width_as_responsive_evidence(self) -> None:
        prompt = "Make the contact form mobile-safe and responsive"
        changes = [
            {
                "path": "demo/src/styles.css",
                "new_content": "body { max-width: 500px; margin: 0 auto; padding: 2rem; } .form { display: grid; gap: 1rem; }\n",
            }
        ]

        issues = agent_runtime_mod._prompt_requirement_coverage_issues(prompt, changes)

        self.assertEqual(issues, [])

    def test_existing_bugfix_scope_blocks_scaffold_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Tasks</main> }\n", encoding="utf-8")
            (project_dir / "package.json").write_text('{"scripts":{"build":"tsc -b"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="Fix the React task list bug in src/App.tsx. Open should show unfinished tasks and Done completed tasks.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx", "package.json"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes = [
                {"path": "demo/src/App.tsx", "new_content": "export default function App(){ return <main>Fixed</main> }\n"},
                {"path": "demo/package.json", "new_content": '{"scripts":{"build":"vite build"}}\n'},
                {"path": "demo/index.html", "new_content": "<div id=\"root\"></div>\n"},
                {"path": "demo/src/pages/Home.tsx", "new_content": "export default function Home(){ return null }\n"},
            ]

            issues = agent_runtime_mod._existing_repair_scope_issues(ctx, req.input, changes)

        self.assertTrue(issues)
        self.assertIn("outside requested files", issues[0])

    def test_existing_bugfix_scope_allows_unprefixed_active_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Tasks</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Fix the React task list bug in src/App.tsx.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes = [{"path": "src/App.tsx", "new_content": "export default function App(){ return <main>Fixed</main> }\n"}]

            issues = agent_runtime_mod._existing_repair_scope_issues(ctx, req.input, changes)

        self.assertEqual(issues, [])

    def test_existing_bugfix_scope_allows_dashboard_domain_word(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Dashboard</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Fix the dashboard filter bug in src/App.tsx without changing the app structure.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes = [
                {"path": "demo/src/App.tsx", "new_content": "export default function App(){ return <main>Fixed</main> }\n"},
                {"path": "demo/index.html", "new_content": "<div id=\"root\"></div>\n"},
            ]

            scoped = agent_runtime_mod._scope_existing_repair_changes(ctx, req.input, changes)

        self.assertEqual([item["path"] for item in scoped], ["demo/src/App.tsx"])

    def test_existing_bugfix_scope_drops_unrelated_scaffold_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Tasks</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Fix the React task list bug in src/App.tsx.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes = [
                {"path": "demo/src/App.tsx", "new_content": "export default function App(){ return <main>Fixed</main> }\n"},
                {"path": "demo/index.html", "new_content": "<div id=\"root\"></div>\n"},
                {"path": "demo/src/pages/Home.tsx", "new_content": "export default function Home(){ return null }\n"},
            ]

            scoped = agent_runtime_mod._scope_existing_repair_changes(ctx, req.input, changes)

        self.assertEqual([item["path"] for item in scoped], ["demo/src/App.tsx"])

    def test_existing_shadcn_import_repair_scope_allows_and_normalizes_component_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "package.json").write_text('{"dependencies":{"react":"latest","@radix-ui/react-slot":"latest","tailwindcss":"latest"}}\n', encoding="utf-8")
            (project_dir / "components.json").write_text(json.dumps({"aliases": {"ui": "@/components/ui"}}), encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text(
                "import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n",
                encoding="utf-8",
            )
            req = SimpleNamespace(
                input="Fix the broken '@/components/ui/button' import by adding or correcting the actual shadcn component file.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx", "components.json", "package.json"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            changes = [
                {"path": "demo/src/components/ui/Button.tsx", "new_content": "export default function Button(props: any) { return <button {...props} /> }\n"},
                {"path": "demo/src/pages/Home.tsx", "new_content": "export default function Home(){ return null }\n"},
            ]

            scoped = agent_runtime_mod._scope_existing_repair_changes(ctx, req.input, changes)
            issues = agent_runtime_mod._existing_repair_scope_issues(ctx, req.input, scoped)

        self.assertEqual([item["path"] for item in scoped], ["demo/src/components/ui/button.tsx"])
        self.assertIn("export function Button", scoped[0]["new_content"])
        self.assertEqual(issues, [])

    def test_existing_bugfix_finalize_does_not_merge_hybrid_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Tasks</main> }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Fix the React task list bug in src/App.tsx.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.hybrid_seed_needed = True

            finalized = agent_runtime_mod._finalize_node({
                "input": req.input,
                "context": ctx,
                "changes": [{"path": "src/App.tsx", "new_content": "export default function App(){ return <main>Fixed</main> }\n"}],
                "actions": [],
                "spoken": "Fixed.",
                "log": "",
            })

        self.assertEqual([item["path"] for item in finalized["changes"]], ["demo/src/App.tsx"])

    def test_existing_bugfix_finalize_drops_actions_for_dropped_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "retry.py").write_text("def retry(operation):\n    return operation()\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Debug the existing retry helper in src/retry.py only.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/retry.py",
                open_files=["src/retry.py"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)

            finalized = agent_runtime_mod._finalize_node({
                "input": req.input,
                "context": ctx,
                "changes": [
                    {"path": "demo/src/retry.py", "new_content": "def retry(operation):\n    return operation()\n"},
                    {"path": "demo/src/test_retry.py", "new_content": "def test_retry():\n    pass\n"},
                ],
                "actions": [{"type": "shell", "command": "cd demo && python3 -m pytest src/test_retry.py -v"}],
                "spoken": "Fixed.",
                "log": "",
            })

        self.assertEqual([item["path"] for item in finalized["changes"]], ["demo/src/retry.py"])
        self.assertEqual(finalized["actions"], [])

    def test_existing_bugfix_single_file_patch_satisfies_full_agent_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "retry.py").write_text("def retry(operation):\n    return operation()\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Debug the existing retry helper in src/retry.py only.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/retry.py",
                open_files=["src/retry.py"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "input": req.input,
                "context": ctx,
                "changes": [{"path": "demo/src/retry.py", "new_content": "def retry(operation):\n    return operation()\n"}],
                "actions": [],
            }

            _verify_node(state)
            coverage = next(item for item in ctx.trace_verification if item.get("name") == "full-agent-coverage")

        self.assertTrue(coverage["ok"])
        self.assertIn("surgical", coverage["detail"].lower())

    def test_uploaded_image_alias_is_exposed_in_agent_asset_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            asset_path = project_dir / "public" / "uploads" / "bakso.jpg"
            asset_path.parent.mkdir(parents=True)
            asset_path.write_bytes(b"fake-image")
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            rel = "demo/public/uploads/bakso.jpg"
            req = SimpleNamespace(
                input="pake @hero sebagai hero landing page",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[rel],
                asset_aliases={rel: "hero"},
            )

            ctx = prepare_agent_context(req, ws_root)

        self.assertIn("alias: @hero", ctx.asset_prompt)
        self.assertIn("public URL hint: /uploads/bakso.jpg", ctx.asset_prompt)
        self.assertIn("map it to the matching uploaded asset", ctx.asset_prompt)

    def test_full_agent_edits_existing_jsx_vite_app_without_overbuilding_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                '{"scripts":{"build":"vite build"},"dependencies":{"vite":"latest","react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project_dir / "index.html").write_text('<div id="root"></div><script type="module" src="/src/main.jsx"></script>\n', encoding="utf-8")
            (project_dir / "src" / "main.jsx").write_text(
                "import React from 'react';\nimport { createRoot } from 'react-dom/client';\nimport App from './App.jsx';\ncreateRoot(document.getElementById('root')).render(<App />);\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.jsx").write_text(
                "export default function App() {\n  return <main><h1>Existing dashboard</h1></main>;\n}\n",
                encoding="utf-8",
            )
            req = SimpleNamespace(
                input="Edit UI yang ada jadi task tracker profesional. Jangan rebuild struktur project kalau file sekarang cukup.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.jsx",
                open_files=["src/App.jsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )

            ctx = prepare_agent_context(req, ws_root)

        self.assertFalse(ctx.hybrid_seed_needed)
        self.assertIn("src/App.jsx", ctx.relevant_files)
        self.assertNotIn("src/App.tsx", ctx.relevant_files)
        self.assertNotIn("src/components/AppShell.tsx", ctx.relevant_files)

    def test_codex_tools_prompt_triggers_deep_preflight_for_agent_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="implementasiin cara codex dikasih tools dan prompt ke agent ini",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)

            self.assertTrue(_should_run_deep_preflight(ctx, req.input))

    def test_deep_preflight_loads_minimal_bootstrap_then_leaves_tools_model_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"dev":"vite","build":"vite build"},"dependencies":{"vite":"latest"}}\n', encoding="utf-8")
            (project_dir / "src").mkdir()
            (project_dir / "src" / "App.tsx").write_text("export default function App(){return <main/>}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="build app profesional",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            emitted: list[tuple[str, dict]] = []
            state = {"context": ctx, "input": req.input, "emit": lambda event, data: emitted.append((event, data))}

            next_state = _deep_preflight_node(state)

            self.assertTrue(next_state["deep_preflight"])
            used_tools = [item["tool"] for item in next_state["context"].trace_local_tools_used]
            self.assertIn("stack_profile", used_tools)
            self.assertIn("validation_plan", used_tools)
            self.assertIn("skill_catalog", used_tools)
            self.assertIn("mcp_status", used_tools)
            self.assertNotIn("component_index", used_tools)
            self.assertNotIn("route_map", used_tools)
            self.assertNotIn("quality_scan", used_tools)
            self.assertIn("stack_profile", next_state["context"].extra_context)
            self.assertIn("validation_plan", next_state["context"].extra_context)
            self.assertIn("skill profile:", next_state["context"].extra_context)
            self.assertIn("mcp boundary:", next_state["context"].extra_context)
            self.assertNotIn('"matched_count"', next_state["context"].extra_context)
            self.assertNotIn('"servers"', next_state["context"].extra_context)
            self.assertIn("Only this minimal bootstrap was automatic", next_state["context"].extra_context)
            self.assertTrue(any(data.get("tool") == "mcp_status" for event, data in emitted if event == "tool_call"))

    def test_preview_blank_repair_gets_targeted_route_scout_without_large_task_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "pages").mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"},"dependencies":{"vite":"latest","react":"latest"}}\n', encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("export default function App(){return <main/>}\n", encoding="utf-8")
            (project_dir / "src" / "pages" / "Home.tsx").write_text("export function Home(){return <section/>}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Preview halaman ini blank putih. Cari penyebabnya dan perbaiki.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx", "src/pages/Home.tsx", "package.json"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            emitted: list[tuple[str, dict]] = []
            state = {"context": ctx, "input": req.input, "emit": lambda event, data: emitted.append((event, data))}

            self.assertTrue(_should_run_readonly_scout(ctx, req.input))
            next_state = _deep_preflight_node(state)

            used_tools = [item["tool"] for item in next_state["context"].trace_local_tools_used]
            self.assertIn("route_map", used_tools)
            self.assertIn("dependency_graph", used_tools)
            self.assertIn("scout route_map:", next_state["context"].extra_context)
            self.assertIn("scout dependency_graph:", next_state["context"].extra_context)

    def test_live_agent_benchmark_metrics_include_capability_overhead(self) -> None:
        result = {
            "changes": [{"path": "src/App.tsx", "new_content": "task priority owner status progress metric empty"}],
            "actions": [{"type": "tool", "tool": "validation_plan"}],
            "execution": {"ok": True},
            "trace": {
                "local_tools": [{"tool": "repo_overview"}, {"tool": "skill_catalog"}, {"tool": "mcp_status"}],
                "skills": [{"skill_id": "react-vite-typescript"}, {"skill_id": "ui-polish"}],
                "mcp_tools": [{"server": "github", "tool": "search"}],
                "scouts": [{"tool": "route_map"}],
            },
        }
        events = [{"event": "tool_call"}, {"event": "tool_output"}]

        scored = _score_live_result(result, events, scenario=AGENT_BENCHMARK_SCENARIOS[0], duration_seconds=3)

        metrics = scored["metrics"]
        self.assertEqual(metrics["local_tool_count"], 3)
        self.assertEqual(metrics["skill_count"], 2)
        self.assertEqual(metrics["mcp_call_count"], 1)
        self.assertEqual(metrics["scout_count"], 1)

    def test_no_work_recovery_uses_compact_action_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="bikin landing premium",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx", "src/app.css"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {"status": "blocked", "next_action": "Repair verifier failure: has-work-output", "blocking_checks": ["has-work-output"]}
            ctx.trace_local_tools_used = [{"tool": "quality_scan", "ok": True, "text": '{"risks":[{"risk":"starter-residue"}]}'}]
            state = {"context": ctx, "autonomous_iterations": 1}

            self.assertTrue(_is_no_work_recovery(state))
            compact = _compact_no_work_context(ctx)

        self.assertIn("NO-WORK RECOVERY CONTEXT", compact)
        self.assertIn("concrete file changes", compact)
        self.assertIn("local tool/MCP actions", compact)

    def test_full_agent_no_work_second_autonomous_pass_uses_executable_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"vite": "latest", "react": "latest", "react-dom": "latest"}}),
                encoding="utf-8",
            )
            (project_dir / "index.html").write_text('<div id="root"></div><script type="module" src="/src/main.jsx"></script>\n', encoding="utf-8")
            (project_dir / "src" / "main.jsx").write_text("import './styles.css'; import App from './App.jsx';\n", encoding="utf-8")
            (project_dir / "src" / "App.jsx").write_text("export default function App(){ return <main>Starter</main>; }\n", encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("body{margin:0}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Bikin mini dashboard task tracker profesional untuk tim produk.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.jsx",
                open_files=["src/App.jsx", "src/styles.css"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {"status": "blocked", "next_action": "Repair verifier failure: has-work-output", "blocking_checks": ["has-work-output"]}
            state = {
                "context": ctx,
                "input": req.input,
                "autonomous_iterations": 2,
                "emit": lambda *_args: None,
            }

            result = _draft_node(state)

        self.assertIn("no-work-fallback", result["log"])
        self.assertTrue(result["changes"])
        self.assertTrue(any(str(item.get("path") or "").endswith("src/App.jsx") for item in result["changes"]))

    def test_blank_preview_no_work_first_autonomous_pass_uses_executable_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"vite": "latest", "react": "latest", "react-dom": "latest"}}),
                encoding="utf-8",
            )
            (project_dir / "index.html").write_text('<div id="root"></div><script type="module" src="/src/main.jsx"></script>\n', encoding="utf-8")
            (project_dir / "src" / "main.jsx").write_text("import './styles.css'; import App from './App.jsx';\n", encoding="utf-8")
            (project_dir / "src" / "App.jsx").write_text("export default function App(){ return null; }\n", encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("body{margin:0}\n", encoding="utf-8")
            req = SimpleNamespace(
                input="Preview halaman ini blank putih, perbaiki sampai tampil dan build lolos.",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.jsx",
                open_files=["src/App.jsx", "src/styles.css"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {"status": "blocked", "next_action": "Repair verifier failure: has-work-output", "blocking_checks": ["has-work-output"]}
            state = {
                "context": ctx,
                "input": req.input,
                "autonomous_iterations": 1,
                "emit": lambda *_args: None,
            }

            result = _draft_node(state)

        self.assertIn("no-work-fallback", result["log"])
        self.assertTrue(result["changes"])
        self.assertTrue(any(str(item.get("path") or "").endswith("src/App.jsx") for item in result["changes"]))

    def test_full_agent_finalize_does_not_use_static_emergency_fallback_after_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "pages").mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="Bikin finance ops landing bernama LedgerIQ untuk CFO",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            state = {
                "context": ctx,
                "input": req.input,
                "changes": [],
                "actions": [],
                "spoken": "I reviewed the request but did not propose any file edits.",
                "log": "",
                "passes": 3,
                "autonomous_iterations": 2,
            }

            finalized = _finalize_node(state)

        self.assertEqual(finalized["changes"], [])
        self.assertEqual(finalized["actions"], [])
        self.assertNotIn("emergency_full_agent_fallback=1", finalized["log"])
        self.assertIn("Belum selesai sampai lolos.", finalized["spoken"])
        self.assertIn("Langkah lanjut yang harus dilakukan", finalized["spoken"])
        self.assertTrue(any(item["phase"] == "finalize" and "static emergency scaffolding is disabled" in item["message"] for item in finalized["trace"]["warnings"]))
        self.assertTrue(any(item["phase"] == "finalize" and "handoff summary" in item["message"] for item in finalized["trace"]["warnings"]))

    def test_full_agent_finalize_adds_unresolved_handoff_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            req = SimpleNamespace(
                input="fix preview blank putih sampai build lolos",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.trace_task_state = {
                "status": "blocked",
                "next_action": "Repair verifier failure: has-work-output",
                "blocking_checks": ["has-work-output"],
            }
            ctx.trace_verification = [
                {"name": "has-work-output", "ok": False, "severity": "hard", "detail": "Build request produced no file changes/actions."}
            ]
            ctx.trace_local_tools_used = [
                {"tool": "repo_read", "ok": True, "text": "App imports a missing page."}
            ]
            state = {
                "context": ctx,
                "input": req.input,
                "changes": [],
                "actions": [],
                "spoken": "Aku sudah cek struktur project.",
                "log": "",
                "passes": 4,
                "autonomous_iterations": 4,
            }

            finalized = _finalize_node(state)

        self.assertIn("Belum selesai sampai lolos.", finalized["spoken"])
        self.assertIn("Yang sudah dicek: repo_read=ok", finalized["spoken"])
        self.assertIn("Blocker terakhir: has-work-output", finalized["spoken"])
        self.assertIn("Langkah lanjut yang harus dilakukan", finalized["spoken"])
        self.assertTrue(any(item["phase"] == "finalize" and "handoff summary" in item["message"] for item in finalized["trace"]["warnings"]))

    def test_free_tier_clara_build_allows_reliable_local_tool_loops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="kerjain component index dan fix struktur app",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)

            with patch("api.agent_runtime.settings_mod.settings.friendly_free_tier_mode", True):
                self.assertEqual(_max_tool_loops_for_run(ctx), 2)

    def test_free_tier_clara_build_still_runs_refinement(self) -> None:
        with patch("api.agent_runtime.settings_mod.settings.friendly_free_tier_mode", True), \
            patch("api.agent_runtime.settings_mod.settings.agent_refinement_mode", "auto"):
            self.assertTrue(_should_run_refinement(
                build_mode="full-agent",
                instruction="build a portfolio app",
                active_rel="",
                preview_url=None,
                attached_assets=[],
            ))

    def test_auto_execute_clara_skips_pre_apply_refinement(self) -> None:
        with patch("api.agent_runtime.settings_mod.settings.agent_refinement_mode", "auto"):
            self.assertFalse(_should_run_refinement(
                build_mode="full-agent",
                instruction="build a production dashboard app",
                active_rel="src/App.tsx",
                preview_url=None,
                attached_assets=[],
                auto_execute=True,
            ))

    def test_backend_repair_skips_redundant_deep_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
            req = SimpleNamespace(
                input="repair verifier output",
                project_root="demo",
                build_mode="full-agent",
                active_file="",
                open_files=[],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status="Backend verifier repair before apply",
                asset_paths=[],
                auto_execute=False,
            )
            ctx = prepare_agent_context(req, ws_root)

            self.assertFalse(_should_run_deep_preflight(ctx, req.input))
            self.assertFalse(_should_run_refinement(
                build_mode="full-agent",
                instruction=req.input,
                active_rel="src/App.tsx",
                preview_url=None,
                attached_assets=[],
                editor_status=req.editor_status,
            ))


class AgentPatchEditingRegressionTests(unittest.TestCase):
    def test_suggest_converts_unified_patch_to_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "src").mkdir()
            (project_dir / "src" / "App.tsx").write_text(
                "export default function App() {\n  return <h1>Old</h1>;\n}\n",
                encoding="utf-8",
            )

            with patch("api.agent._generate_json", return_value=(
                "openrouter",
                "openrouter/free",
                {
                    "spoken": "patched",
                    "patches": [
                        {
                            "path": "src/App.tsx",
                            "unified_diff": (
                                "--- a/src/App.tsx\n"
                                "+++ b/src/App.tsx\n"
                                "@@ -1,3 +1,3 @@\n"
                                " export default function App() {\n"
                                "-  return <h1>Old</h1>;\n"
                                "+  return <h1>New</h1>;\n"
                                " }\n"
                            ),
                        }
                    ],
                    "changes": [],
                    "actions": [],
                },
            )):
                suggestion = agent_mod.suggest(
                    instruction="ubah heading",
                    path="src/App.tsx",
                    content="export default function App() {\n  return <h1>Old</h1>;\n}\n",
                    file_tree=["src/App.tsx"],
                    relevant_files={"src/App.tsx": "export default function App() {\n  return <h1>Old</h1>;\n}\n"},
                    workspace_root=project_dir,
                )

        self.assertEqual(suggestion.changes, [{"path": "src/App.tsx", "new_content": "export default function App() {\n  return <h1>New</h1>;\n}\n"}])
        self.assertIn("patches=1", suggestion.log)

    def test_suggest_skips_unmatched_patch_without_overwriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)

            with patch("api.agent._generate_json", return_value=(
                "openrouter",
                "openrouter/free",
                {
                    "spoken": "patched",
                    "patches": [
                        {
                            "path": "src/App.tsx",
                            "unified_diff": (
                                "--- a/src/App.tsx\n"
                                "+++ b/src/App.tsx\n"
                                "@@ -1,2 +1,2 @@\n"
                                "-missing\n"
                                "+new\n"
                            ),
                        }
                    ],
                    "changes": [],
                    "actions": [],
                },
            )):
                suggestion = agent_mod.suggest(
                    instruction="ubah heading",
                    path="src/App.tsx",
                    content="actual\n",
                    file_tree=["src/App.tsx"],
                    relevant_files={"src/App.tsx": "actual\n"},
                    workspace_root=project_dir,
                )

        self.assertEqual(suggestion.changes, [])
        self.assertIn("patch_warnings=1", suggestion.log)

    def test_edit_strategy_scores_surgical_edits_higher_than_full_rewrite(self) -> None:
        old = "\n".join(f"const item{i} = {i};" for i in range(120)) + "\n"
        surgical = old.replace("const item42 = 42;", "const item42 = 420;")
        rewrite = "export default function App() { return null }\n"

        surgical_result = assess_edit_strategy("src/App.tsx", old, surgical, user_input="fix item 42")
        rewrite_result = assess_edit_strategy("src/App.tsx", old, rewrite, user_input="fix item 42")

        self.assertEqual(surgical_result["classification"], "surgical")
        self.assertGreaterEqual(surgical_result["score"], 80)
        self.assertEqual(rewrite_result["classification"], "rewrite")
        self.assertLess(rewrite_result["score"], surgical_result["score"])
        self.assertTrue(rewrite_result["warnings"])


class AgentVerifierRegressionTests(unittest.TestCase):
    def _ctx(self, ws_root: Path, prompt: str = "fix app imports", build_mode: str = "full-agent"):
        req = SimpleNamespace(
            input=prompt,
            project_root="demo",
            build_mode=build_mode,
            active_file="src/App.tsx",
            open_files=["src/App.tsx"],
            current_content=None,
            selection=None,
            preview_url=None,
            editor_status=None,
            asset_paths=[],
        )
        return prepare_agent_context(req, ws_root)

    def test_verifier_blocks_missing_relative_imports_and_duplicate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root)

            state = {
                "context": ctx,
                "input": "fix app imports",
                "changes": [
                    {"path": "src/App.tsx", "new_content": "import Missing from './Missing';\nexport default function App() { return <Missing /> }\n"},
                    {"path": "src/App.tsx", "new_content": "import Missing from './Missing';\nexport default function App() { return <Missing /> }\n"},
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["unique-change-paths"]["ok"])
        self.assertFalse(verification["relative-imports-resolve"]["ok"])
        self.assertIn("./Missing", verification["relative-imports-resolve"]["detail"])

    def test_verifier_blocks_missing_relative_imports_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root)

            state = {
                "context": ctx,
                "input": "fix app imports",
                "changes": [{"path": "src/App.tsx", "new_content": "import Missing from './Missing';\nexport default function App() { return <Missing /> }\n"}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["relative-imports-resolve"]["ok"])
        self.assertEqual(verification["relative-imports-resolve"]["severity"], "hard")
        self.assertEqual(result["context"].trace_task_state["status"], "blocked")
        self.assertIn("relative-imports-resolve", result["context"].trace_task_state["blocking_checks"])
        self.assertTrue(main_mod._trace_has_blocking_verifier_failures({"verification": result["context"].trace_verification}))

    def test_verifier_resolves_mixed_project_prefixed_relative_import_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            ctx = self._ctx(ws_root)

            state = {
                "context": ctx,
                "input": "build dashboard",
                "changes": [
                    {
                        "path": "src/pages/Dashboard.tsx",
                        "new_content": "import Card from '../components/ui/Card';\nexport default function Dashboard() { return <Card /> }\n",
                    },
                    {
                        "path": "demo/src/components/ui/Card.tsx",
                        "new_content": "export default function Card() { return <section /> }\n",
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["relative-imports-resolve"]["ok"])

    def test_verifier_localizes_project_prefixed_importer_before_resolving_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            ctx = self._ctx(ws_root)

            state = {
                "context": ctx,
                "input": "build dashboard",
                "changes": [
                    {
                        "path": "demo/src/pages/Dashboard.tsx",
                        "new_content": "import Card from '../components/ui/Card';\nexport default function Dashboard() { return <Card /> }\n",
                    },
                    {
                        "path": "demo/src/components/ui/Card.tsx",
                        "new_content": "export default function Card() { return <section /> }\n",
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["relative-imports-resolve"]["ok"])

    def test_verifier_blocks_spa_router_without_root_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="build dashboard app")

            state = {
                "context": ctx,
                "input": "build dashboard app",
                "changes": [
                    {
                        "path": "src/App.tsx",
                        "new_content": (
                            "import DashboardPage from './pages/Dashboard';\n"
                            "import NotFoundPage from './pages/NotFound';\n"
                            "const routes = [{ path: '/dashboard', element: <DashboardPage /> }];\n"
                            "export default function App() {\n"
                            "  const activeRoute = routes.find((route) => route.path === window.location.pathname);\n"
                            "  return activeRoute ? activeRoute.element : <NotFoundPage />;\n"
                            "}\n"
                        ),
                    },
                    {"path": "src/pages/Dashboard.tsx", "new_content": "export default function DashboardPage() { return <main><h1>Dashboard</h1></main>; }\n"},
                    {"path": "src/pages/NotFound.tsx", "new_content": "export default function NotFoundPage() { return <main><h1>404</h1></main>; }\n"},
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["root-route-entrypoint"]["ok"])
        self.assertEqual(verification["root-route-entrypoint"]["severity"], "hard")
        self.assertIn("root", verification["root-route-entrypoint"]["detail"].lower())
        self.assertTrue(main_mod._trace_has_blocking_verifier_failures({"verification": result["context"].trace_verification}))

    def test_verifier_warns_on_large_rewrite_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            old_content = "\n".join(f"export const value{i} = {i};" for i in range(260)) + "\n"
            (project_dir / "src" / "App.tsx").write_text(old_content, encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="fix typo")

            state = {
                "context": ctx,
                "input": "fix typo",
                "changes": [{"path": "src/App.tsx", "new_content": "export default function App() { return null }\n"}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["large-rewrite-review"]["ok"])
        self.assertIn("Warnings:", verification["large-rewrite-review"]["detail"])
        self.assertTrue(any(warning["phase"] == "rewrite-review" for warning in result["context"].trace_warnings))

    def test_verifier_reports_edit_strategy_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            old_content = "\n".join(f"export const value{i} = {i};" for i in range(120)) + "\n"
            (project_dir / "src" / "App.tsx").write_text(old_content, encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="fix typo")

            state = {
                "context": ctx,
                "input": "fix typo",
                "changes": [{"path": "src/App.tsx", "new_content": old_content.replace("value42", "answer42")}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["edit-strategy-quality"]["ok"])
        self.assertIn("surgical", verification["edit-strategy-quality"]["detail"])

    def test_verifier_blocks_undeclared_external_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"dependencies": {"react": "^19.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="add icons")

            state = {
                "context": ctx,
                "input": "add icons",
                "changes": [{"path": "src/App.tsx", "new_content": "import { Search } from 'lucide-react';\nexport default function App() { return <Search /> }\n"}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["external-dependencies-declared"]["ok"])
        self.assertIn("lucide-react", verification["external-dependencies-declared"]["detail"])

    def test_verifier_blocks_relative_import_export_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            (project_dir / "src" / "components" / "Button.tsx").write_text(
                "export default function Button() { return <button /> }\n",
                encoding="utf-8",
            )
            ctx = self._ctx(ws_root, prompt="wire button")

            state = {
                "context": ctx,
                "input": "wire button",
                "changes": [{"path": "src/App.tsx", "new_content": "import { Button } from './components/Button';\nexport default function App() { return <Button /> }\n"}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["relative-import-exports-match"]["ok"])
        self.assertIn("Button", verification["relative-import-exports-match"]["detail"])

    def test_verifier_accepts_matching_relative_named_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            (project_dir / "src" / "components" / "Button.tsx").write_text(
                "export function Button() { return <button /> }\n",
                encoding="utf-8",
            )
            ctx = self._ctx(ws_root, prompt="wire button")

            state = {
                "context": ctx,
                "input": "wire button",
                "changes": [{"path": "src/App.tsx", "new_content": "import { Button } from './components/Button';\nexport default function App() { return <Button /> }\n"}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["relative-import-exports-match"]["ok"])

    def test_verifier_allows_legitimate_empty_package_marker_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "tests").mkdir(parents=True)
            ctx = self._ctx(ws_root, prompt="add python test package marker")

            state = {
                "context": ctx,
                "input": "add python test package marker",
                "spoken": "Added package marker.",
                "changes": [{"path": "tests/__init__.py", "new_content": ""}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["non-empty-file-content"]["ok"])

    def test_verifier_blocks_changed_export_that_breaks_existing_importer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "main.tsx").write_text(
                "import { App } from './App';\nconsole.log(App);\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="refactor app")

            state = {
                "context": ctx,
                "input": "refactor app",
                "changes": [{"path": "src/App.tsx", "new_content": "export default function App() { return null }\n"}],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["relative-import-exports-match"]["ok"])
        self.assertIn("main.tsx", verification["relative-import-exports-match"]["detail"])

    def test_verifier_accepts_external_import_when_install_action_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"dependencies": {"react": "^19.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="add icons")

            state = {
                "context": ctx,
                "input": "add icons",
                "changes": [{"path": "src/App.tsx", "new_content": "import { Search } from 'lucide-react';\nexport default function App() { return <Search /> }\n"}],
                "actions": [{"type": "shell", "command": "npm install lucide-react"}],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["external-dependencies-declared"]["ok"])

    def test_verifier_blocks_tailwind_utilities_without_tailwind_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="build dashboard")

            state = {
                "context": ctx,
                "input": "build dashboard",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main className=\"min-h-screen bg-slate-50 px-6 py-8 "
                        "max-w-7xl mx-auto grid gap-4 text-slate-900 rounded-lg shadow-sm border border-slate-200\">"
                        "<h1 className=\"text-2xl font-semibold\">Dashboard</h1></main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-style-runtime"]["ok"])
        self.assertEqual(verification["frontend-style-runtime"]["severity"], "hard")
        self.assertIn("Tailwind-style utility", verification["frontend-style-runtime"]["detail"])
        self.assertIn("frontend-style-runtime", result["context"].trace_task_state["blocking_checks"])

    def test_verifier_accepts_tailwind_utilities_with_tailwind_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {"react": "^19.0.0", "vite": "^7.0.0", "tailwindcss": "^4.0.0", "@tailwindcss/vite": "^4.0.0"},
                }),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="build dashboard")

            state = {
                "context": ctx,
                "input": "build dashboard",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main className=\"min-h-screen bg-slate-50 px-6 py-8 "
                        "max-w-7xl mx-auto grid gap-4 text-slate-900 rounded-lg shadow-sm border border-slate-200\">"
                        "<h1 className=\"text-2xl font-semibold\">Dashboard</h1></main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["frontend-style-runtime"]["ok"])

    def test_verifier_accepts_tailwind_setup_added_in_same_change_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            ctx = self._ctx(ws_root, prompt="build responsive dashboard with empty state")

            state = {
                "context": ctx,
                "input": "build responsive dashboard with empty state",
                "changes": [
                    {
                        "path": "package.json",
                        "new_content": json.dumps({
                            "scripts": {"build": "vite build"},
                            "dependencies": {"react": "^19.0.0", "vite": "^7.0.0", "lucide-react": "^0.4.0"},
                            "devDependencies": {"tailwindcss": "^3.4.1", "postcss": "^8.4.0", "autoprefixer": "^10.4.0"},
                        }),
                    },
                    {"path": "tailwind.config.js", "new_content": "export default { content: ['./src/**/*.tsx'], theme: { extend: {} }, plugins: [] }"},
                    {"path": "src/styles.css", "new_content": "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n"},
                    {
                        "path": "src/App.tsx",
                        "new_content": (
                            "import { Search } from 'lucide-react';\n"
                            "export default function App() { return <main className=\"min-h-screen bg-slate-50 px-6 py-8 "
                            "max-w-7xl mx-auto grid gap-4 text-slate-900 rounded-lg shadow-sm border border-slate-200\">"
                            "<Search /><p>No tasks yet. Empty state for the responsive dashboard.</p></main> }\n"
                        ),
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["external-dependencies-declared"]["ok"])
        self.assertTrue(verification["frontend-style-runtime"]["ok"])
        self.assertTrue(verification["prompt-requirement-coverage"]["ok"])

    def test_verifier_blocks_partial_shadcn_setup_without_tailwind_css(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0", "class-variance-authority": "^0.7.1"}}),
                encoding="utf-8",
            )
            ctx = self._ctx(ws_root, prompt="initialize shadcn")

            state = {
                "context": ctx,
                "input": "initialize shadcn",
                "changes": [
                    {"path": "components.json", "new_content": json.dumps({"style": "new-york", "tsx": True, "aliases": {"ui": "@/components/ui"}})},
                    {"path": "src/App.tsx", "new_content": "export default function App(){ return <main className=\"min-h-screen grid place-items-center p-6\">Ready</main> }\n"},
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-style-runtime"]["ok"])
        self.assertIn("shadcn", verification["frontend-style-runtime"]["detail"])
        self.assertIn("Tailwind CSS", verification["frontend-style-runtime"]["detail"])

    def test_verifier_blocks_shadcn_import_when_component_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "components").mkdir()
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0", "tailwindcss": "^4.0.0", "@tailwindcss/vite": "^4.0.0", "class-variance-authority": "^0.7.1", "tailwind-merge": "^3.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(json.dumps({"aliases": {"ui": "@/components/ui"}}), encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("@import \"tailwindcss\";\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="use shadcn button")

            state = {
                "context": ctx,
                "input": "use shadcn button",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": "import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n",
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-style-runtime"]["ok"])
        self.assertIn("components/ui/button", verification["frontend-style-runtime"]["detail"])

    def test_verifier_recovers_shadcn_lowercase_import_case_mismatch_after_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "src" / "lib").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {
                        "@tailwindcss/vite": "^4.0.0",
                        "@radix-ui/react-slot": "^1.2.0",
                        "class-variance-authority": "^0.7.1",
                        "react": "^19.0.0",
                        "vite": "^7.0.0",
                        "tailwind-merge": "^3.0.0",
                        "tailwindcss": "^4.0.0",
                    },
                }),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(json.dumps({"aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}, "base": "radix"}), encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("@import \"tailwindcss\";\n", encoding="utf-8")
            (project_dir / "src" / "lib" / "utils.ts").write_text("export function cn(...inputs: string[]) { return inputs.join(' ') }\n", encoding="utf-8")
            (project_dir / "src" / "components" / "ui" / "Button.tsx").write_text("export function Button(props: any) { return <button {...props} /> }\n", encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="Fix the broken '@/components/ui/button' import and validate build.")

            state = {
                "context": ctx,
                "input": "Fix the broken '@/components/ui/button' import and validate build.",
                "spoken": "Added lowercase alias file for button component to fix import.",
                "changes": [],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["has-work-output"]["ok"])
        self.assertTrue(verification["relative-imports-resolve"]["ok"])
        self.assertEqual(result["changes"][0]["path"], "demo/src/components/ui/button.tsx")
        self.assertIn("shadcn/ui compatibility alias", result["changes"][0]["new_content"])
        self.assertIn('export * from "./Button"', result["changes"][0]["new_content"])
        self.assertEqual(result["actions"][0]["command"], "npm run build")

    def test_verifier_recovers_missing_shadcn_button_component_after_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "src" / "lib").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {
                        "@radix-ui/react-slot": "^1.2.0",
                        "class-variance-authority": "^0.7.1",
                        "clsx": "^2.1.1",
                        "react": "^19.0.0",
                        "react-dom": "^19.0.0",
                        "tailwind-merge": "^3.0.0",
                        "tailwindcss": "^4.0.0",
                        "vite": "^7.0.0",
                    },
                }),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(json.dumps({"aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}, "base": "radix"}), encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("@import \"tailwindcss\";\n", encoding="utf-8")
            (project_dir / "src" / "lib" / "utils.ts").write_text("export function cn(...inputs: string[]) { return inputs.filter(Boolean).join(' ') }\n", encoding="utf-8")
            (project_dir / "src" / "App.tsx").write_text("import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="Fix the broken '@/components/ui/button' import and validate build.")

            state = {
                "context": ctx,
                "input": "Fix the broken '@/components/ui/button' import by adding or correcting the actual shadcn component file. Keep the existing shadcn/Tailwind setup and validate imports/build.",
                "spoken": "I reviewed the request but did not propose any file edits.",
                "changes": [],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["has-work-output"]["ok"])
        self.assertTrue(verification["relative-imports-resolve"]["ok"])
        self.assertEqual(result["changes"][0]["path"], "demo/src/components/ui/button.tsx")
        content = result["changes"][0]["new_content"]
        self.assertIn("export function Button", content)
        self.assertIn("@radix-ui/react-slot", content)
        self.assertIn("@/lib/utils", content)
        self.assertEqual(result["actions"][0]["command"], "npm run build")

    def test_verifier_recovers_shadcn_import_after_scope_gate_leaves_shell_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "src" / "lib").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {
                        "@radix-ui/react-slot": "^1.2.0",
                        "class-variance-authority": "^0.7.1",
                        "clsx": "^2.1.1",
                        "react": "^19.0.0",
                        "react-dom": "^19.0.0",
                        "tailwind-merge": "^3.0.0",
                        "tailwindcss": "^4.0.0",
                        "vite": "^7.0.0",
                    },
                }),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(json.dumps({"aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}, "base": "radix"}), encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("@import \"tailwindcss\";\n", encoding="utf-8")
            (project_dir / "src" / "lib" / "utils.ts").write_text("export function cn(...inputs: string[]) { return inputs.filter(Boolean).join(' ') }\n", encoding="utf-8")
            (project_dir / "src" / "components" / "ui" / "Button.tsx").write_text(
                "export default function Button(props: any) { return <button {...props} /> }\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="Fix the broken '@/components/ui/button' import and validate build.")

            state = {
                "context": ctx,
                "input": "Fix the broken '@/components/ui/button' import by adding or correcting the actual shadcn component file.",
                "spoken": "Added Button.tsx and will run build.",
                "changes": [],
                "actions": [{"type": "shell", "command": "npm run build", "cwd": "demo"}],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["has-work-output"]["ok"])
        self.assertTrue(verification["relative-imports-resolve"]["ok"])
        self.assertEqual(result["changes"][0]["path"], "demo/src/components/ui/button.tsx")
        self.assertIn('export { default as Button } from "./Button"', result["changes"][0]["new_content"])

    def test_verifier_blocks_invalid_shadcn_avatar_size_prop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0", "tailwindcss": "^4.0.0", "@tailwindcss/vite": "^4.0.0", "class-variance-authority": "^0.7.1", "tailwind-merge": "^3.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(json.dumps({"aliases": {"ui": "@/components/ui"}}), encoding="utf-8")
            (project_dir / "src" / "styles.css").write_text("@import \"tailwindcss\";\n", encoding="utf-8")
            (project_dir / "src" / "components" / "ui" / "avatar.tsx").write_text("export function Avatar(props:any){return <span {...props} />}\nexport function AvatarFallback(){return null}\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="repair shadcn profile")

            state = {
                "context": ctx,
                "input": "repair shadcn profile",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": "import { Avatar, AvatarFallback } from '@/components/ui/avatar';\nexport default function App(){ return <Avatar size=\"lg\"><AvatarFallback>AP</AvatarFallback></Avatar> }\n",
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-style-runtime"]["ok"])
        self.assertIn("Avatar", verification["frontend-style-runtime"]["detail"])
        self.assertIn("size", verification["frontend-style-runtime"]["detail"])

    def test_verifier_blocks_custom_classes_without_css_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "pages").mkdir(parents=True)
            (project_dir / "src" / "app.css").write_text(
                ".templatePanel { border: 1px solid #ddd; }\n",
                encoding="utf-8",
            )
            ctx = self._ctx(ws_root, prompt="add QA readiness section")

            state = {
                "context": ctx,
                "input": "add QA readiness section",
                "changes": [{
                    "path": "src/pages/Dashboard.tsx",
                    "new_content": (
                        "export default function Dashboard() { return <section className=\"templatePanel\">"
                        "<div className=\"qaList\"><span className=\"qaItem\">Build passes</span></div>"
                        "</section>; }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-style-runtime"]["ok"])
        self.assertIn("qaList", verification["frontend-style-runtime"]["detail"])
        self.assertIn("qaItem", verification["frontend-style-runtime"]["detail"])

    def test_verifier_blocks_custom_classes_without_css_definition_in_workspace_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "pages").mkdir(parents=True)
            (project_dir / "src" / "app.css").write_text(".templatePanel { border: 1px solid #ddd; }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="add QA readiness section", build_mode="hybrid")

            state = {
                "context": ctx,
                "input": "add QA readiness section",
                "changes": [{
                    "path": "src/pages/Dashboard.tsx",
                    "new_content": (
                        "export default function Dashboard() { return <section className=\"templatePanel\">"
                        "<div className=\"qaList\"><span className={true ? \"qaItem done\" : \"qaItem\"}>Build passes</span></div>"
                        "</section>; }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-style-runtime"]["ok"])
        self.assertIn("qaList", verification["frontend-style-runtime"]["detail"])
        self.assertIn("qaItem", verification["frontend-style-runtime"]["detail"])

    def test_verifier_accepts_custom_classes_defined_in_changed_css(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "pages").mkdir(parents=True)
            ctx = self._ctx(ws_root, prompt="add QA readiness section")

            state = {
                "context": ctx,
                "input": "add QA readiness section",
                "changes": [
                    {
                        "path": "src/pages/Dashboard.tsx",
                        "new_content": (
                            "export default function Dashboard() { return <section className=\"qaList\">"
                            "<span className=\"qaItem\">Build passes</span></section>; }\n"
                        ),
                    },
                    {
                        "path": "src/app.css",
                        "new_content": ".qaList { display: grid; gap: 8px; }\n.qaItem { font-weight: 700; }\n",
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["frontend-style-runtime"]["ok"])

    def test_verifier_ignores_classname_template_expression_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components").mkdir(parents=True)
            ctx = self._ctx(ws_root, prompt="add app sidebar")

            state = {
                "context": ctx,
                "input": "add app sidebar",
                "changes": [
                    {
                        "path": "src/components/Sidebar.tsx",
                        "new_content": (
                            "export default function Sidebar({ modules, active }) { return <nav>"
                            "{modules.map((mod) => <button className={`sidebarNav ${active === mod.id ? \"active\" : \"\"}`} "
                            "aria-current={active === mod.id ? 'page' : undefined}>{mod.label}</button>)}"
                            "</nav> }\n"
                        ),
                    },
                    {
                        "path": "src/app.css",
                        "new_content": ".sidebarNav { display: flex; }\n.active { color: red; }\n",
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["frontend-style-runtime"]["ok"], verification["frontend-style-runtime"]["detail"])

    def test_verifier_accepts_compound_css_selector_classes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            ctx = self._ctx(ws_root, prompt="add error state")

            state = {
                "context": ctx,
                "input": "add error state",
                "changes": [
                    {
                        "path": "src/App.tsx",
                        "new_content": (
                            "export default function App() { return <div className=\"stateBox error\" role=\"alert\">"
                            "Invalid rule</div> }\n"
                        ),
                    },
                    {
                        "path": "src/app.css",
                        "new_content": ".stateBox { padding: 12px; }\n.stateBox.error { border-color: #a33b2f; }\n",
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["frontend-style-runtime"]["ok"], verification["frontend-style-runtime"]["detail"])

    def test_verifier_ignores_dynamic_class_prefix_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            ctx = self._ctx(ws_root, prompt="add severity list")

            state = {
                "context": ctx,
                "input": "add severity list",
                "changes": [
                    {
                        "path": "src/App.tsx",
                        "new_content": (
                            "export default function App() { const severity = 'critical'; "
                            "return <span className={`severity-${severity} incidentTime`}>Critical</span> }\n"
                        ),
                    },
                    {
                        "path": "src/app.css",
                        "new_content": ".severity-critical { color: #b42318; }\n.incidentTime { font-size: 12px; }\n",
                    },
                ],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["frontend-style-runtime"]["ok"], verification["frontend-style-runtime"]["detail"])

    def test_verifier_blocks_placeholder_media_in_full_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat landing page warung bakso yang siap dipakai")

            state = {
                "context": ctx,
                "input": "buat landing page warung bakso yang siap dipakai",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "const menu = [{ name: 'Bakso Urat', image: 'https://placehold.co/300x200?text=Bakso' }];\n"
                        "export default function App() { return <main><h1>Warung Bakso</h1>"
                        "{menu.map(item => <img key={item.name} src={item.image} alt={item.name} />)}</main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-asset-quality"]["ok"])
        self.assertEqual(verification["frontend-asset-quality"]["severity"], "hard")
        self.assertIn("placeholder media", verification["frontend-asset-quality"]["detail"])
        self.assertIn("frontend-asset-quality", result["context"].trace_task_state["blocking_checks"])

    def test_verifier_blocks_missing_explicit_uploaded_asset_alias_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            asset_path = project_dir / "public" / "uploads" / "bakso.jpg"
            asset_path.parent.mkdir(parents=True)
            asset_path.write_bytes(b"fake-image")
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            rel = "demo/public/uploads/bakso.jpg"
            req = SimpleNamespace(
                input="pake @hero sebagai hero landing page",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[rel],
                asset_aliases={rel: "hero"},
            )
            ctx = prepare_agent_context(req, ws_root)

            state = {
                "context": ctx,
                "input": "pake @hero sebagai hero landing page",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": "export default function App() { return <main><h1>Warung Bakso</h1></main> }\n",
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["referenced-asset-usage"]["ok"])
        self.assertEqual(verification["referenced-asset-usage"]["severity"], "hard")
        self.assertIn("@hero", verification["referenced-asset-usage"]["detail"])
        self.assertIn("referenced-asset-usage", result["context"].trace_task_state["blocking_checks"])

    def test_verifier_enforces_uploaded_asset_alias_even_when_file_not_hydrated_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            rel = "demo/public/uploads/bakso.jpg"
            req = SimpleNamespace(
                input="pake @hero sebagai hero landing page",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[rel],
                asset_aliases={rel: "hero"},
            )
            ctx = prepare_agent_context(req, ws_root)

            self.assertIn(rel, ctx.attached_assets)
            self.assertIn("@hero", ctx.asset_prompt)
            self.assertTrue(any("not found" in item["message"] for item in ctx.trace_warnings))

            state = {
                "context": ctx,
                "input": "pake @hero sebagai hero landing page",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": "export default function App() { return <main><h1>Warung Bakso</h1></main> }\n",
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["referenced-asset-usage"]["ok"])
        self.assertIn("@hero", verification["referenced-asset-usage"]["detail"])

    def test_verifier_accepts_explicit_uploaded_asset_alias_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            asset_path = project_dir / "public" / "uploads" / "bakso.jpg"
            asset_path.parent.mkdir(parents=True)
            asset_path.write_bytes(b"fake-image")
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            rel = "demo/public/uploads/bakso.jpg"
            req = SimpleNamespace(
                input="pake @hero sebagai hero landing page",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                asset_paths=[rel],
                asset_aliases={rel: "hero"},
            )
            ctx = prepare_agent_context(req, ws_root)

            state = {
                "context": ctx,
                "input": "pake @hero sebagai hero landing page",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main><img src=\"/uploads/bakso.jpg\" "
                        "alt=\"Bakso rumahan\" /><h1>Warung Bakso</h1></main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["referenced-asset-usage"]["ok"])

    def test_verifier_blocks_fake_business_contact_in_full_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat landing page warung bakso dengan nomor wa")

            state = {
                "context": ctx,
                "input": "buat landing page warung bakso dengan nomor wa",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "const whatsappNumber = '6281234567890';\n"
                        "export default function App() { return <main><h1>Warung Bakso</h1>"
                        "<a href={`https://wa.me/${whatsappNumber}`}>Pesan via WA</a>"
                        "<p>Jl. Contoh No. 123</p></main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-business-data-honesty"]["ok"])
        self.assertEqual(verification["frontend-business-data-honesty"]["severity"], "hard")
        self.assertIn("fake business contact", verification["frontend-business-data-honesty"]["detail"])
        self.assertIn("frontend-business-data-honesty", result["context"].trace_task_state["blocking_checks"])

    def test_verifier_blocks_fake_business_operational_claims_in_full_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat landing page warung bakso lokal tanpa data palsu")

            state = {
                "context": ctx,
                "input": "buat landing page warung bakso lokal tanpa data palsu",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main>"
                        "<h1>Bakso Sehat</h1><p>Warung lokal yang menjaga rasa sejak 2015.</p>"
                        "<strong>10K+ pelanggan puas</strong><span>500+ pesanan harian</span>"
                        "<span>4.9 rating kepuasan</span><p>Buka 07:00 - 21:00 WIB</p>"
                        "<p>Delivery tersedia untuk area [TAMBAHKAN NAMA KOTA]</p>"
                        "</main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-business-data-honesty"]["ok"])
        self.assertIn("fake business", verification["frontend-business-data-honesty"]["detail"])
        self.assertIn("frontend-business-data-honesty", result["context"].trace_task_state["blocking_checks"])

    def test_backend_verifier_repair_prompt_explains_business_data_honesty_fix(self) -> None:
        req = main_mod.AgentReq(
            input="buat landing page bakso, jangan bikin nomor WA palsu",
            project_root="demo",
            build_mode="full-agent",
        )
        prompt = main_mod._build_backend_verifier_repair_prompt(req, {
            "verification": [{
                "name": "frontend-business-data-honesty",
                "ok": False,
                "severity": "hard",
                "detail": "src/App.jsx: fake business contact/data detected (6281234567890)",
            }]
        })

        self.assertIn("remove fake business contact", prompt)
        self.assertIn("disabled/configuration-gated", prompt)
        self.assertIn("Do not mention WhatsApp", prompt)

    def test_verifier_blocks_dead_frontend_interactions_in_full_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat landing page jasa yang siap dipakai")

            state = {
                "context": ctx,
                "input": "buat landing page jasa yang siap dipakai",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main><h1>Studio Renovasi</h1>"
                        "<a href=\"#\">Konsultasi sekarang</a>"
                        "<button onClick={() => alert('coming soon')}>Lihat paket</button>"
                        "</main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-interaction-integrity"]["ok"])
        self.assertEqual(verification["frontend-interaction-integrity"]["severity"], "hard")
        self.assertIn("dead frontend interaction", verification["frontend-interaction-integrity"]["detail"])
        self.assertIn("frontend-interaction-integrity", result["context"].trace_task_state["blocking_checks"])

    def test_verifier_blocks_inert_visible_frontend_buttons_in_full_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat landing page premium")

            state = {
                "context": ctx,
                "input": "buat landing page premium",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "import Button from './Button';\n"
                        "export default function App() { return <main>"
                        "<button type=\"button\">Lihat Menu</button>"
                        "<Button type=\"button\">Cek Area Delivery</Button>"
                        "</main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-interaction-integrity"]["ok"])
        self.assertIn("inert visible button", verification["frontend-interaction-integrity"]["detail"])

    def test_verifier_allows_real_frontend_section_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat landing page jasa yang siap dipakai")

            state = {
                "context": ctx,
                "input": "buat landing page jasa yang siap dipakai",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main><nav>"
                        "<a href=\"#menu\">Lihat menu</a></nav>"
                        "<section id=\"menu\"><h2>Menu</h2></section></main> }\n"
                    ),
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(verification["frontend-interaction-integrity"]["ok"])

    def test_verifier_warns_on_excessive_inline_style_full_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            ctx = self._ctx(ws_root, prompt="buat dashboard task tracker profesional")
            repeated_inline_blocks = "\n".join(
                f"<div style={{{{ padding: '{idx}px' }}}}>Card {idx}</div>"
                for idx in range(26)
            )

            state = {
                "context": ctx,
                "input": "buat dashboard task tracker profesional",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": f"export default function App() {{ return <main>{repeated_inline_blocks}</main> }}\n",
                }],
                "actions": [],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertFalse(verification["frontend-maintainability-integrity"]["ok"])
        self.assertEqual(verification["frontend-maintainability-integrity"]["severity"], "advisory")
        self.assertIn("excessive inline styles", verification["frontend-maintainability-integrity"]["detail"])
        self.assertNotIn("frontend-maintainability-integrity", result["context"].trace_task_state["blocking_checks"])

    def test_verifier_promotes_concrete_auto_execute_work_and_drops_raw_tool_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            req = SimpleNamespace(
                input="hi",
                project_root="demo",
                build_mode="full-agent",
                active_file="src/App.tsx",
                open_files=["src/App.tsx"],
                current_content=None,
                selection=None,
                preview_url=None,
                editor_status=None,
                auto_execute=True,
                asset_paths=[],
            )
            ctx = prepare_agent_context(req, ws_root)
            ctx.intent = AgentIntent(
                kind="conversation",
                confidence=0.96,
                rationale="forced regression fixture",
                should_write_files=False,
                should_run_tools=False,
                wants_app_builder=False,
            )
            state = {
                "context": ctx,
                "input": req.input,
                "spoken": "Aku nemu preview kosong dan sudah patch route awal.",
                "changes": [{"path": "src/App.tsx", "new_content": "export default function App() { return <main>Ready</main> }\n"}],
                "actions": [{"type": "tool", "tool": "repo_search", "arguments": {"query": "App"}}],
            }
            result = _verify_node(state)

        verification = {item["name"]: item for item in result["context"].trace_verification}
        self.assertTrue(result["context"].intent.should_write_files)
        self.assertEqual(result["context"].intent.kind, "command")
        self.assertTrue(verification["has-work-output"]["ok"])
        self.assertTrue(verification["no-unexecuted-tool-actions"]["ok"])
        self.assertNotIn("read-only-boundary", verification)
        self.assertEqual(result["actions"], [])
        self.assertTrue(any(warning["phase"] == "intent-promotion" for warning in result["context"].trace_warnings))


class MemoryRetrievalRegressionTests(unittest.TestCase):
    def test_local_vector_memory_retrieval_prefers_relevant_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            docs_dir = project_dir / "docs"
            docs_dir.mkdir(parents=True)
            (docs_dir / "rag.md").write_text(
                "Supabase RAG retrieval uses chunk sync, vector ranking, and fallback warnings for agent memory.\n"
                "Responsive audit and accessibility checks are also part of the current agent quality lane.\n",
                encoding="utf-8",
            )
            (docs_dir / "other.md").write_text(
                "This file talks about unrelated CLI aliases and shell notes only.\n",
                encoding="utf-8",
            )

            with patch("api.agent_memory.has_supabase", return_value=False):
                hits = retrieve_agent_memory(
                    ws_root,
                    project_dir=project_dir,
                    project_root="demo",
                    interaction_kind="inspection",
                    query="supabase rag vector retrieval responsive accessibility",
                    active_rel="src/App.tsx",
                    open_files=["src/App.tsx"],
                    limit_long=3,
                )

            self.assertEqual(hits.backend, "local-hash-vector-chunks")
            self.assertGreaterEqual(len(hits.long_term), 1)
            self.assertIn("rag.md", hits.long_term[0].source)
            self.assertIn("LONG-TERM MEMORY (local-hash-vector-chunks)", hits.prompt)

    def test_supabase_vector_memory_retrieval_uses_remote_chunks_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            docs_dir = project_dir / "docs"
            docs_dir.mkdir(parents=True)
            (docs_dir / "local.md").write_text("Local doc about supabase RAG.", encoding="utf-8")

            remote_rows = [
                {
                    "project_root": "demo",
                    "source_path": "docs/remote.md",
                    "title": "Remote",
                    "content": "Supabase RAG remote chunk about vector retrieval and tool calling.",
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "content_hash": "hash1",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]

            with patch("api.agent_memory.has_supabase", return_value=True), \
                patch("api.agent_memory.get_agent_memory_chunks_table_status", return_value="ready"), \
                patch("api.agent_memory._sync_supabase_doc_chunks", return_value=True), \
                patch("api.agent_memory.list_agent_memory_chunks", return_value=remote_rows):
                hits = retrieve_agent_memory(
                    ws_root,
                    project_dir=project_dir,
                    project_root="demo",
                    interaction_kind="inspection",
                    query="vector retrieval supabase rag",
                    active_rel="src/App.tsx",
                    open_files=["src/App.tsx"],
                    limit_long=2,
                )

        self.assertEqual(hits.backend, "supabase-hash-vector-chunks")
        self.assertTrue(hits.long_term)
        self.assertEqual(hits.long_term[0].source, "docs/remote.md")
        self.assertIn("LONG-TERM MEMORY (supabase-hash-vector-chunks)", hits.prompt)

    def test_supabase_real_embedding_memory_retrieval_uses_remote_embedding_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            docs_dir = project_dir / "docs"
            docs_dir.mkdir(parents=True)
            (docs_dir / "local.md").write_text("Local fallback chunk.", encoding="utf-8")
            remote_rows = [
                {
                    "source_path": "docs/wrong.md",
                    "title": "docs/wrong.md",
                    "content": "Unrelated old route notes.",
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "content_hash": "hash-wrong",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "embedding": [0.0, 1.0, 0.0],
                    "embedding_model": "text-embedding-3-small",
                },
                {
                    "source_path": "docs/real.md",
                    "title": "docs/real.md",
                    "content": "Real embedding chunk about preview rollback and build outcome memory.",
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "content_hash": "hash-real",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "embedding": [1.0, 0.0, 0.0],
                    "embedding_model": "text-embedding-3-small",
                },
            ]

            with patch("api.agent_memory.has_supabase", return_value=True), \
                patch("api.agent_memory.get_agent_memory_chunks_table_status", return_value="ready"), \
                patch("api.agent_memory._real_embedding_backend_ready", return_value=True), \
                patch("api.agent_memory._embed_real_texts", return_value=[[1.0, 0.0, 0.0]]), \
                patch("api.agent_memory._sync_supabase_doc_chunks", return_value=True), \
                patch("api.agent_memory.list_agent_memory_chunks", return_value=remote_rows):
                hits = retrieve_agent_memory(
                    ws_root,
                    project_dir=project_dir,
                    project_root="demo",
                    interaction_kind="command",
                    query="preview rollback build outcome memory",
                    active_rel="src/App.tsx",
                    open_files=["src/App.tsx"],
                    limit_long=2,
                )

        self.assertEqual(hits.backend, "supabase-real-embedding-chunks")
        self.assertTrue(hits.long_term)
        self.assertEqual(hits.long_term[0].source, "docs/real.md")
        self.assertIn("LONG-TERM MEMORY (supabase-real-embedding-chunks)", hits.prompt)

    def test_project_profile_memory_is_persisted_and_retrieved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            src_dir = project_dir / "src"
            src_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps({"dependencies": {"react": "^19.0.0", "vite": "^7.0.0", "@supabase/supabase-js": "^2.0.0"}}),
                encoding="utf-8",
            )
            (project_dir / "tsconfig.json").write_text("{}", encoding="utf-8")
            (src_dir / "app.css").write_text(":root { color-scheme: light; }", encoding="utf-8")

            remember_agent_run(
                ws_root,
                project_root="demo",
                build_mode="full-agent",
                interaction_kind="command",
                user_input="Bikin UI minimalist elegant buat deploy Vercel dan Supabase",
                spoken="Updated the hosted app shell.",
                changes=[
                    {"path": "demo/src/App.tsx", "new_content": "export default function App() { return null }"},
                    {"path": "demo/src/app.css", "new_content": "body { margin: 0 }"},
                ],
                actions=[{"type": "shell", "command": "npm run build"}],
                task_state={
                    "goal": "Bikin UI minimalist elegant buat deploy Vercel dan Supabase",
                    "status": "blocked",
                    "next_action": "Finish preview polish then rerun validation.",
                    "horizon": {
                        "enabled": True,
                        "status": "blocked",
                        "current_checkpoint": "preview-polish",
                        "checkpoints": [
                            {"id": "context", "title": "Read project shape", "status": "done"},
                            {"id": "preview-polish", "title": "Polish preview UI", "status": "blocked"},
                            {"id": "validation", "title": "Validate build", "status": "pending"},
                        ],
                        "completion_criteria": ["Preview feels production-ready", "Build passes"],
                    },
                },
            )

            with patch("api.agent_memory.has_supabase", return_value=False):
                hits = retrieve_agent_memory(
                    ws_root,
                    project_dir=project_dir,
                    project_root="demo",
                    interaction_kind="command",
                    query="lanjut polish UI supabase vercel",
                    active_rel="src/App.tsx",
                    open_files=["src/App.tsx"],
                    limit_short=4,
                    limit_long=0,
                )

            overview = get_agent_memory_overview(ws_root, project_root="demo")
            self.assertTrue(overview.has_project_profile)
            self.assertIsNotNone(overview.project_profile_updated_at)
            self.assertIn("PROJECT MEMORY PROFILE", hits.prompt)
            self.assertIn("React", hits.prompt)
            self.assertIn("Supabase-backed hosted workflow", hits.prompt)
            self.assertIn("minimalist, elegant", hits.prompt)
            self.assertIn("Horizon: current=preview-polish", hits.prompt)
            self.assertIn("context=done, preview-polish=blocked, validation=pending", hits.prompt)

    def test_execution_outcome_memory_is_persisted_and_retrieved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")

            remember_agent_run(
                ws_root,
                project_root="demo",
                build_mode="full-agent",
                interaction_kind="command",
                user_input="Perbaiki preview blank dan validasi build",
                spoken="Patched render path.",
                changes=[{"path": "demo/src/App.tsx", "new_content": "export default function App(){return <main>Ready</main>}"}],
                actions=[{"type": "shell", "command": "npm run build"}],
                execution_outcome={
                    "ok": True,
                    "summary": "Complete: backend execution criteria passed.",
                    "state": "completed",
                    "validation_commands": ["npm run build"],
                    "validation_ok": True,
                    "preview_ok": True,
                    "preview_summary": "mode=browser; h1=Ready; blocking=0",
                    "repair_passes": 1,
                    "rollback_count": 1,
                    "rollback_paths": ["demo/src/App.tsx"],
                    "final_changed_paths": ["demo/src/App.tsx", "demo/src/app.css"],
                },
            )

            with patch("api.agent_memory.has_supabase", return_value=False):
                hits = retrieve_agent_memory(
                    ws_root,
                    project_dir=project_dir,
                    project_root="demo",
                    interaction_kind="command",
                    query="lanjut preview build repair outcome",
                    active_rel="src/App.tsx",
                    open_files=["src/App.tsx"],
                    limit_short=4,
                    limit_long=0,
                )

        self.assertIn("EXECUTION OUTCOME", hits.prompt)
        self.assertIn("validation=passed", hits.prompt)
        self.assertIn("preview=passed", hits.prompt)
        self.assertIn("repairs=1", hits.prompt)
        self.assertIn("rollbacks=1", hits.prompt)
        self.assertIn("final_changed_paths=demo/src/App.tsx, demo/src/app.css", hits.prompt)


class PreviewAuditRegressionTests(unittest.TestCase):
    def test_quality_checks_cover_responsive_a11y_and_states(self) -> None:
        html = (
            "<!doctype html><html lang='en'><head>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Preview</title></head><body>"
            "<header>Hero</header><nav>Main nav</nav><main>"
            "<h1>Landing page</h1>"
            "<form><label for='email'>Email</label><input id='email' /></form>"
            "<img src='hero.png' alt='Hero image' />"
            "</main><footer>Footer</footer></body></html>"
        )
        snapshot = _extract_preview_snapshot_from_html(html)
        checks = _build_quality_checks(
            snapshot,
            project_signals={
                "responsive": True,
                "loading": True,
                "error": False,
                "empty": True,
                "labels": True,
            },
        )
        by_id = {str(item["id"]): item for item in checks}

        self.assertTrue(by_id["responsive-foundation"]["ok"])
        self.assertTrue(by_id["a11y-landmarks"]["ok"])
        self.assertTrue(by_id["a11y-alt-text"]["ok"])
        self.assertTrue(by_id["a11y-form-labels"]["ok"])
        self.assertTrue(by_id["state-loading"]["ok"])
        self.assertTrue(by_id["state-empty"]["ok"])
        self.assertFalse(by_id["state-error"]["ok"])
        self.assertTrue(by_id["starter-residue"]["ok"])
        self.assertTrue(by_id["source-type-discipline"]["ok"])

    def test_browser_quality_checks_flag_actionable_dom_issues(self) -> None:
        snapshot = {
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "input_count": 0,
            "labeled_input_count": 0,
            "images_missing_alt": 0,
            "mobile_overflow_x": True,
            "unlabeled_interactive": ["button.icon-only"],
            "mobile_small_tap_targets": ["a.nav (18x20)"],
            "mobile_text_overflow_nodes": ["h1.hero \"Very long heading\""],
            "broken_images": ["hero.png"],
            "mobile_fixed_overlays": ["div.modal"],
        }
        checks = _build_quality_checks(snapshot, project_signals={"loading": True, "error": True, "empty": True})
        by_id = {str(item["id"]): item for item in checks}

        self.assertFalse(by_id["responsive-overflow"]["ok"])
        self.assertFalse(by_id["a11y-interactive-labels"]["ok"])
        self.assertFalse(by_id["mobile-tap-targets"]["ok"])
        self.assertFalse(by_id["mobile-text-fit"]["ok"])
        self.assertFalse(by_id["image-loads"]["ok"])
        self.assertFalse(by_id["blocking-overlays"]["ok"])

    def test_sr_only_labels_do_not_count_as_mobile_text_overflow(self) -> None:
        raw_nodes = [
            'main.container > section.card > div.flex > label.sr-only "Task Title"',
            'main.container > section.card > div.flex > label.visually-hidden "Filter Tasks"',
            'h1.hero "Very long heading"',
        ]

        self.assertEqual(_filter_visual_text_overflow_nodes(raw_nodes), ['h1.hero "Very long heading"'])

        snapshot = {
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "input_count": 2,
            "labeled_input_count": 2,
            "images_missing_alt": 0,
            "mobile_overflow_x": False,
            "mobile_text_overflow_nodes": raw_nodes[:2],
        }
        checks = _build_quality_checks(snapshot, project_signals={"loading": True, "error": True, "empty": True})
        by_id = {str(item["id"]): item for item in checks}

        self.assertTrue(by_id["mobile-text-fit"]["ok"])

    def test_project_signal_scan_marks_fetch_ui_as_dynamic_state_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "src").mkdir()
            (project / "src" / "Dashboard.tsx").write_text(
                "import { useEffect, useState } from 'react';\n"
                "export function Dashboard() {\n"
                "  const [rows, setRows] = useState([]);\n"
                "  useEffect(() => { fetch('/api/tasks').then((res) => res.json()).then(setRows); }, []);\n"
                "  return <main><h1>Task dashboard</h1>{rows.map((row) => <p>{row.name}</p>)}</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            signals = main_mod._scan_project_quality_signals(project)

        self.assertTrue(signals["dynamic_state_required"])

    def test_dynamic_source_requires_state_checks_even_when_dom_is_simple(self) -> None:
        snapshot = {
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "input_count": 0,
            "labeled_input_count": 0,
            "images_missing_alt": 0,
            "mobile_overflow_x": False,
            "headings": ["Task dashboard"],
            "buttons": [],
            "links": [],
            "section_count": 5,
            "card_like_count": 4,
            "product_surface_count": 2,
            "excerpt": "Task dashboard for operations.",
        }

        checks = _build_quality_checks(snapshot, project_signals={"dynamic_state_required": True})
        by_id = {str(item["id"]): item for item in checks}

        self.assertFalse(by_id["state-loading"]["ok"])
        self.assertFalse(by_id["state-error"]["ok"])
        self.assertFalse(by_id["state-empty"]["ok"])

    def test_dense_dashboard_surface_satisfies_product_depth_without_landing_sections(self) -> None:
        snapshot = {
            "title": "OpsPulse - Task Operations Dashboard",
            "meta_description": "Dashboard for task operations.",
            "headings": ["OpsPulse"],
            "subheadings": ["Task Operations Dashboard"],
            "buttons": ["Add task", "Retry", "Save"],
            "links": [],
            "section_count": 3,
            "card_like_count": 10,
            "product_surface_count": 0,
            "table_count": 0,
            "word_count": 130,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 15,
            "form_count": 1,
            "input_count": 2,
            "labeled_input_count": 2,
            "excerpt": "OpsPulse task operations dashboard with search, filter, add form, metrics, loading, error, and empty states.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 2,
            "mobile_overflow_x": False,
        }

        audit = _build_preview_audit_result(
            "http://127.0.0.1:4173",
            snapshot,
            audit_mode="browser",
            project_signals={"loading": True, "error": True, "empty": True},
        )

        by_id = {str(item["id"]): item for item in audit["quality_checks"]}
        self.assertTrue(by_id["product-depth"]["ok"])
        self.assertFalse(any(item["category"] == "product-depth" for item in audit["issue_details"]))

    def test_dense_table_dashboard_satisfies_product_depth_without_landing_sections(self) -> None:
        snapshot = {
            "title": "Task Tracker Tim Produk",
            "meta_description": "Dashboard operasi tugas tim produk.",
            "headings": ["Task Tracker Tim Produk"],
            "subheadings": [],
            "buttons": ["Semua", "Menunggu", "Dikerjakan", "Selesai", "Hapus"],
            "links": [],
            "section_count": 0,
            "card_like_count": 5,
            "product_surface_count": 2,
            "table_count": 1,
            "word_count": 104,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 24,
            "form_count": 0,
            "input_count": 0,
            "labeled_input_count": 0,
            "excerpt": "Task tracker dengan metrik status, filter, tabel tugas, prioritas, assignee, aksi hapus, dan progress.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 2,
            "mobile_overflow_x": False,
        }

        audit = _build_preview_audit_result(
            "http://127.0.0.1:4173",
            snapshot,
            audit_mode="browser",
            project_signals={"loading": True, "error": True, "empty": True},
        )

        by_id = {str(item["id"]): item for item in audit["quality_checks"]}
        self.assertTrue(by_id["product-depth"]["ok"])
        self.assertFalse(any(item["category"] == "product-depth" for item in audit["issue_details"]))

    def test_compact_interactive_dashboard_satisfies_product_depth(self) -> None:
        snapshot = {
            "title": "Product Team Dashboard - Task Tracker Tim Produk",
            "meta_description": "Mini dashboard task tracker untuk tim produk.",
            "headings": ["Product Team Dashboard"],
            "subheadings": ["Priority", "Progress", "Review"],
            "buttons": ["All", "Todo", "In Progress", "Review", "Done", "View detail", "Retry", "Close"],
            "links": [],
            "section_count": 1,
            "card_like_count": 4,
            "product_surface_count": 2,
            "table_count": 0,
            "word_count": 74,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 10,
            "form_count": 0,
            "input_count": 0,
            "labeled_input_count": 0,
            "excerpt": "Product team dashboard with task cards, priorities, assignees, progress, review states, retry state, and detail workflow.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 2,
            "mobile_overflow_x": False,
        }

        audit = _build_preview_audit_result(
            "http://127.0.0.1:4173",
            snapshot,
            audit_mode="browser",
            project_signals={"loading": True, "error": True, "empty": True},
        )

        by_id = {str(item["id"]): item for item in audit["quality_checks"]}
        self.assertTrue(by_id["product-depth"]["ok"])
        self.assertFalse(any(item["category"] == "product-depth" for item in audit["issue_details"]))

    def test_preview_audit_flags_starter_residue_and_generic_polish(self) -> None:
        snapshot = {
            "title": "FlowPilot",
            "meta_description": "Task manager",
            "headings": ["FlowPilot"],
            "subheadings": ["Built for teams"],
            "buttons": ["Start free trial"],
            "links": ["Home", "Vite"],
            "section_count": 2,
            "card_like_count": 1,
            "product_surface_count": 0,
            "table_count": 0,
            "word_count": 120,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 3,
            "excerpt": "FlowPilot Seeded template React + Vite + TS ✨ Task management reimagined. Streamline every workflow with an all-in-one platform.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "mobile_overflow_x": False,
        }
        audit = _build_preview_audit_result(
            "http://127.0.0.1:4173",
            snapshot,
            audit_mode="browser",
            project_signals={
                "inline_style_count": 14,
                "any_cast_count": 1,
                "emoji_count": 6,
                "layout_overflow_risk_count": 2,
                "layout_overflow_risks": ["src/app.css:8: .panel { min-width: 720px; }"],
                "generic_copy_count": 2,
                "loading": True,
                "error": True,
                "empty": True,
            },
        )

        self.assertFalse(audit["ok"])
        categories = {item["category"] for item in audit["issue_details"]}
        self.assertIn("production-polish", categories)
        self.assertIn("source-quality", categories)
        self.assertIn("visual-polish", categories)
        self.assertIn("responsive", categories)
        self.assertIn("product-depth", categories)
        self.assertIn("copy-specificity", categories)
        self.assertIn("starter_residue", audit["visual_summary"])
        self.assertEqual(audit["visual_summary"]["layout_overflow_risk_count"], 2)
        self.assertGreater(audit["visual_summary"]["generic_copy_count"], 2)
        by_id = {str(item["id"]): item for item in audit["quality_checks"]}
        self.assertFalse(by_id["starter-residue"]["ok"])
        self.assertFalse(by_id["source-style-discipline"]["ok"])
        self.assertFalse(by_id["source-overflow-risk"]["ok"])
        self.assertFalse(by_id["product-depth"]["ok"])
        self.assertFalse(by_id["copy-specificity"]["ok"])

    def test_preview_audit_allows_vite_as_project_tech_stack(self) -> None:
        snapshot = {
            "title": "Arga Pratama | Web Developer Portfolio",
            "meta_description": "Portfolio for Arga Pratama",
            "headings": ["Hi, I'm Arga Pratama", "Skill Stack", "Project Showcase", "Experience", "Contact"],
            "subheadings": ["Frontend", "Backend", "Tools"],
            "buttons": ["View Work", "Contact"],
            "links": ["React", "TypeScript", "Vite", "GitHub", "LinkedIn"],
            "section_count": 6,
            "card_like_count": 5,
            "product_surface_count": 3,
            "table_count": 0,
            "word_count": 180,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 8,
            "excerpt": "Arga builds responsive React interfaces with TypeScript, Vite, accessibility checks, and production deployment workflows.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "mobile_overflow_x": False,
        }
        audit = _build_preview_audit_result(
            "http://127.0.0.1:4173",
            snapshot,
            audit_mode="browser",
            project_signals={"loading": True, "error": True, "empty": True},
        )

        by_id = {str(item["id"]): item for item in audit["quality_checks"]}
        self.assertTrue(by_id["starter-residue"]["ok"])
        self.assertNotIn("Vite", audit["visual_summary"]["starter_residue"])
        self.assertFalse(
            any(
                item["category"] == "production-polish" and "Vite" in item["detail"]
                for item in audit["issue_details"]
            )
        )

    def test_preview_audit_does_not_require_dynamic_states_for_static_portfolio(self) -> None:
        snapshot = {
            "title": "Arka Pratama - Frontend Portfolio",
            "meta_description": "Portfolio profesional Arka Pratama untuk frontend engineering.",
            "headings": ["Arka Pratama builds sharp web products that feel fast, clear, and production-ready."],
            "subheadings": ["Selected work", "Capability", "Experience"],
            "buttons": ["Configure contact email"],
            "links": ["Projects", "Skills", "Contact", "Lihat project", "Bahas kerja sama"],
            "section_count": 15,
            "card_like_count": 11,
            "product_surface_count": 2,
            "table_count": 0,
            "word_count": 297,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 6,
            "form_count": 0,
            "input_count": 0,
            "excerpt": "Arka Pratama builds responsive portfolio sections, selected work, skills, testimonials, and contact CTA.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "mobile_overflow_x": False,
        }

        audit = _build_preview_audit_result("http://127.0.0.1:4173", snapshot, audit_mode="browser")

        by_id = {str(item["id"]): item for item in audit["quality_checks"]}
        self.assertTrue(by_id["state-loading"]["ok"])
        self.assertTrue(by_id["state-error"]["ok"])
        self.assertTrue(by_id["state-empty"]["ok"])
        categories = {item["category"] for item in audit["issue_details"]}
        self.assertNotIn("state-loading", categories)
        self.assertNotIn("state-error", categories)
        self.assertNotIn("state-empty", categories)

    def test_preview_audit_returns_blocking_issue_details(self) -> None:
        snapshot = {
            "title": "Demo",
            "meta_description": "Demo app",
            "headings": ["Demo"],
            "buttons": ["Go"],
            "links": ["Home"],
            "word_count": 120,
            "image_count": 1,
            "images_missing_alt": 0,
            "interactive_count": 2,
            "broken_images": ["missing.png"],
            "unlabeled_interactive": ["button.icon-only"],
            "mobile_text_overflow_nodes": ["h1.hero"],
            "console_errors": [],
            "page_errors": [],
        }
        audit = _build_preview_audit_result("http://127.0.0.1:4173", snapshot, audit_mode="browser")

        self.assertFalse(audit["ok"])
        severities = {item["severity"] for item in audit["issue_details"]}
        self.assertIn("blocking", severities)
        self.assertTrue(any(item["category"] == "assets" for item in audit["issue_details"]))
        self.assertIn("repair_brief", audit)
        self.assertIn("visual_summary", audit)

    def test_preview_audit_returns_actionable_repair_targets(self) -> None:
        snapshot = {
            "title": "Demo",
            "meta_description": "Demo app",
            "headings": ["Demo"],
            "buttons": ["Go"],
            "links": ["Home"],
            "word_count": 120,
            "image_count": 1,
            "images_missing_alt": 0,
            "interactive_count": 2,
            "broken_images": ["img.hero"],
            "unlabeled_interactive": ["button.icon-only"],
            "mobile_text_overflow_nodes": ["h1.hero \"Very long heading\""],
            "small_tap_targets": ["a.nav (18x20)"],
            "mobile_overflow_x": True,
            "mobile_scroll_width": 620,
            "mobile_viewport_width": 390,
            "page_errors": ["src/App.tsx:12: ReferenceError: Hero is not defined"],
            "console_errors": [],
        }
        audit = _build_preview_audit_result(
            "http://127.0.0.1:4173",
            snapshot,
            audit_mode="agent-browser",
            project_signals={
                "layout_overflow_risks": ["src/app.css:9: .hero { min-width: 720px; }"],
                "repair_candidate_files": ["src/App.tsx", "src/app.css"],
            },
        )

        targets = audit["evidence_pack"]["repair_targets"]
        by_kind = {target["kind"]: target for target in targets}
        self.assertIn("runtime", by_kind)
        self.assertIn("responsive", by_kind)
        self.assertIn("accessibility", by_kind)
        self.assertIn("assets", by_kind)
        self.assertIn("src/App.tsx", by_kind["runtime"]["likely_files"])
        self.assertIn("src/app.css", by_kind["responsive"]["likely_files"])
        self.assertTrue(by_kind["accessibility"]["selectors"])
        self.assertIn("Repair target", audit["repair_brief"])

    def test_preview_audit_blocks_stale_starter_shell(self) -> None:
        snapshot = {
            "title": "Task tracker",
            "meta_description": "Task tracker",
            "headings": ["Starter"],
            "buttons": [],
            "links": [],
            "word_count": 4,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 0,
            "excerpt": "Starter Make this useful.",
            "console_errors": [],
            "page_errors": [],
        }
        audit = _build_preview_audit_result("http://127.0.0.1:4173", snapshot, audit_mode="browser")

        self.assertFalse(audit["ok"])
        blockers = [item for item in audit["issue_details"] if item["severity"] == "blocking"]
        self.assertTrue(any(item["category"] == "content" for item in blockers))
        self.assertTrue(any(item["category"] == "production-polish" for item in blockers))
        self.assertEqual(audit["visual_summary"]["mode"], "browser")
        self.assertTrue(audit["visual_summary"]["top_blockers"])
        self.assertIn("Top issues:", audit["repair_brief"])

    def test_preview_audit_flags_root_route_404_as_routing_blocker(self) -> None:
        snapshot = {
            "title": "Product Task Tracker",
            "meta_description": "Task tracker",
            "headings": ["404"],
            "buttons": ["Go back home"],
            "links": ["Dashboard", "Features", "Pricing"],
            "word_count": 34,
            "image_count": 0,
            "images_missing_alt": 0,
            "interactive_count": 6,
            "excerpt": "404 Page not found. Go back home.",
            "console_errors": [],
            "page_errors": [],
            "viewport_meta": True,
            "document_lang": "en",
            "main_count": 1,
            "landmark_count": 3,
            "mobile_overflow_x": False,
        }
        audit = _build_preview_audit_result("http://127.0.0.1:4173", snapshot, audit_mode="browser")

        self.assertFalse(audit["ok"])
        routing_blockers = [
            item for item in audit["issue_details"]
            if item["severity"] == "blocking" and item["category"] == "routing"
        ]
        self.assertTrue(routing_blockers)
        self.assertIn("root route", routing_blockers[0]["suggested_fix"].lower())
        self.assertIn("routing", audit["repair_brief"].lower())

    def test_browser_audit_is_runtime_capability_not_project_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "package.json").write_text('{"dependencies":{}}', encoding="utf-8")

            with patch.object(main_mod, "_resolve_agent_browser_binary", return_value="/usr/local/bin/agent-browser"):
                self.assertTrue(main_mod._browser_preview_audit_ready(project_dir))

    def test_agent_browser_preview_audit_collects_dom_snapshot(self) -> None:
        snapshot = {
            "title": "OpsFlow",
            "meta_description": "Operational dashboard for dispatch teams",
            "viewport_meta": True,
            "document_lang": "en",
            "headings": ["OpsFlow Dispatch"],
            "subheadings": ["Live queue"],
            "buttons": ["Create task", "Filter"],
            "links": ["Dashboard", "Reports"],
            "form_count": 1,
            "section_count": 5,
            "table_count": 1,
            "card_like_count": 4,
            "product_surface_count": 3,
            "input_count": 1,
            "labeled_input_count": 1,
            "landmark_count": 3,
            "main_count": 1,
            "button_count": 2,
            "interactive_count": 5,
            "unlabeled_interactive": [],
            "small_tap_targets": [],
            "fixed_overlays": [],
            "text_overflow_nodes": [],
            "word_count": 120,
            "image_count": 0,
            "images_missing_alt": 0,
            "broken_images": [],
            "scroll_width": 1440,
            "viewport_width": 1440,
            "viewport_height": 900,
            "excerpt": "OpsFlow Dispatch keeps queue, reports, loading state, error state, and empty state visible for operations teams.",
        }
        mobile_snapshot = {**snapshot, "scroll_width": 390, "viewport_width": 390, "viewport_height": 844}
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if "eval" in cmd:
                payload = mobile_snapshot if any(prev[-3:] == ["set", "viewport", "390"] for prev in calls) else snapshot
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
            if "console" in cmd or "errors" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.main._resolve_agent_browser_binary", return_value="/usr/bin/agent-browser"), \
            patch("api.main.subprocess.run", side_effect=fake_run):
            audit, warning = _run_agent_browser_preview_audit(
                "http://127.0.0.1:4173",
                Path(tmp),
                project_signals={"responsive": True, "loading": True, "error": True, "empty": True},
            )

        self.assertIsNone(warning)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["audit_mode"], "agent-browser")
        self.assertEqual(audit["visual_summary"]["mode"], "agent-browser")
        self.assertEqual(audit["evidence_pack"]["audit_mode"], "agent-browser")
        self.assertTrue(audit["visual_evidence"]["has_screen"])
        self.assertTrue(audit["visual_evidence"]["has_dom_snapshot"])
        self.assertEqual(audit["visual_evidence"]["screen_backend"], "agent-browser")
        self.assertIn("visual_evidence", audit["evidence_pack"])
        self.assertIn("repair_brief", audit["evidence_pack"])
        flattened = [" ".join(cmd) for cmd in calls]
        self.assertTrue(any("agent-browser --session-name" in item and "open http://127.0.0.1:4173" in item for item in flattened))
        self.assertTrue(any("set viewport 390 844" in item for item in flattened))

    def test_completion_report_records_visual_review_evidence(self) -> None:
        execution = {
            "ok": True,
            "apply": {"ok": True, "applied": 1, "count": 1},
            "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "audit_mode": "agent-browser",
                "issue_details": [],
                "visual_evidence": {
                    "has_screen": True,
                    "has_screenshot": True,
                    "screenshot_path": "/tmp/appora-preview-audit.png",
                    "screen_backend": "agent-browser",
                    "has_dom_snapshot": True,
                    "desktop_viewport": {"width": 1440, "height": 900},
                    "mobile_viewport": {"width": 390, "height": 844},
                },
            },
        }

        report = main_mod._execution_completion_report(execution)

        criteria = {item["label"]: item for item in report["criteria"]}
        self.assertEqual(criteria["visual-review"]["status"], "passed")
        self.assertIn("screen=agent-browser", criteria["visual-review"]["detail"])
        self.assertIn("screenshot=/tmp/appora-preview-audit.png", criteria["visual-review"]["detail"])

    def test_completion_report_warns_on_html_fallback_without_browser_visual_evidence(self) -> None:
        execution = {
            "ok": True,
            "apply": {"ok": True, "applied": 1, "count": 1},
            "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "audit_mode": "html",
                "issue_details": [],
                "visual_evidence": {
                    "has_screen": False,
                    "has_screenshot": False,
                    "has_dom_snapshot": True,
                },
            },
        }

        report = main_mod._execution_completion_report(execution)

        criteria = {item["label"]: item for item in report["criteria"]}
        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], "complete")
        self.assertEqual(criteria["visual-review"]["status"], "warning")
        self.assertTrue(any("fallback inspection" in item for item in report["residual_risks"]))

    def test_preview_audit_prefers_agent_browser_then_falls_back_to_playwright(self) -> None:
        playwright_audit = {
            "ok": True,
            "preview_url": "http://127.0.0.1:4173",
            "audit_mode": "browser",
            "runtime_warnings": [],
            "issue_details": [],
            "issues": [],
            "summary": "mode=browser; blocking=0; warnings=0",
        }

        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.main._ws", return_value=Path(tmp)), \
            patch("api.main._hydrate_hosted_project", return_value=None), \
            patch("api.main._scan_project_quality_signals", return_value={}), \
            patch("api.main._run_agent_browser_preview_audit", return_value=(None, "agent-browser failed")), \
            patch("api.main._run_playwright_preview_audit", return_value=(playwright_audit, None)):
            audit = main_mod.preview_audit(
                main_mod.PreviewAuditReq(preview_url="http://127.0.0.1:4173", project_root=".", mode="auto")
            )

        self.assertEqual(audit["audit_mode"], "browser")
        self.assertIn("agent-browser failed", audit["runtime_warnings"])


class CommandPolicyRegressionTests(unittest.TestCase):
    def test_infer_validation_commands_uses_python_stack_without_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "py-tools"
            (project / "app").mkdir(parents=True)
            (project / "tests").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]\nname='py-tools'\n", encoding="utf-8")
            (project / "app" / "metrics.py").write_text("def completion_rate(done, total):\n    return 0\n", encoding="utf-8")
            (project / "tests" / "test_metrics.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

            commands = main_mod._infer_validation_commands(project)

        self.assertIn("python3 -m unittest discover -s tests", commands)
        self.assertIn("python3 -m compileall .", commands)
        self.assertNotIn("npm run build", commands)

    def test_command_policy_allows_project_validation_commands(self) -> None:
        for command in [
            "npm run build",
            "npm test",
            "npm i typescript --save-dev",
            "pnpm add lucide-react",
            "yarn add @vitejs/plugin-react",
            "bun add clsx",
            "npm install && npm run build",
            "npm run build 2>&1",
            "cd my-first-porto && npm run build",
            "cd apps/web && pnpm run build",
            "git status --short",
            "git diff -- src/App.tsx",
            "ls src",
            "find src -maxdepth 2 -type f",
            "cat package.json",
            "sed -n 1,80p src/App.tsx",
            "tsc --noEmit",
            "vite build",
            "eslint src",
            "vitest run",
            "python3 -m compileall api",
            "python3 -m pytest",
            "go test ./...",
            "cargo test",
            "cargo check",
            "mvn test",
            "gradle test",
            "./gradlew test",
            "composer validate",
            "bundle exec rspec",
            "dotnet test",
            "terraform validate",
            "cd services/api && go test ./...",
        ]:
            with self.subTest(command=command):
                decision = _command_policy_decision(command)
                self.assertTrue(decision.ok)
                self.assertEqual(decision.risk_level, "safe")

    def test_command_policy_blocks_or_gates_risky_commands(self) -> None:
        blocked = _command_policy_decision("rm -rf src")
        gated = _command_policy_decision("git reset --hard")

        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.risk_level, "blocked")
        self.assertFalse(gated.ok)
        self.assertEqual(gated.risk_level, "approval_required")

        for command in ["npm run build; rm -rf src", "npm run build | bash", "npm install -g vercel", "cd .. && npm run build", "cd /tmp && npm run build", "sed -i s/a/b/ src/App.tsx", "cat ../secret.txt"]:
            with self.subTest(command=command):
                decision = _command_policy_decision(command)
                self.assertFalse(decision.ok)
                self.assertIn(decision.risk_level, {"approval_required", "blocked"})

    def test_command_policy_trusted_project_allows_broader_project_scoped_commands(self) -> None:
        safe_npx = _command_policy_decision("npx shadcn@latest add button")
        trusted_npx = _command_policy_decision("npx shadcn@latest add button", access_mode="trusted")
        safe_init = _command_policy_decision("npx shadcn@latest init -d --base radix")
        trusted_init = _command_policy_decision("npx shadcn@latest init -d --base radix", access_mode="trusted")
        trusted_custom = _command_policy_decision("node scripts/generate.js", access_mode="trusted")
        trusted_destructive = _command_policy_decision("rm -rf src", access_mode="trusted")
        trusted_escape = _command_policy_decision("cat ../secret.txt", access_mode="trusted")

        self.assertFalse(safe_npx.ok)
        self.assertEqual(safe_npx.risk_level, "approval_required")
        self.assertIn("shadcn", safe_npx.reason)
        self.assertTrue(trusted_npx.ok)
        self.assertFalse(safe_init.ok)
        self.assertIn("shadcn", safe_init.reason)
        self.assertTrue(trusted_init.ok)
        self.assertTrue(trusted_custom.ok)
        self.assertFalse(trusted_destructive.ok)
        self.assertFalse(trusted_escape.ok)

    def test_agent_harness_runs_shell_actions_with_policy_evidence(self) -> None:
        session_id = "harness-shell-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                (root / "ok.py").write_text("print('ok')\n", encoding="utf-8")
                req = main_mod.AgentHarnessRunShellReq(
                    project_root=".",
                    actions=[
                        main_mod.AgentHarnessShellAction(command="python3 -m compileall .", reason="validate python files"),
                        main_mod.AgentHarnessShellAction(command="rm -rf src", reason="unsafe destructive command"),
                    ],
                )

                result = main_mod.agent_harness_run_shell(req)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertEqual(result["ran"], 2)
        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["policy"]["risk_level"], "safe")
        self.assertFalse(result["results"][1]["ok"])
        self.assertEqual(result["results"][1]["returncode"], 126)
        self.assertIn(result["results"][1]["policy"]["risk_level"], {"blocked", "approval_required"})

    def test_agent_harness_normalizes_redundant_cd_into_project_root(self) -> None:
        session_id = "harness-normalize-cd-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "ok.py").write_text("print('ok')\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                req = main_mod.AgentHarnessRunShellReq(
                    project_root="demo",
                    actions=[
                        main_mod.AgentHarnessShellAction(command="cd demo && python3 -m compileall .", reason="agent used redundant cd"),
                    ],
                )

                result = main_mod.agent_harness_run_shell(req)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["command"], "python3 -m compileall .")
        self.assertEqual(result["results"][0]["original_command"], "cd demo && python3 -m compileall .")
        self.assertIn("redundant", result["results"][0]["normalization"])

    def test_agent_harness_normalizes_windows_cd_drive_flag_into_project_root(self) -> None:
        session_id = "harness-normalize-windows-cd-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "package.json").write_text(
                    json.dumps({"scripts": {"build": "node -e \"console.log('built')\""}}),
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                req = main_mod.AgentHarnessRunShellReq(
                    project_root="demo",
                    actions=[
                        main_mod.AgentHarnessShellAction(
                            command="cd /d /home/user/demo && npm run build",
                            reason="agent emitted Windows cd drive flag",
                        ),
                    ],
                )

                result = main_mod.agent_harness_run_shell(req)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["command"], "npm run build")
        self.assertEqual(result["results"][0]["original_command"], "cd /d /home/user/demo && npm run build")
        self.assertIn("Windows", result["results"][0]["normalization"])

    def test_agent_harness_normalizes_python_alias_to_python3(self) -> None:
        session_id = "harness-normalize-python-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "ok.py").write_text("print('ok')\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                req = main_mod.AgentHarnessRunShellReq(
                    project_root="demo",
                    actions=[
                        main_mod.AgentHarnessShellAction(command="cd demo && python -m compileall .", reason="agent used python alias"),
                    ],
                )

                result = main_mod.agent_harness_run_shell(req)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["command"], "python3 -m compileall .")
        self.assertEqual(result["results"][0]["original_command"], "cd demo && python -m compileall .")
        self.assertIn("python3", result["results"][0]["normalization"])

    def test_agent_harness_strips_harmless_capture_redirect(self) -> None:
        session_id = "harness-normalize-redirect-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "package.json").write_text(
                    json.dumps({"scripts": {"build": "node -e \"console.log('built')\""}}),
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                req = main_mod.AgentHarnessRunShellReq(
                    project_root="demo",
                    actions=[
                        main_mod.AgentHarnessShellAction(command="cd demo && npm run build 2>&1", reason="agent asked to capture stderr"),
                    ],
                )

                result = main_mod.agent_harness_run_shell(req)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["command"], "npm run build")
        self.assertEqual(result["results"][0]["original_command"], "cd demo && npm run build 2>&1")
        self.assertIn("stream redirection", result["results"][0]["normalization"])

    def test_agent_harness_localizes_project_prefixed_read_paths_inside_project_cwd(self) -> None:
        session_id = "harness-normalize-prefixed-read-path-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "src" / "App.tsx").write_text("export default null;\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                req = main_mod.AgentHarnessRunShellReq(
                    project_root="demo",
                    actions=[
                        main_mod.AgentHarnessShellAction(command="cat demo/src/App.tsx", reason="agent included project root in path"),
                    ],
                )

                result = main_mod.agent_harness_run_shell(req)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["command"], "cat src/App.tsx")
        self.assertIn("project-prefixed path", result["results"][0]["normalization"])


class MCPHintRegressionTests(unittest.TestCase):
    def test_suggest_mcp_actions_prefers_read_only_audit_tools(self) -> None:
        tool_catalog = {
            "browser": [
                MCPToolInfo(
                    server="browser",
                    name="browser_audit",
                    description="Browser audit and DOM snapshot for responsive and accessibility review",
                    input_schema={"type": "object", "properties": {}},
                    source="test",
                ),
                MCPToolInfo(
                    server="browser",
                    name="take_screenshot",
                    description="Capture a screenshot for layout review",
                    input_schema={"type": "object", "properties": {}, "required": ["path"]},
                    source="test",
                ),
            ],
            "repo": [
                MCPToolInfo(
                    server="repo",
                    name="search_code",
                    description="Search project files and inspect logs",
                    input_schema={"type": "object", "properties": {}},
                    source="test",
                )
            ],
        }

        actions = suggest_mcp_actions("audit responsive preview and inspect errors", tool_catalog, limit=3)
        action_pairs = [(item["server"], item["tool"]) for item in actions]

        self.assertIn(("browser", "browser_audit"), action_pairs)
        self.assertIn(("repo", "search_code"), action_pairs)
        self.assertNotIn(("browser", "take_screenshot"), action_pairs)

    def test_execute_mcp_tool_forwards_function_call_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            server = MCPServerInfo(
                name="repo",
                transport="stdio",
                target="repo-server",
                tools=["search_code"],
                source="test",
                command="repo-server",
            )
            expected = MCPToolCallResult(
                server="repo",
                tool="search_code",
                arguments={"query": "supabase rag"},
                ok=True,
                text="found matches",
                raw={"content": [{"type": "text", "text": "found matches"}]},
                duration_ms=12,
                error=None,
            )

            with patch("api.agent_mcp._resolve_server", return_value=server), \
                patch("api.agent_mcp._call_tool_async", new=AsyncMock(return_value=expected)) as call_tool:
                result = execute_mcp_tool(
                    ws_root,
                    project_dir,
                    server_name="repo",
                    tool_name="search_code",
                    arguments={"query": "supabase rag"},
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "found matches")
        call_tool.assert_awaited_once()
        await_args = call_tool.await_args.args
        self.assertEqual(await_args[0].name, "repo")
        self.assertEqual(await_args[1], "search_code")
        self.assertEqual(await_args[2], {"query": "supabase rag"})


class AgentToolsRegressionTests(unittest.TestCase):
    def test_discover_mcp_servers_parses_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            cfg_dir = ws_root / ".voiceide"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "mcp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "repo": {
                                "command": "repo-server",
                                "args": ["--fast"],
                                "tools": ["search_code"],
                            },
                            "browser": {
                                "url": "http://localhost:1234/mcp",
                                "tools": ["browser_audit"],
                            },
                            "off": {
                                "command": "nope",
                                "enabled": False,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            warnings: list[str] = []
            servers = discover_mcp_servers(ws_root, project_dir, warnings=warnings)

        names = {s.name for s in servers}
        self.assertIn("repo", names)
        self.assertIn("browser", names)
        self.assertNotIn("off", names)
        repo = next(s for s in servers if s.name == "repo")
        self.assertEqual(repo.transport, "stdio")
        browser = next(s for s in servers if s.name == "browser")
        self.assertEqual(browser.transport, "http")

    def test_discover_mcp_servers_imports_claude_and_codex_config_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "context7": {
                                "command": "npx",
                                "args": ["-y", "@upstash/context7-mcp"],
                                "tools": ["resolve-library-id"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / ".claude").mkdir()
            (project_dir / ".claude" / "settings.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "browser": {
                                "url": "http://127.0.0.1:3333/mcp",
                                "enabled": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (ws_root / ".codex").mkdir()
            (ws_root / ".codex" / "config.toml").write_text(
                '[mcp_servers.deepwiki]\nurl = "https://mcp.deepwiki.com/mcp"\n',
                encoding="utf-8",
            )

            servers = discover_mcp_servers(ws_root, project_dir)

        by_name = {server.name: server for server in servers}
        self.assertEqual(by_name["context7"].transport, "stdio")
        self.assertEqual(by_name["context7"].command, "npx")
        self.assertEqual(by_name["browser"].transport, "http")
        self.assertEqual(by_name["deepwiki"].transport, "http")

    def test_detect_project_stack_component_and_browser_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "dependencies": {
                            "@radix-ui/react-dialog": "^1.0.0",
                            "react": "^19.0.0",
                        },
                        "devDependencies": {
                            "@playwright/test": "^1.59.0",
                        },
                    }
                ),
                encoding="utf-8",
            )

            stack = detect_project_stack(project_dir)

        self.assertIn("radix-ui", stack.component_libraries)
        self.assertTrue(stack.has_playwright)
        self.assertTrue(stack.has_headless_browser)

    def test_skill_catalog_prioritizes_relevant_local_frontend_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            skills_dir = project_dir / ".codex" / "skills"
            skills_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {"build": "vite build"},
                        "dependencies": {"react": "^19.0.0", "vite": "^7.0.0"},
                        "devDependencies": {"typescript": "^5.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "src").mkdir()
            (project_dir / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
            (skills_dir / "queue-system.md").write_text(
                "# Queue System\n\nUse for durable queue consumers and delayed jobs.",
                encoding="utf-8",
            )
            (skills_dir / "frontend-dashboard.md").write_text(
                "# Frontend Dashboard\n\nUse for React Vite dashboard UI, responsive layout, components, and preview polish.",
                encoding="utf-8",
            )

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="skill_catalog",
                arguments={"project_root": "demo", "query": "dashboard task tracker profesional", "limit": 2},
            )

        self.assertTrue(result.ok, result.error)
        skills = result.raw["skills"]
        self.assertGreaterEqual(result.raw["matched_count"], 1)
        self.assertEqual(skills[0]["skill_id"], "frontend-dashboard")
        self.assertNotEqual(skills[0]["skill_id"], "queue-system")

    def test_detect_project_stack_supports_backend_and_system_languages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "polyglot"
            project_dir.mkdir(parents=True)
            (project_dir / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
            (project_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (project_dir / "go.mod").write_text("module demo\n", encoding="utf-8")
            (project_dir / "Cargo.toml").write_text("[package]\nname='core'\nversion='0.1.0'\n", encoding="utf-8")
            (project_dir / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
            (project_dir / "supabase" / "migrations").mkdir(parents=True)
            (project_dir / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")

            stack = detect_project_stack(project_dir)

        self.assertIn("python", stack.languages)
        self.assertIn("go", stack.languages)
        self.assertIn("rust", stack.languages)
        self.assertIn("java", stack.languages)
        self.assertIn("maven", stack.frameworks)
        self.assertTrue(stack.has_database_schema)
        self.assertTrue(stack.has_infra)

    def test_build_validation_plan_recommends_stack_specific_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "polyglot"
            project_dir.mkdir(parents=True)
            (project_dir / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
            (project_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (project_dir / "go.mod").write_text("module demo\n", encoding="utf-8")
            (project_dir / "Cargo.toml").write_text("[package]\nname='core'\nversion='0.1.0'\n", encoding="utf-8")
            (project_dir / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")

            plan = build_validation_plan(project_dir, project_root="polyglot")
            commands = [item["command"] for item in plan["commands"]]

        self.assertIn("cd polyglot && python3 -m pytest", commands)
        self.assertIn("cd polyglot && python3 -m compileall .", commands)
        self.assertIn("cd polyglot && go test ./...", commands)
        self.assertIn("cd polyglot && cargo test", commands)
        self.assertIn("cd polyglot && gradle test", commands)

    def test_detect_project_stack_supports_more_runtimes_and_infra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "systems"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "k8s").mkdir(parents=True)
            project_dir.mkdir(parents=True, exist_ok=True)
            (project_dir / "deno.json").write_text('{"tasks":{"test":"deno test"}}\n', encoding="utf-8")
            (project_dir / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
            (project_dir / "src" / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
            (project_dir / "Package.swift").write_text("// swift-tools-version: 5.9\n", encoding="utf-8")
            (project_dir / "mix.exs").write_text("defmodule Demo.MixProject do\nend\n", encoding="utf-8")
            (project_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (project_dir / "k8s" / "deployment.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")

            stack = detect_project_stack(project_dir)

        self.assertIn("typescript", stack.languages)
        self.assertIn("cpp", stack.languages)
        self.assertIn("swift", stack.languages)
        self.assertIn("elixir", stack.languages)
        self.assertIn("deno", stack.runtimes)
        self.assertIn("docker-compose", stack.frameworks)
        self.assertIn("kubernetes", stack.frameworks)
        self.assertTrue(stack.has_infra)

    def test_validation_plan_recommends_more_runtime_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "systems"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "k8s").mkdir(parents=True)
            project_dir.mkdir(parents=True, exist_ok=True)
            (project_dir / "deno.json").write_text('{"tasks":{"test":"deno test"}}\n', encoding="utf-8")
            (project_dir / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
            (project_dir / "build").mkdir()
            (project_dir / "Package.swift").write_text("// swift-tools-version: 5.9\n", encoding="utf-8")
            (project_dir / "mix.exs").write_text("defmodule Demo.MixProject do\nend\n", encoding="utf-8")
            (project_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (project_dir / "k8s" / "deployment.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")

            plan = build_validation_plan(project_dir, project_root="systems")
            commands = [item["command"] for item in plan["commands"]]
            optional = [item["command"] for item in plan["optional_commands"]]

        self.assertIn("cd systems && deno test", commands)
        self.assertIn("cd systems && deno check .", commands)
        self.assertIn("cd systems && cmake --build build", commands)
        self.assertIn("cd systems && swift test", commands)
        self.assertIn("cd systems && mix test", commands)
        self.assertIn("cd systems && docker compose config", optional)
        self.assertIn("cd systems && kubectl apply --dry-run=client -f k8s", optional)

    def test_guarded_autonomy_allows_polyglot_validation_commands(self) -> None:
        for command in [
            "deno test",
            "deno check .",
            "cmake --build build",
            "swift test",
            "mix test",
            "docker compose config",
            "kubectl apply --dry-run=client -f k8s",
        ]:
            with self.subTest(command=command):
                decision = main_mod._command_policy_decision(command)
                self.assertTrue(decision.ok, decision)

    def test_resolve_agent_skills_prefers_component_library_skills_when_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "dependencies": {
                            "@radix-ui/react-dialog": "^1.0.0",
                            "react": "^19.0.0",
                        },
                    }
                ),
                encoding="utf-8",
            )

            skills = resolve_agent_skills(
                ws_root,
                project_dir=project_dir,
                query="use existing components and improve dialog accessibility",
                build_mode="full-agent",
                active_rel="src/App.tsx",
                preview_url=None,
                limit=6,
            )
            skill_ids = {s.skill_id for s in skills}

        self.assertIn("component-library-awareness", skill_ids)
        self.assertIn("project-component-libraries", skill_ids)

    def test_local_tools_repo_search_and_read_are_read_only_and_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text(
                "export const x = 'supabase rag';\n",
                encoding="utf-8",
            )

            search = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="repo_search",
                arguments={"project_root": "demo", "query": "supabase"},
            )
            self.assertTrue(search.ok)
            self.assertIn("demo/src/App.tsx", search.text)

            read = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="repo_read",
                arguments={"path": "demo/src/App.tsx", "max_chars": 2000},
            )
            self.assertTrue(read.ok)
            self.assertIn("supabase rag", read.text)

            project_relative_read = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="repo_read",
                arguments={"path": "src/App.tsx", "max_chars": 2000},
            )
            self.assertTrue(project_relative_read.ok)
            self.assertEqual(project_relative_read.raw["path"], "demo/src/App.tsx")
            self.assertIn("supabase rag", project_relative_read.text)

            project_relative_window = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="file_window",
                arguments={"path": "src/App.tsx", "line": 1, "context": 1},
            )
            self.assertTrue(project_relative_window.ok)
            self.assertIn("FILE: demo/src/App.tsx", project_relative_window.text)

    def test_runtime_scopes_project_tools_when_model_omits_project_root(self) -> None:
        args = agent_runtime_mod._scoped_local_tool_arguments(
            SimpleNamespace(project_root="demo"),
            "repo_overview",
            {},
        )
        self.assertEqual(args["project_root"], "demo")

        read_args = agent_runtime_mod._scoped_local_tool_arguments(
            SimpleNamespace(project_root="demo"),
            "repo_read",
            {"path": "src/App.tsx"},
        )
        self.assertNotIn("project_root", read_args)

    def test_local_tools_skip_dependency_and_build_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "node_modules" / "pkg").mkdir(parents=True)
            (project_dir / "dist").mkdir(parents=True)
            (project_dir / ".git").mkdir(parents=True)
            (project_dir / "src" / "App.tsx").write_text("visible needle\n", encoding="utf-8")
            (project_dir / "node_modules" / "pkg" / "index.js").write_text("hidden needle\n", encoding="utf-8")
            (project_dir / "dist" / "bundle.js").write_text("hidden needle\n", encoding="utf-8")
            (project_dir / ".git" / "config").write_text("hidden needle\n", encoding="utf-8")

            search = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="repo_search",
                arguments={"project_root": "demo", "query": "needle", "max_matches": 20},
            )
            self.assertTrue(search.ok)
            self.assertIn("demo/src/App.tsx", search.text)
            self.assertNotIn("node_modules", search.text)
            self.assertNotIn("dist/bundle.js", search.text)
            self.assertNotIn(".git", search.text)

            listing = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="repo_list",
                arguments={"project_root": "demo", "max_files": 20},
            )
            self.assertTrue(listing.ok)
            self.assertIn("src/App.tsx", listing.text)
            self.assertNotIn("node_modules", listing.text)
            self.assertNotIn("dist/bundle.js", listing.text)
            self.assertNotIn(".git", listing.text)

    def test_local_tools_import_codex_and_claude_style_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            codex_skill = ws_root / ".codex" / "skills" / "api-review"
            claude_skill = project_dir / ".claude" / "skills" / "django-fix"
            codex_skill.mkdir(parents=True)
            claude_skill.mkdir(parents=True)
            project_dir.mkdir(parents=True, exist_ok=True)
            (codex_skill / "SKILL.md").write_text(
                "---\nname: api-review\ndescription: Review REST API error handling and status codes\n---\n# API Review\nCheck handlers, auth boundaries, and response contracts.\n",
                encoding="utf-8",
            )
            (claude_skill / "SKILL.md").write_text(
                "---\nname: django-fix\ndescription: Use for Django model, view, migration, and queryset bugs\n---\n# Django Fix\nInspect models, migrations, serializers, and tests before editing.\n",
                encoding="utf-8",
            )

            catalog = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="skill_catalog",
                arguments={"project_root": "demo", "query": "fix django queryset api bug"},
            )
            detail = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="skill_read",
                arguments={"project_root": "demo", "skill_id": "django-fix"},
            )

        self.assertTrue(catalog.ok)
        self.assertIn('"django-fix"', catalog.text)
        self.assertIn('"api-review"', catalog.text)
        self.assertIn('"provider": "claude"', catalog.text)
        self.assertIn('"provider": "codex"', catalog.text)
        self.assertTrue(detail.ok)
        self.assertIn("Inspect models", detail.text)
        self.assertIn('"source"', detail.text)

    def test_local_tools_provide_repo_overview_package_scripts_and_dependency_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "packageManager": "pnpm@10.0.0",
                        "scripts": {"dev": "vite", "lint": "eslint .", "build": "vite build"},
                        "dependencies": {"@vitejs/plugin-react": "^latest", "react": "^19.0.0"},
                        "devDependencies": {"typescript": "^5.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "src" / "components" / "Button.tsx").write_text(
                "export interface ButtonProps { label: string }\nexport function Button(props: ButtonProps) { return <button aria-label={props.label}>{props.label}</button> }\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text(
                "import React from 'react';\nimport { Button } from './components/Button';\nexport function App() { const loading = false; const error = null; return <main><a href=\"/dashboard\">Dashboard</a><Button label=\"Save\" /></main> }\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "app.css").write_text(
                ":root { --color-bg: #fff; }\n@media (max-width: 700px) { main { display: grid; } }\n.panel { min-width: 720px; white-space: nowrap; }\n/* TODO: remove old spacing token */\n",
                encoding="utf-8",
            )

            read_many = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="repo_read_many",
                arguments={"paths": ["demo/src/App.tsx", "demo/src/components/Button.tsx"]},
            )
            self.assertTrue(read_many.ok)
            self.assertIn("FILE: demo/src/App.tsx", read_many.text)
            self.assertIn("FILE: demo/src/components/Button.tsx", read_many.text)

            scripts = execute_local_tool(ws_root, project_dir, tool_name="package_scripts", arguments={"project_root": "demo"})
            self.assertTrue(scripts.ok)
            self.assertIn('"lint": "eslint ."', scripts.text)
            self.assertIn('"build"', scripts.text)

            overview = execute_local_tool(ws_root, project_dir, tool_name="repo_overview", arguments={"project_root": "demo"})
            self.assertTrue(overview.ok)
            self.assertIn('"package_manager": "pnpm@10.0.0"', overview.text)
            self.assertIn("src/App.tsx", overview.text)

            graph = execute_local_tool(ws_root, project_dir, tool_name="dependency_graph", arguments={"project_root": "demo"})
            self.assertTrue(graph.ok)
            self.assertIn("src/components/Button.tsx", graph.text)
            self.assertIn("react", graph.text)

            components = execute_local_tool(ws_root, project_dir, tool_name="component_index", arguments={"project_root": "demo"})
            self.assertTrue(components.ok)
            self.assertIn('"Button"', components.text)
            self.assertIn('"ButtonProps"', components.text)
            self.assertIn("src/App.tsx", components.text)

            routes = execute_local_tool(ws_root, project_dir, tool_name="route_map", arguments={"project_root": "demo"})
            self.assertTrue(routes.ok)
            self.assertIn("/dashboard", routes.text)

            quality = execute_local_tool(ws_root, project_dir, tool_name="quality_scan", arguments={"project_root": "demo"})
            self.assertTrue(quality.ok)
            self.assertIn('"responsive": true', quality.text)
            self.assertIn('"a11y_labels": true', quality.text)
            self.assertIn('"todo"', quality.text)
            self.assertIn('"mobile-overflow-width"', quality.text)
            self.assertIn('"mobile-overflow-nowrap"', quality.text)

            memory = execute_local_tool(ws_root, project_dir, tool_name="memory_overview", arguments={"project_root": "demo"})
            self.assertTrue(memory.ok)
            self.assertIn('"retrieval_backend"', memory.text)

            stack_profile = execute_local_tool(ws_root, project_dir, tool_name="stack_profile", arguments={"project_root": "demo"})
            self.assertTrue(stack_profile.ok)
            self.assertIn('"typescript"', stack_profile.text)
            self.assertIn('"vite"', stack_profile.text)

            validation = execute_local_tool(ws_root, project_dir, tool_name="validation_plan", arguments={"project_root": "demo"})
            self.assertTrue(validation.ok)
            self.assertIn("pnpm run lint", validation.text)
            self.assertIn("pnpm run build", validation.text)

            mcp_status = execute_local_tool(ws_root, project_dir, tool_name="mcp_status", arguments={"project_root": "demo"})
            self.assertTrue(mcp_status.ok)
            self.assertIn('"servers"', mcp_status.text)

            preview_caps = execute_local_tool(ws_root, project_dir, tool_name="preview_capabilities", arguments={"project_root": "demo"})
            self.assertTrue(preview_caps.ok)
            self.assertIn('"can_attempt_preview": true', preview_caps.text)
            self.assertIn('"dev"', preview_caps.text)

    def test_local_tools_provide_swe_agent_and_aider_style_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {"build": "vite build"},
                        "dependencies": {"@tailwindcss/vite": "^4.0.0", "react": "^19.0.0"},
                        "devDependencies": {"typescript": "^5.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "src" / "components" / "Panel.tsx").write_text(
                "export type PanelProps = { title: string }\n"
                "export function Panel(props: PanelProps) {\n"
                "  return <section className=\"rounded-xl border p-4\">{props.title}</section>\n"
                "}\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text(
                "import { Panel } from './components/Panel';\n"
                "export default function App() {\n"
                "  return <main className=\"grid gap-4\"><Panel title=\"Ops\" /></main>\n"
                "}\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "style.css").write_text(
                "@import \"tailwindcss\";\n:root { --app-bg: #fff; }\n.card { border-radius: 12px; }\n",
                encoding="utf-8",
            )

            repo_map = execute_local_tool(ws_root, project_dir, tool_name="repo_map", arguments={"project_root": "demo", "query": "Panel dashboard", "max_files": 20})
            file_window = execute_local_tool(ws_root, project_dir, tool_name="file_window", arguments={"path": "demo/src/App.tsx", "line": 2, "context": 1})
            symbol_search = execute_local_tool(ws_root, project_dir, tool_name="symbol_search", arguments={"project_root": "demo", "query": "Panel"})
            style_stack = execute_local_tool(ws_root, project_dir, tool_name="style_stack", arguments={"project_root": "demo"})

        self.assertTrue(repo_map.ok)
        self.assertIn("src/components/Panel.tsx", repo_map.text)
        self.assertIn("PanelProps", repo_map.text)
        self.assertIn("export function Panel", repo_map.text)
        self.assertTrue(file_window.ok)
        self.assertIn("1| import { Panel }", file_window.text)
        self.assertIn("3|   return <main", file_window.text)
        self.assertTrue(symbol_search.ok)
        self.assertIn('"Panel"', symbol_search.text)
        self.assertIn('"src/components/Panel.tsx"', symbol_search.text)
        self.assertTrue(style_stack.ok)
        self.assertIn('"tailwind": true', style_stack.text)
        self.assertIn('"utility_class_usage": true', style_stack.text)
        self.assertIn('"css_custom_properties": true', style_stack.text)

    def test_local_tools_report_first_class_shadcn_ui_stack_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src" / "components" / "ui").mkdir(parents=True)
            (project_dir / "src" / "lib").mkdir(parents=True)
            (project_dir / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {"build": "vite build"},
                        "dependencies": {
                            "@radix-ui/react-slot": "^1.2.0",
                            "@tailwindcss/vite": "^4.0.0",
                            "class-variance-authority": "^0.7.1",
                            "clsx": "^2.1.1",
                            "react": "^19.0.0",
                            "tailwind-merge": "^3.0.0",
                            "tailwindcss": "^4.0.0",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "components.json").write_text(
                json.dumps({"style": "new-york", "rsc": False, "tsx": True, "aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}, "base": "radix"}),
                encoding="utf-8",
            )
            (project_dir / "src" / "styles.css").write_text("@import \"tailwindcss\";\n@theme inline { --color-background: var(--background); }\n", encoding="utf-8")
            (project_dir / "src" / "components" / "ui" / "button.tsx").write_text("import { cva } from 'class-variance-authority';\nexport function Button(){ return null }\n", encoding="utf-8")
            (project_dir / "src" / "lib" / "utils.ts").write_text("import { twMerge } from 'tailwind-merge';\nexport function cn(...inputs: string[]) { return twMerge(inputs.join(' ')) }\n", encoding="utf-8")

            style_stack = execute_local_tool(ws_root, project_dir, tool_name="style_stack", arguments={"project_root": "demo"})
            stack_profile = execute_local_tool(ws_root, project_dir, tool_name="stack_profile", arguments={"project_root": "demo"})

        self.assertTrue(style_stack.ok)
        self.assertIn('"shadcn": true', style_stack.text)
        self.assertIn('"components_json": true', style_stack.text)
        self.assertIn('"tailwind_version": "v4"', style_stack.text)
        self.assertIn('"ui_component_files"', style_stack.text)
        self.assertIn('"cn_utility": true', style_stack.text)
        self.assertTrue(stack_profile.ok)
        self.assertIn('"ui_stack"', stack_profile.text)
        self.assertIn('"shadcn": true', stack_profile.text)
        self.assertIn('"base": "radix"', stack_profile.text)

    def test_local_tools_provide_swe_agent_and_aider_style_edit_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "math.py").write_text(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "def subtract(a, b):\n"
                "    return a - b\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text(
                "export default function App() {\n"
                "  return <main className=\"grid gap-4\">Old</main>\n"
                "}\n",
                encoding="utf-8",
            )

            line_preview = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="line_replace_preview",
                arguments={
                    "path": "demo/src/math.py",
                    "start_line": 2,
                    "end_line": 2,
                    "replacement": "    return a + b + 1\n",
                    "context": 1,
                },
            )
            replace_preview = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="search_replace_preview",
                arguments={
                    "path": "demo/src/App.tsx",
                    "search": "return <main className=\"grid   gap-4\">Old</main>",
                    "replace": "return <main className=\"grid gap-4\">Ready</main>",
                    "context": 1,
                },
            )

            unchanged = (project_dir / "src" / "math.py").read_text(encoding="utf-8")

        self.assertTrue(line_preview.ok)
        self.assertIn('"strategy": "line-range"', line_preview.text)
        self.assertIn("2|     return a + b + 1", line_preview.text)
        self.assertIn('"path": "demo/src/math.py"', line_preview.text)
        self.assertIn("return a + b\n", unchanged)
        self.assertTrue(replace_preview.ok)
        self.assertIn('"strategy": "whitespace-flexible"', replace_preview.text)
        self.assertIn("Ready</main>", replace_preview.text)
        self.assertIn('"suggested_change"', replace_preview.text)

    def test_local_tools_apply_swe_agent_and_aider_style_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "src").mkdir(parents=True)
            (project_dir / "src" / "math.py").write_text(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "def subtract(a, b):\n"
                "    return a - b\n",
                encoding="utf-8",
            )
            (project_dir / "src" / "App.tsx").write_text(
                "export default function App() {\n"
                "  return <main className=\"shell\">Old</main>\n"
                "}\n",
                encoding="utf-8",
            )

            line_apply = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="line_replace_apply",
                arguments={
                    "path": "demo/src/math.py",
                    "start_line": 2,
                    "end_line": 2,
                    "replacement": "    return a + b + 1\n",
                    "context": 1,
                },
            )
            replace_apply = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="search_replace_apply",
                arguments={
                    "path": "demo/src/App.tsx",
                    "search": "return <main className=\"shell\">Old</main>",
                    "replace": "return <main className=\"shell\">Ready</main>",
                    "context": 1,
                },
            )

            math_text = (project_dir / "src" / "math.py").read_text(encoding="utf-8")
            app_text = (project_dir / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertTrue(line_apply.ok, line_apply.error)
        self.assertIn("return a + b + 1", math_text)
        self.assertIn('"applied": true', line_apply.text)
        self.assertIn("--- demo/src/math.py", line_apply.text)
        self.assertTrue(replace_apply.ok, replace_apply.error)
        self.assertIn("Ready</main>", app_text)
        self.assertIn('"strategy": "exact"', replace_apply.text)

    def test_local_tools_format_lint_fixes_json_and_reports_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text('{"name":"demo","scripts":{"build":"vite build"}}\n', encoding="utf-8")

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="format_lint",
                arguments={"project_root": "demo", "mode": "fix", "files": ["package.json"], "tools": ["json"]},
            )
            formatted = (project_dir / "package.json").read_text(encoding="utf-8")

        self.assertTrue(result.ok, result.error)
        self.assertIn('"applied": true', result.text)
        self.assertIn('"tool": "json"', result.text)
        self.assertIn("--- package.json", result.text)
        self.assertIn('{\n  "name": "demo"', formatted)

    def test_task_depth_gate_blocks_generic_fallback_for_large_frontend_app(self) -> None:
        prompt = (
            "Buat Ops Command Center untuk tim operasi SaaS enterprise. Workspace/dashboard bukan landing. "
            "Sidebar modules Overview, Incidents, Deployments, Customers, Automation, Reports. "
            "Topbar search/env/health/action, KPIs, timeline, pipeline, SLA list, customer table, filters, "
            "incident detail panel, automation form validation/success, empty state, responsive."
        )
        changes = [
            {
                "path": "src/App.tsx",
                "new_content": (
                    "export default function App() {\n"
                    "  return <main><h1>Task Operations Dashboard</h1><button>Add task</button></main>\n"
                    "}\n"
                ),
            },
            {"path": "src/App.css", "new_content": "@media (max-width: 720px) { main { padding: 12px; } }\n"},
        ]

        issues = agent_runtime_mod._task_depth_gate_issues(prompt, changes)

        self.assertTrue(issues)
        self.assertIn("large app", issues[0].lower())
        self.assertIn("incidents", issues[0].lower())

    def test_local_tools_run_test_suite_and_capture_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            (project_dir / "tests").mkdir(parents=True)
            (project_dir / "tests" / "test_math.py").write_text(
                "import unittest\n\n"
                "class MathTest(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(1 + 1, 2)\n\n",
                encoding="utf-8",
            )

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="test_runner",
                arguments={
                    "project_root": "demo",
                    "commands": ["python3 -m unittest discover -s tests"],
                    "timeout_seconds": 20,
                },
            )

        self.assertTrue(result.ok)
        self.assertIn('"ran": 1', result.text)
        self.assertIn('"failed": 0', result.text)
        self.assertIn("python3 -m unittest discover -s tests", result.text)
        self.assertIn("OK", result.text)

    def test_local_tools_git_manager_reports_status_diff_and_commit_plan_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True, text=True)
            (project_dir / "app.py").write_text("print('old')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=project_dir, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            (project_dir / "app.py").write_text("print('new')\n", encoding="utf-8")

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="git_manager",
                arguments={"project_root": "demo", "mode": "commit_plan", "message": "Update app output"},
            )
            commit_count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        self.assertTrue(result.ok)
        self.assertIn('"dirty": true', result.text)
        self.assertIn("app.py", result.text)
        self.assertIn("print('new')", result.text)
        self.assertIn("git add app.py", result.text)
        self.assertIn("git commit -m", result.text)
        self.assertEqual(commit_count, "1")

    def test_local_tools_database_client_runs_sqlite_migration_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)

            migrate = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="database_client",
                arguments={
                    "project_root": "demo",
                    "backend": "sqlite",
                    "database": "app.db",
                    "mode": "migrate",
                    "sql": "CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT); INSERT INTO tasks(title) VALUES ('Ship agent');",
                },
            )
            query = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="database_client",
                arguments={
                    "project_root": "demo",
                    "backend": "sqlite",
                    "database": "app.db",
                    "mode": "query",
                    "sql": "SELECT title FROM tasks ORDER BY id",
                },
            )

        self.assertTrue(migrate.ok, migrate.error)
        self.assertIn('"writes_performed": true', migrate.text)
        self.assertTrue(query.ok, query.error)
        self.assertIn("Ship agent", query.text)
        self.assertIn('"columns": [', query.text)

    def test_database_client_blocks_write_sql_in_query_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="database_client",
                arguments={
                    "project_root": "demo",
                    "backend": "sqlite",
                    "database": "app.db",
                    "mode": "query",
                    "sql": "DROP TABLE users",
                },
            )

        self.assertFalse(result.ok)
        self.assertIn("read-only", result.error)

    def test_local_tools_git_manager_can_commit_locally_and_blocks_remote_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project_dir, check=True, capture_output=True, text=True)
            (project_dir / "app.py").write_text("print('old')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=project_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, check=True, capture_output=True, text=True)
            (project_dir / "app.py").write_text("print('new')\n", encoding="utf-8")

            commit_result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="git_manager",
                arguments={"project_root": "demo", "mode": "commit", "message": "Update app output", "paths": ["app.py"]},
            )
            push_result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="git_manager",
                arguments={"project_root": "demo", "mode": "push"},
            )
            commit_count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            status = subprocess.run(["git", "status", "--short"], cwd=project_dir, check=True, capture_output=True, text=True).stdout

        self.assertTrue(commit_result.ok, commit_result.error)
        self.assertIn('"writes_performed": true', commit_result.text)
        self.assertIn("Update app output", commit_result.text)
        self.assertEqual(commit_count, "2")
        self.assertEqual(status.strip(), "")
        self.assertFalse(push_result.ok)
        self.assertIn("allow_remote", push_result.error or push_result.text)

    def test_git_manager_pr_plan_requires_remote_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True, text=True)

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="git_manager",
                arguments={"project_root": "demo", "mode": "pr_plan"},
            )

        self.assertFalse(result.ok)
        self.assertIn("allow_remote", result.error)

    def test_local_tools_docs_browser_fetches_public_https_docs_with_bounded_text(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"<html><head><title>API Docs</title></head><body><main><h1>Install SDK</h1><p>Use the latest client.</p><script>noise()</script></main></body></html>"

            def geturl(self):
                return "https://docs.example.com/api"

        with tempfile.TemporaryDirectory() as tmp, patch("api.agent_tools.urlopen", return_value=FakeResponse()):
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)

            result = execute_local_tool(
                ws_root,
                project_dir,
                tool_name="docs_browser",
                arguments={"url": "https://docs.example.com/api", "max_chars": 2000},
            )

        self.assertTrue(result.ok)
        self.assertIn('"title": "API Docs"', result.text)
        self.assertIn("Install SDK", result.text)
        self.assertIn("Use the latest client", result.text)
        self.assertNotIn("noise()", result.text)


class HybridSeedRegressionTests(unittest.TestCase):
    def test_saas_brief_defaults_to_app_workspace_not_dashboard(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Acme Flow",
            instruction="Build a modern SaaS product with onboarding, workspace, and integrations.",
        )

        app_tsx = files["demo/src/App.tsx"]
        self.assertIn('path: "/workspace"', app_tsx)
        self.assertIn('path: "/integrations"', app_tsx)
        self.assertNotIn('path: "/dashboard"', app_tsx)
        self.assertIn("demo/src/pages/Workspace.tsx", files)
        self.assertIn("demo/src/pages/Integrations.tsx", files)
        self.assertIn("demo/src/pages/AppSettings.tsx", files)
        self.assertNotIn("demo/src/pages/Features.tsx", files)
        self.assertNotIn("demo/src/pages/Pricing.tsx", files)
        self.assertNotIn("demo/src/pages/Contact.tsx", files)

    def test_explicit_marketing_brief_keeps_landing_sections(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Launch Kit",
            instruction="Create a landing page with testimonials, FAQ, pricing, and contact form.",
        )

        self.assertIn('path: "/contact"', files["demo/src/App.tsx"])
        self.assertIn("Requested section", files["demo/src/pages/Home.tsx"])
        self.assertIn("Testimonials", files["demo/src/pages/Home.tsx"])
        self.assertIn("FAQ", files["demo/src/pages/Home.tsx"])
        self.assertIn("demo/src/pages/Contact.tsx", files)
        self.assertNotIn("demo/src/pages/Workspace.tsx", files)
        self.assertNotIn("demo/src/pages/Integrations.tsx", files)
        self.assertNotIn("demo/src/pages/AppSettings.tsx", files)

    def test_docs_brief_prefers_docs_route_over_app_workspace(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Handbook",
            instruction="Create product documentation with guides, reference docs, and changelog style navigation.",
        )

        app_tsx = files["demo/src/App.tsx"]
        self.assertIn('path: "/docs"', app_tsx)
        self.assertNotIn('path: "/workspace"', app_tsx)
        self.assertIn("demo/src/pages/Docs.tsx", files)
        self.assertNotIn("demo/src/pages/Workspace.tsx", files)
        self.assertNotIn("demo/src/pages/Integrations.tsx", files)
        self.assertNotIn("demo/src/pages/AppSettings.tsx", files)

    def test_dashboard_brief_prefers_dashboard_route_over_landing_or_app_noise(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Ops Hub",
            instruction="Build an admin dashboard for operations, analytics, billing, and inventory monitoring.",
        )

        app_tsx = files["demo/src/App.tsx"]
        self.assertIn('path: "/", element: <DashboardPage />', app_tsx)
        self.assertIn('path: "/dashboard"', app_tsx)
        self.assertNotIn('import HomePage from "./pages/Home";', app_tsx)
        self.assertNotIn('path: "/workspace"', app_tsx)
        self.assertNotIn('path: "/contact"', app_tsx)
        self.assertIn("demo/src/pages/Dashboard.tsx", files)
        self.assertNotIn("demo/src/pages/Workspace.tsx", files)
        self.assertNotIn("demo/src/pages/Integrations.tsx", files)
        self.assertNotIn("demo/src/pages/AppSettings.tsx", files)

    def test_hybrid_seed_nav_accepts_tuple_or_object_items(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Ops Hub",
            instruction="Build a dashboard for product operations.",
        )

        app_tsx = files["demo/src/App.tsx"]
        shell_tsx = files["demo/src/components/AppShell.tsx"]
        self.assertIn('type NavItem = [string, string] | { path: string; label: string } | { href: string; label: string };', app_tsx)
        self.assertIn("const NAV_ITEMS: NavItem[]", app_tsx)
        self.assertNotIn("Array<[string, string]>", app_tsx)
        self.assertNotIn("as any", app_tsx)
        self.assertIn("navItems: NavItem[];", shell_tsx)
        self.assertIn("function navItemParts(item: NavItem)", shell_tsx)
        self.assertNotIn("navItems.map(([href, label])", shell_tsx)

    def test_hybrid_seed_ui_wrappers_accept_common_dom_props(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Ops Hub",
            instruction="Build a dashboard for product operations.",
        )

        card_tsx = files["demo/src/components/ui/Card.tsx"]
        button_tsx = files["demo/src/components/ui/Button.tsx"]
        self.assertIn('ComponentPropsWithoutRef<"section">', card_tsx)
        self.assertIn('const { title, eyebrow, children, className = "", ...sectionProps } = props;', card_tsx)
        self.assertIn("<section {...sectionProps} className={classes}>", card_tsx)
        self.assertIn("ButtonHTMLAttributes<HTMLButtonElement>", button_tsx)
        self.assertIn("...buttonProps", button_tsx)

    def test_starter_residue_does_not_flag_input_placeholder_attribute(self) -> None:
        text = '<input className="input" placeholder="Search tasks..." aria-label="Search tasks" />'
        self.assertEqual(main_mod._starter_residue_terms(text), [])
        self.assertIn("placeholder content", main_mod._starter_residue_terms("Remove placeholder content before launch."))


class HostedProfileIdRegressionTests(unittest.TestCase):
    def _request(self, *, method: str = "POST", path: str = "/api/agent/worker/run", headers: dict[str, str] | None = None) -> Request:
        return Request({
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(key.lower().encode("latin1"), value.encode("latin1")) for key, value in (headers or {}).items()],
        })

    def test_background_agent_job_can_be_resumed_by_worker(self) -> None:
        seen: list[tuple[str | None, str, str | None]] = []

        def fake_run_agent_impl(req, event_cb=None, job_id=None):
            seen.append((CURRENT_PROFILE_ID.get(), req.input, req.active_file))
            if event_cb:
                event_cb("done", {"result": {"ok": True, "changes": [], "actions": []}})
            return {"ok": True, "job_id": job_id}

        with patch("api.main._run_agent_impl", side_effect=fake_run_agent_impl), \
            patch("api.main.has_supabase", return_value=False):
            session_token = CURRENT_SESSION_ID.set("worker-test")
            user_token = CURRENT_USER_ID.set("user-1")
            profile_token = CURRENT_PROFILE_ID.set("user-1")
            try:
                queued = main_mod.agent(
                    main_mod.AgentReq(
                        input="fix header",
                        project_root="demo",
                        build_mode="hybrid",
                        active_file="src/App.tsx",
                        background=True,
                    )
                )
                job_id = queued["job_id"]
                run = main_mod._run_agent_worker_jobs(job_id=job_id, limit=1)
            finally:
                CURRENT_PROFILE_ID.reset(profile_token)
                CURRENT_USER_ID.reset(user_token)
                CURRENT_SESSION_ID.reset(session_token)

        self.assertTrue(queued["ok"])
        self.assertEqual(run["processed"], 1)
        self.assertEqual(seen, [("user-1", "fix header", "src/App.tsx")])

    def test_worker_endpoint_requires_secret_in_serverless(self) -> None:
        with patch("api.main._is_serverless_runtime", return_value=True), \
            patch.dict("os.environ", {"AGENT_WORKER_SECRET": "secret"}, clear=False):
            with self.assertRaises(HTTPException) as raised:
                main_mod._require_worker_auth(self._request())

        self.assertEqual(raised.exception.status_code, 401)

    def test_worker_get_endpoint_accepts_secret_for_cron(self) -> None:
        with patch("api.main._is_serverless_runtime", return_value=True), \
            patch("api.main.list_agent_jobs_by_status", return_value=[]), \
            patch("api.main.has_supabase", return_value=True), \
            patch.dict("os.environ", {"AGENT_WORKER_SECRET": "secret"}, clear=False):
            resp = main_mod.agent_worker_run_get(
                self._request(method="GET", headers={"Authorization": "Bearer secret"}),
                limit=1,
            )

        self.assertEqual(resp["processed"], 0)

    def test_streaming_agent_keeps_profile_context_in_worker_thread(self) -> None:
        seen_profile_ids: list[str | None] = []
        errors: list[BaseException] = []

        def fake_run_agent_impl(req, event_cb=None, job_id=None):
            seen_profile_ids.append(CURRENT_PROFILE_ID.get())
            if event_cb:
                event_cb("done", {"result": {"ok": True, "reply": "hi", "actions": [], "changes": [], "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "warnings": []}}})
            return {"ok": True}

        with patch("api.main.resolve_request_user", return_value=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123")), \
            patch("api.main._run_agent_impl", side_effect=fake_run_agent_impl), \
            patch("api.main.has_supabase", return_value=False):
            def worker() -> None:
                session_token = CURRENT_SESSION_ID.set("sess-1")
                user_token = CURRENT_USER_ID.set("sb-user-123")
                profile_token = CURRENT_PROFILE_ID.set("sb-user-123")
                try:
                    main_mod._run_agent_impl(main_mod.AgentReq(input="hello", stream=False), event_cb=lambda *_args: None)
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)
                finally:
                    CURRENT_PROFILE_ID.reset(profile_token)
                    CURRENT_USER_ID.reset(user_token)
                    CURRENT_SESSION_ID.reset(session_token)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(seen_profile_ids, ["sb-user-123"])

    def test_upsert_profile_migrates_legacy_uuid_profile_to_internal_id(self) -> None:
        class FakeResponse:
            def __init__(self, data):
                self.data = data

        class FakeQuery:
            def __init__(self, client, op, payload=None):
                self.client = client
                self.op = op
                self.payload = payload or {}
                self.filters: dict[str, str] = {}
                self._limit = None

            def select(self, _fields: str):
                self.op = "select"
                return self

            def eq(self, key: str, value: str):
                self.filters[key] = value
                return self

            def limit(self, value: int):
                self._limit = value
                return self

            def update(self, payload):
                self.op = "update"
                self.payload = payload
                return self

            def upsert(self, payload):
                self.op = "upsert"
                self.payload = payload
                return self

            def execute(self):
                if self.op == "select":
                    rows = [
                        row for row in self.client.rows.values()
                        if all(str(row.get(k)) == str(v) for k, v in self.filters.items())
                    ]
                    if self._limit is not None:
                        rows = rows[: self._limit]
                    return FakeResponse(rows)
                if self.op == "update":
                    target_id = self.filters.get("id")
                    row = dict(self.client.rows.get(target_id, {}))
                    row.update(self.payload)
                    self.client.rows[target_id] = row
                    return FakeResponse([row])
                if self.op == "upsert":
                    row = dict(self.payload)
                    self.client.rows[str(row["id"])] = row
                    return FakeResponse([row])
                raise AssertionError(f"Unexpected op: {self.op}")

        class FakeClient:
            def __init__(self):
                self.rows = {
                    "00000000-0000-0000-0000-000000000123": {
                        "id": "00000000-0000-0000-0000-000000000123",
                        "supabase_user_id": "00000000-0000-0000-0000-000000000123",
                        "display_name": "Legacy User",
                        "email": "legacy@example.com",
                    }
                }

            def table(self, _name: str):
                return FakeQuery(self, "select")

        fake_client = FakeClient()
        with patch("api.storage.supabase.get_supabase_admin", return_value=fake_client):
            row = upsert_profile(
                user_id="sb-user-123",
                supabase_user_id="00000000-0000-0000-0000-000000000123",
                display_name=None,
                email=None,
            )

        self.assertEqual(row["id"], "sb-user-123")
        self.assertEqual(fake_client.rows["sb-user-123"]["supabase_user_id"], "00000000-0000-0000-0000-000000000123")
        self.assertIsNone(fake_client.rows["00000000-0000-0000-0000-000000000123"]["supabase_user_id"])
        self.assertEqual(fake_client.rows["sb-user-123"]["display_name"], "Legacy User")

    def test_get_provider_secret_reads_and_migrates_legacy_uuid_secret(self) -> None:
        class FakeResponse:
            def __init__(self, data):
                self.data = data

        class FakeSecretQuery:
            def __init__(self, client, op="select"):
                self.client = client
                self.op = op
                self.payload = None
                self.filters: dict[str, str] = {}
                self._limit = None

            def select(self, _fields: str):
                self.op = "select"
                return self

            def eq(self, key: str, value: str):
                self.filters[key] = value
                return self

            def limit(self, value: int):
                self._limit = value
                return self

            def upsert(self, payload):
                self.op = "upsert"
                self.payload = payload
                return self

            def delete(self):
                self.op = "delete"
                return self

            def execute(self):
                if self.op == "select":
                    rows = [
                        row for row in self.client.rows
                        if all(str(row.get(k)) == str(v) for k, v in self.filters.items())
                    ]
                    if self._limit is not None:
                        rows = rows[: self._limit]
                    return FakeResponse(rows)
                if self.op == "upsert":
                    payload = dict(self.payload or {})
                    self.client.rows = [
                        row for row in self.client.rows
                        if not (
                            str(row.get("profile_id")) == str(payload.get("profile_id"))
                            and str(row.get("provider")) == str(payload.get("provider"))
                        )
                    ]
                    self.client.rows.append(payload)
                    return FakeResponse([payload])
                if self.op == "delete":
                    self.client.rows = [
                        row for row in self.client.rows
                        if not all(str(row.get(k)) == str(v) for k, v in self.filters.items())
                    ]
                    return FakeResponse([])
                raise AssertionError(f"Unexpected op: {self.op}")

        class FakeSecretClient:
            def __init__(self):
                self.rows = [
                    {
                        "profile_id": "93fba5d6-7247-472b-a028-2ff2af197815",
                        "provider": "openai",
                        "secret_ciphertext": "cipher-demo",
                    }
                ]

            def table(self, _name: str):
                return FakeSecretQuery(self)

        fake_client = FakeSecretClient()
        with patch("api.storage.secrets._require_supabase", return_value=fake_client), \
            patch("api.storage.secrets._decrypt", side_effect=lambda value: "sk-demo" if value == "cipher-demo" else None):
            secret = get_provider_secret(profile_id="sb-93fba5d6-7247-472b-a028-2ff2af197815", provider="openai")
            has_secret = has_provider_secret(profile_id="sb-93fba5d6-7247-472b-a028-2ff2af197815", provider="openai")

        self.assertEqual(secret, "sk-demo")
        self.assertTrue(has_secret)
        self.assertTrue(any(row.get("profile_id") == "sb-93fba5d6-7247-472b-a028-2ff2af197815" for row in fake_client.rows))

    def test_hosted_settings_save_uses_internal_profile_id_for_secrets_and_preferences(self) -> None:
        router = build_settings_router(session_state=lambda: {"workspace": None}, env_set=lambda *_args, **_kwargs: None, env_unset=lambda *_args, **_kwargs: None, reload_settings=lambda: None)
        update_endpoint = next(route.endpoint for route in router.routes if getattr(route, "path", "") == "/api/settings" and "PUT" in getattr(route, "methods", set()))

        saved_secret_updates: list[tuple[str, str]] = []
        saved_pref_profile_ids: list[str] = []

        with patch.dict("os.environ", {"VOICEIDE_SECRET_KEY": "secret-ready"}, clear=False), \
            patch("api.config.router.resolve_request_user", return_value=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123")), \
            patch("api.config.router.has_supabase", return_value=True), \
            patch("api.config.router.upsert_provider_secret", side_effect=lambda profile_id, provider, api_key: saved_secret_updates.append((profile_id, provider))), \
            patch("api.config.router.upsert_user_preferences", side_effect=lambda profile_id, req: saved_pref_profile_ids.append(profile_id)):
            resp = update_endpoint(SettingsUpdateReq(llm_provider="openai", nine_router_api_key="sk-demo"))

        self.assertTrue(resp["ok"])
        self.assertEqual(saved_secret_updates, [("sb-user-123", "nine_router")])
        self.assertEqual(saved_pref_profile_ids, ["sb-user-123"])

    def test_hosted_preferences_router_uses_internal_profile_id(self) -> None:
        router = build_preferences_router()
        get_endpoint = next(route.endpoint for route in router.routes if getattr(route, "path", "") == "/api/preferences/user" and "GET" in getattr(route, "methods", set()))

        seen_profile_ids: list[str] = []

        with patch("api.preferences.router.get_user_preferences", side_effect=lambda profile_id: seen_profile_ids.append(profile_id) or UserPreferencesRecord(profile_id=profile_id)):
            resp = get_endpoint(user=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123"))

        self.assertEqual(resp.preferences.profile_id, "sb-user-123")
        self.assertEqual(seen_profile_ids, ["sb-user-123"])


class AgentAutoExecuteRegressionTests(unittest.TestCase):
    def test_auto_execute_reuses_successful_shell_validation_command(self) -> None:
        session_id = "auto-execute-reuse-shell-validation-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                shell_result = {
                    "ok": True,
                    "project_root": "demo",
                    "ran": 1,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": "", "returncode": 0}],
                }
                calls: list[list[str]] = []

                def fake_run_shell(*, actions, **_kwargs):
                    calls.append([action.command for action in actions])
                    return shell_result

                with patch("api.main._infer_validation_commands", return_value=["npm run build"]), \
                    patch("api.main._run_harness_shell_actions_internal", side_effect=fake_run_shell), \
                    patch("api.main._auto_execute_preview_audit", return_value=None):
                    execution = main_mod._auto_execute_agent_result(
                        main_mod.AgentReq(input="build it", project_root="demo", auto_execute=True),
                        [{"path": "demo/src/App.tsx", "new_content": "export default function App() { return <main>Ready</main>; }\n"}],
                        [{"type": "shell", "command": "npm run build", "reason": "validate"}],
                        lambda *_args: None,
                    )

                self.assertTrue(execution["ok"])
                self.assertEqual(calls, [["npm run build"]])
                self.assertEqual(execution["validation"]["reused"], 1)
                self.assertEqual(execution["validation"]["ran"], 0)
                self.assertTrue(execution["validation"]["ok"])
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_repair_success_does_not_clear_preview_failure_without_preview_rerun(self) -> None:
        parent_execution = {
            "ok": False,
            "preview_audit": {
                "ok": False,
                "skipped": False,
                "issue_details": [
                    {"severity": "blocking", "category": "responsive-overflow", "detail": "mobile overflow"},
                ],
            },
        }
        build_only_repair = {
            "ok": True,
            "validation": {"ok": True, "ran": 1, "failed": 0},
            "preview_audit": None,
        }
        preview_skipped_repair = {
            "ok": True,
            "validation": {"ok": True, "ran": 1, "failed": 0},
            "preview_audit": {"ok": True, "skipped": True, "summary": "Preview audit skipped."},
        }
        preview_clean_repair = {
            "ok": True,
            "validation": {"ok": True, "ran": 1, "failed": 0},
            "preview_audit": {"ok": True, "skipped": False, "issue_details": []},
        }
        preview_warning_repair = {
            "ok": True,
            "validation": {"ok": True, "ran": 1, "failed": 0},
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "issue_details": [
                    {"severity": "warning", "category": "product-depth", "detail": "Product depth masih tipis."},
                ],
            },
        }

        self.assertFalse(main_mod._repair_resolves_parent_execution(parent_execution, build_only_repair))
        self.assertFalse(main_mod._repair_resolves_parent_execution(parent_execution, preview_skipped_repair))
        self.assertFalse(main_mod._repair_resolves_parent_execution(parent_execution, preview_warning_repair))
        self.assertTrue(main_mod._repair_resolves_parent_execution(parent_execution, preview_clean_repair))

    def test_preview_polish_warnings_keep_execution_in_repair_lane(self) -> None:
        execution = {
            "ok": True,
            "apply": {"ok": True, "applied": True, "count": 1},
            "shell": {"ok": True, "ran": 1, "results": [{"ok": True}]},
            "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "audit_mode": "browser",
                "issue_details": [
                    {"severity": "warning", "category": "metadata", "detail": "Preview page is missing a meta description."},
                    {"severity": "warning", "category": "mobile-tap-targets", "detail": "Target tap terlalu kecil."},
                ],
            },
        }

        self.assertTrue(main_mod._execution_needs_repair(execution))
        report = main_mod._execution_completion_report(execution)

        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "polish-needed")
        criteria = {item["label"]: item for item in report["criteria"]}
        self.assertEqual(criteria["preview-polish"]["status"], "failed")
        self.assertIn("Preview polish", " ".join(report["residual_risks"]))

    def test_state_readiness_warnings_are_reported_separately_from_polish_debt(self) -> None:
        execution = {
            "ok": True,
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "issue_details": [
                    {"severity": "warning", "category": "state-loading", "detail": "No loading branch."},
                    {"severity": "warning", "category": "state-error", "detail": "No error branch."},
                    {"severity": "warning", "category": "metadata", "detail": "Missing description."},
                ],
            },
        }

        state_debt = main_mod._preview_state_readiness_debt(execution)
        polish_debt = main_mod._preview_polish_debt(execution)

        self.assertEqual({item["category"] for item in state_debt}, {"state-loading", "state-error"})
        self.assertEqual([item["category"] for item in polish_debt], ["metadata"])

    def test_preview_polish_warnings_after_repair_attempt_do_not_block_green_validation(self) -> None:
        execution = {
            "ok": True,
            "apply": {"ok": True, "applied": True, "count": 1},
            "shell": {"ok": True, "ran": 1, "results": [{"ok": True}]},
            "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "audit_mode": "browser",
                "issue_details": [
                    {"severity": "warning", "category": "source-quality", "detail": "Inline style masih bisa dipoles."},
                    {"severity": "warning", "category": "visual-polish", "detail": "Hierarchy visual masih generic."},
                ],
            },
            "repairs": [
                {
                    "execution": {
                        "ok": False,
                        "preview_audit": {
                            "ok": True,
                            "skipped": False,
                            "issue_details": [
                                {"severity": "warning", "category": "source-quality", "detail": "Inline style masih bisa dipoles."},
                            ],
                        },
                    },
                }
            ],
        }

        report = main_mod._execution_completion_report(execution)

        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], "complete-with-warnings")
        criteria = {item["label"]: item for item in report["criteria"]}
        self.assertEqual(criteria["preview-polish"]["status"], "warning")
        self.assertEqual(criteria["repair-loop"]["status"], "warning")
        self.assertIn("Complete with warnings", report["summary"])

    def test_completion_report_blocks_when_hard_criteria_failed_even_if_execution_ok(self) -> None:
        execution = {
            "ok": True,
            "apply": {"ok": True, "applied": True, "count": 4},
            "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
            "preview_audit": {
                "ok": False,
                "skipped": False,
                "audit_mode": "browser",
                "issue_details": [
                    {"severity": "blocking", "category": "blank", "detail": "Preview rendered blank."},
                ],
            },
            "repairs": [
                {
                    "execution": {
                        "ok": False,
                        "validation": {"ok": False, "ran": 1, "failed": 1, "commands": ["npm run build"]},
                    },
                }
            ],
        }

        report = main_mod._execution_completion_report(execution)

        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "blocked")
        criteria = {item["label"]: item for item in report["criteria"]}
        self.assertEqual(criteria["preview"]["status"], "failed")
        self.assertEqual(criteria["repair-loop"]["status"], "failed")
        self.assertIn("Blocked:", report["summary"])

    def test_missing_project_bootstrap_creates_editable_react_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            result = main_mod._bootstrap_missing_agent_project(root, "fresh-porto")

            self.assertIsInstance(result, dict)
            project = root / "fresh-porto"
            self.assertTrue((project / "package.json").exists())
            self.assertTrue((project / "index.html").exists())
            self.assertTrue((project / "src" / "App.tsx").exists())
            package_data = json.loads((project / "package.json").read_text(encoding="utf-8"))
            self.assertTrue(package_data["apporaTemplate"])
            self.assertIn("build", package_data["scripts"])

    def test_verifier_repair_invalid_json_uses_executable_recovery_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project = ws_root / "demo"
            (project / "src").mkdir(parents=True)
            (project / "package.json").write_text(
                '{"scripts":{"build":"tsc -b && vite build"},"dependencies":{"vite":"latest","react":"latest","react-dom":"latest"}}\n',
                encoding="utf-8",
            )
            (project / "index.html").write_text('<div id="root"></div><script type="module" src="/src/main.tsx"></script>\n', encoding="utf-8")
            (project / "src" / "main.tsx").write_text("import './styles.css'; import App from './App';\n", encoding="utf-8")
            (project / "src" / "App.tsx").write_text("export default function App(){ return <main /> }\n", encoding="utf-8")
            (project / "src" / "styles.css").write_text("body{margin:0}\n", encoding="utf-8")
            req = main_mod.AgentReq(
                input="Bikin website portfolio profesional bernama Arka Pratama",
                project_root="demo",
                build_mode="full-agent",
                auto_execute=True,
            )
            trace = {
                "verification": [
                    {"name": "frontend-style-runtime", "ok": False, "detail": "Tailwind utilities without Tailwind setup."},
                ],
                "task_state": {"status": "blocked", "blocking_checks": ["frontend-style-runtime"]},
            }

            with patch("api.agent.suggest", side_effect=RuntimeError("LLM did not return valid JSON: {")):
                repair = main_mod._run_backend_verifier_repair_pass(
                    req,
                    ws_root,
                    trace,
                    lambda *_args: None,
                    base_changes=[],
                    base_actions=[],
                )

        self.assertTrue(repair["json_recovery"])
        self.assertTrue(repair["changes"])
        self.assertTrue(any(item.get("path") == "demo/src/App.tsx" for item in repair["changes"]))
        self.assertTrue(any(item.get("type") == "shell" and "npm run build" in item.get("command", "") for item in repair["actions"]))

    def test_auto_execute_runs_llm_repair_for_visual_preview_polish_debt(self) -> None:
        session_id = "warning-only-preview-polish-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "index.html").write_text("<div id=\"root\"></div>\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                preview_warning = {
                    "ok": True,
                    "skipped": False,
                    "audit_mode": "browser",
                    "issue_details": [
                        {"severity": "warning", "category": "visual-polish", "detail": "Hierarchy can be improved."},
                    ],
                    "summary": "Preview audit mode=browser, blocking=0, warnings=1.",
                }
                repaired_preview = {"ok": True, "skipped": False, "audit_mode": "browser", "issue_details": []}
                repair_result = {
                    "changes": [{"path": "demo/src/App.tsx", "new_content": "export default function App() { return <main>Polished</main>; }\n"}],
                    "actions": [],
                    "execution": {
                        "ok": True,
                        "apply": {"ok": True, "applied": True, "count": 1},
                        "validation": {"ok": True, "ran": 0, "failed": 0},
                        "preview_audit": repaired_preview,
                        "failure_analysis": {"summary": "clean"},
                    },
                    "pre_repair_failure_analysis": {"repeated_failure": False},
                }

                with patch("api.main._infer_validation_commands", return_value=[]), \
                    patch("api.main._auto_execute_preview_audit", return_value=preview_warning), \
                    patch("api.main._run_backend_repair_pass", return_value=repair_result) as repair_mock:
                    execution = main_mod._auto_execute_agent_result(
                        main_mod.AgentReq(input="polish", project_root="demo", auto_execute=True),
                        [{"path": "src/App.tsx", "new_content": "export default function App() { return <main>Ready</main>; }\n"}],
                        [],
                        lambda *_args: None,
                    )

                self.assertTrue(execution["ok"])
                self.assertFalse(main_mod._execution_has_primary_failure(execution))
                self.assertFalse(main_mod._preview_polish_debt(execution))
                repair_mock.assert_called_once()
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_deterministic_interaction_gate_disables_remaining_inert_buttons(self) -> None:
        changes, paths = main_mod._gate_inert_final_buttons_in_changes([
            {
                "path": "src/pages/Home.tsx",
                "new_content": (
                    "export default function Home() {\n"
                    "  return <><button className=\"btn\">Booking</button>"
                    "<button type=\"submit\">Kirim</button>"
                    "<Button onClick={() => setOpen(true)}>Buka</Button></>;\n"
                    "}\n"
                ),
            }
        ])

        self.assertEqual(paths, ["src/pages/Home.tsx"])
        content = str(changes[0]["new_content"])
        self.assertIn('className="btn" type="button" disabled aria-disabled="true"', content)
        self.assertIn('<button type="submit">Kirim</button>', content)
        self.assertIn('<Button onClick={() => setOpen(true)}>Buka</Button>', content)

    def test_deterministic_business_data_gate_neutralizes_fake_contact_claims(self) -> None:
        changes, paths = main_mod._gate_fake_business_data_in_changes([
            {
                "path": "src/App.tsx",
                "new_content": (
                    "export default function App() { return <main>"
                    "<a href=\"https://wa.me/6281234567890\">Booking via WhatsApp</a>"
                    "<p>Sejak 2018 dipercaya 1000+ pelanggan dengan 4.9 rating.</p>"
                    "<p>Alamat: Jl. Contoh No. 123</p>"
                    "<p>Jam operasional buka 08:00-21:00</p>"
                    "</main>; }\n"
                ),
            }
        ])

        self.assertEqual(paths, ["src/App.tsx"])
        content = str(changes[0]["new_content"])
        self.assertIn('aria-disabled="true"', content)
        self.assertIn("kontak-belum-dikonfigurasi", content)
        self.assertIn("siap dikonfigurasi", content)
        self.assertIn("banyak pelanggan", content)
        self.assertIn("ulasan pelanggan", content)
        self.assertIn("Alamat belum dikonfigurasi", content)
        self.assertIn("Jam operasional belum dikonfigurasi", content)
        self.assertNotIn("6281234567890", content)
        self.assertNotIn("2018", content)
        self.assertNotIn("4.9 rating", content)

    def test_successful_clean_repair_supersedes_parent_preview_polish_warnings(self) -> None:
        execution = {
            "ok": True,
            "apply": {"ok": True, "applied": True, "count": 1},
            "shell": {"ok": True, "ran": 1, "results": [{"ok": True}]},
            "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "audit_mode": "browser",
                "issue_details": [
                    {"severity": "warning", "category": "metadata", "detail": "Missing description."},
                    {"severity": "warning", "category": "product-depth", "detail": "Surface thin."},
                ],
            },
            "repairs": [
                {
                    "execution": {
                        "ok": True,
                        "apply": {"ok": True, "applied": True, "count": 1},
                        "shell": {"ok": True, "ran": 1, "results": [{"ok": True}]},
                        "validation": {"ok": True, "ran": 1, "failed": 0, "commands": ["npm run build"]},
                        "preview_audit": {
                            "ok": True,
                            "skipped": False,
                            "audit_mode": "browser",
                            "issue_details": [],
                        },
                    },
                }
            ],
        }

        report = main_mod._execution_completion_report(execution)

        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], "complete")
        criteria = {item["label"]: item for item in report["criteria"]}
        self.assertEqual(criteria["preview-polish"]["status"], "superseded")
        self.assertEqual(criteria["repair-loop"]["status"], "passed")
        self.assertIn("Complete:", report["summary"])

    def test_auto_execute_uses_remaining_repair_budget_for_preview_polish_debt(self) -> None:
        session_id = "preview-polish-repair-budget-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                preview_with_debt = {
                    "ok": True,
                    "skipped": False,
                    "issue_details": [
                        {"severity": "warning", "category": "product-depth", "detail": "Surface thin."},
                    ],
                }
                first_repair = {
                    "changes": [{"path": "demo/index.html", "new_content": "<html><body><section>Still thin</section></body></html>"}],
                    "actions": [],
                    "execution": {
                        "ok": False,
                        "preview_audit": preview_with_debt,
                        "failure_analysis": {"summary": "still thin"},
                    },
                    "pre_repair_failure_analysis": {"repeated_failure": False},
                }
                second_repair = {
                    "changes": [{"path": "demo/index.html", "new_content": "<html><body><section>Clean</section></body></html>"}],
                    "actions": [],
                    "execution": {
                        "ok": True,
                        "apply": {"ok": True, "applied": True, "count": 1},
                        "validation": {"ok": True, "ran": 0, "failed": 0},
                        "preview_audit": {"ok": True, "skipped": False, "issue_details": []},
                        "failure_analysis": {"summary": "clean"},
                    },
                    "pre_repair_failure_analysis": {"repeated_failure": False},
                }

                with (
                    patch("api.main._auto_execute_preview_audit", return_value=preview_with_debt),
                    patch("api.main._try_quick_preview_polish_repair", return_value=None),
                    patch("api.main._run_backend_repair_pass", side_effect=[first_repair, second_repair]) as repair_pass,
                ):
                    result = main_mod._auto_execute_agent_result(
                        main_mod.AgentReq(input="make it production-ready", project_root="demo", build_mode="full-agent"),
                        [{"path": "demo/index.html", "new_content": "<html><body><section>Thin</section></body></html>"}],
                        [],
                        lambda _event, _data: None,
                        max_repair_passes=2,
                    )

                self.assertEqual(repair_pass.call_count, 2)
                self.assertTrue(result["ok"])
                self.assertEqual(result["preview_audit"]["issue_details"], [])
                self.assertEqual(len(result["repairs"]), 2)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_failure_analysis_drops_command_failures_resolved_by_repair_replay(self) -> None:
        execution = {
            "shell": {
                "results": [
                    {"ok": False, "command": "npm run build", "returncode": 127, "stdout": "tsc not found", "stderr": ""},
                ],
            },
            "validation": {
                "results": [
                    {"ok": False, "command": "npm run build", "returncode": 127, "stdout": "tsc not found", "stderr": ""},
                ],
            },
            "preview_audit": {
                "ok": False,
                "skipped": False,
                "issue_details": [
                    {"severity": "blocking", "category": "responsive-overflow", "detail": "mobile overflow"},
                ],
            },
            "repairs": [
                {
                    "execution": {
                        "replay": {
                            "results": [
                                {"ok": True, "command": "npm run build", "returncode": 0, "stdout": "built", "stderr": ""},
                            ],
                        }
                    }
                }
            ],
        }

        analysis = main_mod._execution_failure_analysis(execution)

        self.assertTrue(analysis["failures"])
        self.assertTrue(all(item["kind"] == "preview_audit" for item in analysis["failures"]))
        self.assertIn("preview audit failed", analysis["primary_failure"])

    def test_quick_ts6133_repair_inserts_void_usage_and_reruns_validation(self) -> None:
        session_id = "quick-ts6133-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src" / "components").mkdir(parents=True)
                app_shell = project / "src" / "components" / "AppShell.tsx"
                app_shell.write_text(
                    "type Props = { title: string; description: string; children: React.ReactNode };\n"
                    "export default function AppShell({ title, description, children }: Props) {\n"
                    "  return <main>{children}</main>;\n"
                    "}\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "apply": {"ok": True},
                    "validation": {
                        "ok": False,
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "ok": False,
                                "command": "npm run build",
                                "stderr": (
                                    "src/components/AppShell.tsx(2,36): error TS6133: 'title' is declared but its value is never read.\n"
                                    "src/components/AppShell.tsx(2,43): error TS6133: 'description' is declared but its value is never read."
                                ),
                            }
                        ],
                    },
                }
                rerun_shell = {
                    "ok": True,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                }
                events: list[tuple[str, dict]] = []
                with patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell):
                    result = main_mod._try_quick_ts6133_repair(
                        main_mod.AgentReq(input="fix build", project_root="demo", auto_execute=True),
                        execution,
                        lambda event, data: events.append((event, data)),
                    )

                self.assertTrue(result["ok"])
                text = app_shell.read_text(encoding="utf-8")
                self.assertIn("void title;", text)
                self.assertIn("void description;", text)
                self.assertTrue(any(data.get("tool") == "quick-repair" for event, data in events if event == "tool_output"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_ts6133_repair_removes_unused_import_without_touching_type_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src" / "components").mkdir(parents=True)
            app_shell = project / "src" / "components" / "AppShell.tsx"
            app_shell.write_text(
                "import Button from './ui/Button';\n"
                "import Card from './ui/Card';\n"
                "import type { ReactNode } from 'react';\n"
                "\n"
                "type Props = {\n"
                "  title: string;\n"
                "  description: string;\n"
                "  children: ReactNode;\n"
                "};\n"
                "\n"
                "export default function AppShell({ title, description, children }: Props) {\n"
                "  return <main>{children}</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [
                    {"path": "src/components/AppShell.tsx", "line": 1, "name": "Button"},
                    {"path": "src/components/AppShell.tsx", "line": 2, "name": "Card"},
                    {"path": "src/components/AppShell.tsx", "line": 11, "name": "title"},
                    {"path": "src/components/AppShell.tsx", "line": 11, "name": "description"},
                ],
            )

            text = app_shell.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/components/AppShell.tsx"])
            self.assertNotIn("import Button", text)
            self.assertNotIn("import Card", text)
            self.assertNotIn("void Button", text)
            self.assertNotIn("void Card", text)
            self.assertIn("title: string;", text)
            self.assertIn("description: string;", text)
            self.assertIn("void title;", text)
            self.assertIn("void description;", text)

    def test_quick_ts2304_repair_adds_missing_react_hook_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "import type { ReactNode } from 'react';\n"
                "\n"
                "type Props = { children?: ReactNode };\n"
                "\n"
                "export default function Dashboard({ children }: Props) {\n"
                "  const [ready, setReady] = useState(false);\n"
                "  useEffect(() => setReady(true), []);\n"
                "  return <main>{ready ? children : null}</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._add_missing_react_hook_imports(
                project,
                [
                    {"path": "src/Dashboard.tsx", "name": "useState"},
                    {"path": "src/Dashboard.tsx", "name": "useEffect"},
                ],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertIn("import { useEffect, useState } from 'react';", text)
            self.assertIn("import type { ReactNode } from 'react';", text)

    def test_ts2304_react_hook_parser_reads_build_output(self) -> None:
        execution = {
            "validation": {
                "results": [
                    {
                        "stdout": "src/App.tsx(8,29): error TS2304: Cannot find name 'useState'.\n"
                        "src/App.tsx(9,3): error TS2304: Cannot find name 'useEffect'.\n"
                    }
                ]
            }
        }

        issues = main_mod._ts2304_react_hook_issues_from_execution(execution)

        self.assertEqual(
            issues,
            [
                {"path": "src/App.tsx", "name": "useState"},
                {"path": "src/App.tsx", "name": "useEffect"},
            ],
        )

    def test_quick_vite_alias_repair_adds_src_alias_for_shadcn_imports(self) -> None:
        session_id = "quick-vite-alias-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src" / "components" / "ui").mkdir(parents=True)
                (project / "src" / "App.tsx").write_text(
                    "import { Button } from '@/components/ui/button';\nexport default function App(){ return <Button>Save</Button> }\n",
                    encoding="utf-8",
                )
                (project / "src" / "components" / "ui" / "button.tsx").write_text(
                    "export function Button(props: any) { return <button {...props} /> }\n",
                    encoding="utf-8",
                )
                (project / "vite.config.ts").write_text(
                    'import { defineConfig } from "vite";\nimport react from "@vitejs/plugin-react";\n\nexport default defineConfig({\n  plugins: [react()],\n});\n',
                    encoding="utf-8",
                )
                (project / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "ok": False,
                                "command": "npm run build",
                                "stdout": 'error during build:\n[vite]: Rollup failed to resolve import "@/components/ui/button" from "/tmp/demo/src/App.tsx".',
                                "stderr": "",
                            }
                        ],
                    }
                }

                with patch("api.main._run_harness_shell_actions_internal", return_value={"ok": True, "results": [{"ok": True, "command": "npm run build"}]}):
                    result = main_mod._try_quick_vite_alias_repair(
                        main_mod.AgentReq(input="fix shadcn import", project_root="demo"),
                        execution,
                        lambda _event, _data: None,
                    )

                config = (project / "vite.config.ts").read_text(encoding="utf-8")

            self.assertTrue(result["ok"])
            self.assertIn("node:url", config)
            self.assertIn("resolve", config)
            self.assertIn('"@"', config)
            self.assertEqual(result["changed_paths"], ["vite.config.ts"])
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_surgical_shadcn_import_repair_skips_preview_gate_after_build_passes(self) -> None:
        session_id = "surgical-shadcn-import-preview-skip-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src" / "components" / "ui").mkdir(parents=True)
                (project / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
                (project / "index.html").write_text('<div id="root"></div><script type="module" src="/src/main.tsx"></script>\n', encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                shell_ok = {"ok": True, "results": [{"ok": True, "command": "npm run build", "stdout": "built"}], "ran": 1}
                with patch("api.main._run_harness_shell_actions_internal", return_value=shell_ok), \
                    patch("api.main._infer_validation_commands", return_value=["npm run build"]), \
                    patch("api.main._auto_execute_preview_audit") as preview_mock:
                    execution = main_mod._auto_execute_agent_result(
                        main_mod.AgentReq(
                            input="Fix the broken '@/components/ui/button' import by adding or correcting the actual shadcn component file. Keep the existing shadcn/Tailwind setup and validate imports/build.",
                            project_root="demo",
                            auto_execute=True,
                        ),
                        [{"path": "demo/src/components/ui/button.tsx", "new_content": "export function Button(props: any) { return <button {...props} /> }\n"}],
                        [{"type": "shell", "command": "npm run build", "cwd": "demo"}],
                        lambda *_args: None,
                    )

            self.assertTrue(execution["ok"])
            self.assertIsNone(execution["preview_audit"])
            preview_mock.assert_not_called()
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_ts6133_repair_removes_unused_usestate_setter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "import { useState } from 'react';\n"
                "export default function Dashboard() {\n"
                "  const [tasks, setTasks] = useState<string[]>([]);\n"
                "  return <main>{tasks.length}</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [{"path": "src/Dashboard.tsx", "line": 3, "name": "setTasks"}],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertIn("const [tasks] = useState<string[]>([]);", text)
            self.assertNotIn("void setTasks", text)

    def test_quick_ts6133_repair_removes_fully_unused_usestate_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "import { useState } from 'react';\n"
                "export default function Dashboard() {\n"
                "  const [query, setQuery] = useState('');\n"
                "  const [showEmptyState, setShowEmptyState] = useState(false);\n"
                "  return <main>{query}<button onClick={() => setQuery('x')}>Set</button></main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [
                    {"path": "src/Dashboard.tsx", "line": 4, "name": "showEmptyState"},
                    {"path": "src/Dashboard.tsx", "line": 4, "name": "setShowEmptyState"},
                ],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertNotIn("showEmptyState", text)
            self.assertNotIn("setShowEmptyState", text)
            self.assertNotIn("void showEmptyState", text)
            self.assertIn("const [query, setQuery] = useState('');", text)

    def test_quick_ts6133_repair_preserves_used_usestate_setter_when_value_unused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "import { useState } from 'react';\n"
                "export default function Dashboard() {\n"
                "  const [query, setQuery] = useState('');\n"
                "  return <main><button onClick={() => setQuery('x')}>Set</button></main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [{"path": "src/Dashboard.tsx", "line": 3, "name": "query"}],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertIn("const [, setQuery] = useState('');", text)
            self.assertNotIn("void query", text)

    def test_quick_ts6133_repair_skips_arrow_expression_body_props(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            original = (
                "const StatChip = ({\n"
                "  label,\n"
                "  color,\n"
                "}: {\n"
                "  label: string;\n"
                "  color: string;\n"
                "}) => (\n"
                "  <span>{label}</span>\n"
                ");\n"
                "\n"
                "export default function Dashboard() {\n"
                "  return <StatChip label=\"Open\" color=\"green\" />;\n"
                "}\n"
            )
            page.write_text(original, encoding="utf-8")

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [{"path": "src/Dashboard.tsx", "line": 3, "name": "color"}],
            )

            self.assertEqual(changed, [])
            self.assertEqual(page.read_text(encoding="utf-8"), original)

    def test_quick_ts6133_repair_references_unused_local_const_after_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "export default function Dashboard() {\n"
                "  const statusColor = '#22c55e';\n"
                "  return <main>ok</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [{"path": "src/Dashboard.tsx", "line": 2, "name": "statusColor"}],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertIn("  const statusColor = '#22c55e';\n  void statusColor;\n", text)
            self.assertNotIn("function Dashboard() {\n  void statusColor;\n", text)

    def test_quick_ts6133_repair_references_unused_multiline_const_after_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "export default function Dashboard() {\n"
                "  const statusLabels = {\n"
                "    todo: 'Todo',\n"
                "    done: 'Done',\n"
                "  };\n"
                "  return <main>ok</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [{"path": "src/Dashboard.tsx", "line": 2, "name": "statusLabels"}],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertIn("  };\n  void statusLabels;\n  return <main>ok</main>;", text)
            self.assertNotIn("function Dashboard() {\n  void statusLabels;\n", text)

    def test_quick_ts6133_repair_references_unused_function_after_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "src").mkdir(parents=True)
            page = project / "src" / "Dashboard.tsx"
            page.write_text(
                "function StatusBadge() {\n"
                "  return <span>Active</span>;\n"
                "}\n"
                "\n"
                "export default function Dashboard() {\n"
                "  return <main>ok</main>;\n"
                "}\n",
                encoding="utf-8",
            )

            changed = main_mod._insert_void_usage_for_unused_symbols(
                project,
                [{"path": "src/Dashboard.tsx", "line": 1, "name": "StatusBadge"}],
            )

            text = page.read_text(encoding="utf-8")
            self.assertEqual(changed, ["src/Dashboard.tsx"])
            self.assertIn("}\nvoid StatusBadge;\n\nexport default function Dashboard", text)
            self.assertNotIn("function StatusBadge() {\n  void StatusBadge;\n", text)

    def test_quick_ts6133_failed_validation_rolls_back_mechanical_edit(self) -> None:
        session_id = "quick-ts6133-rollback-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                page = project / "src" / "Dashboard.tsx"
                original = (
                    "import Card from './Card';\n"
                    "import Button from './Button';\n"
                    "export default function Dashboard() {\n"
                    "  return <main>ok</main>;\n"
                    "}\n"
                )
                page.write_text(original, encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "ok": False,
                                "stderr": (
                                    "src/Dashboard.tsx(1,1): error TS6133: 'Card' is declared but its value is never read.\n"
                                    "src/Dashboard.tsx(2,1): error TS6133: 'Button' is declared but its value is never read.\n"
                                ),
                            }
                        ],
                    },
                }

                with patch("api.main._run_harness_shell_actions_internal", return_value={"ok": False, "results": [{"ok": False}]}):
                    result = main_mod._try_quick_ts6133_repair(
                        main_mod.AgentReq(input="fix", project_root="demo"),
                        execution,
                        lambda _event, _data: None,
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(result["rolled_back_paths"], ["src/Dashboard.tsx"])
                self.assertEqual(page.read_text(encoding="utf-8"), original)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_degrading_repair_checkpoint_can_be_rolled_back(self) -> None:
        session_id = "repair-checkpoint-rollback-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                target = project / "src" / "Dashboard.tsx"
                target.write_text("export default function Dashboard() { return <main>ok</main>; }\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                apply_result = main_mod.agent_harness_apply(
                    main_mod.AgentHarnessApplyReq(
                        project_root="demo",
                        changes=[
                            main_mod.AgentHarnessApplyChange(
                                path="demo/src/Dashboard.tsx",
                                content="export default function Dashboard() { return <main><div></main>; }\n",
                            )
                        ],
                    )
                )
                parent_execution = {
                    "ok": False,
                    "validation": {"ok": True, "results": [{"ok": True}]},
                    "preview_audit": {"ok": False, "skipped": False, "issue_details": [{"severity": "blocking", "category": "content"}]},
                }
                repair_execution = {
                    "ok": False,
                    "apply": apply_result,
                    "validation": {
                        "ok": False,
                        "results": [
                            {
                                "ok": False,
                                "stderr": "src/Dashboard.tsx(1,54): error TS17002: Expected corresponding JSX closing tag for 'div'.",
                            }
                        ],
                    },
                }

                self.assertTrue(main_mod._repair_execution_degrades_parent(parent_execution, repair_execution))
                rollback = main_mod._rollback_repair_checkpoint(repair_execution)

                self.assertTrue(rollback["ok"])
                self.assertEqual(target.read_text(encoding="utf-8"), "export default function Dashboard() { return <main>ok</main>; }\n")
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_repair_preview_regression_counts_as_degradation(self) -> None:
        parent_execution = {
            "ok": True,
            "preview_audit": {
                "ok": True,
                "skipped": False,
                "issue_details": [{"severity": "warning", "category": "source-quality"}],
            },
        }
        repair_execution = {
            "ok": False,
            "preview_audit": {
                "ok": False,
                "skipped": False,
                "issue_details": [
                    {"severity": "blocking", "category": "responsive"},
                    {"severity": "warning", "category": "source-quality"},
                ],
            },
        }

        self.assertTrue(main_mod._repair_execution_degrades_parent(parent_execution, repair_execution))

    def test_rolled_back_degrading_repair_preserves_parent_preview_state(self) -> None:
        session_id = "rolled-back-repair-preserves-parent-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "src" / "App.jsx").write_text("export default function App() { return <main>Old</main>; }\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                parent_preview = {
                    "ok": True,
                    "skipped": False,
                    "audit_mode": "browser",
                    "issue_details": [{"severity": "warning", "category": "product-depth", "detail": "Surface can be deeper."}],
                    "summary": "Preview ok with warnings.",
                }
                bad_repair_preview = {
                    "ok": False,
                    "skipped": False,
                    "audit_mode": "browser",
                    "issue_details": [{"severity": "blocking", "category": "responsive", "detail": "Overlay blocks preview."}],
                    "summary": "Preview blocked.",
                }
                repair = {
                    "changes": [{"path": "demo/src/App.jsx", "new_content": "export default function App() { return <main><div></main>; }\n"}],
                    "actions": [],
                    "execution": {
                        "ok": False,
                        "preview_audit": bad_repair_preview,
                        "apply": {"ok": True, "checkpoint_path": "demo/.voiceide/checkpoints/bad.json"},
                        "validation": {"ok": True, "ran": 0, "failed": 0, "commands": []},
                        "failure_analysis": {"summary": "Preview blocked."},
                    },
                }

                with (
                    patch("api.main._infer_validation_commands", return_value=[]),
                    patch("api.main._project_has_preview_surface", return_value=True),
                    patch("api.main._auto_execute_preview_audit", return_value=parent_preview),
                    patch("api.main._run_backend_repair_pass", return_value=repair),
                    patch("api.main._repair_execution_degrades_parent", return_value=True),
                    patch("api.main._rollback_repair_checkpoint", return_value={"ok": True, "checkpoint_path": "demo/.voiceide/checkpoints/bad.json"}),
                ):
                    execution = main_mod._auto_execute_agent_result(
                        main_mod.AgentReq(input="polish preview", project_root="demo", auto_execute=True),
                        [{"path": "demo/src/App.jsx", "new_content": "export default function App() { return <main>Dashboard</main>; }\n"}],
                        [],
                        lambda *_args: None,
                        max_repair_passes=1,
                    )

                self.assertTrue(execution["ok"])
                self.assertEqual(execution["preview_audit"], parent_preview)
                self.assertEqual(execution["completion_report"]["state"], "complete-with-warnings")
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_repair_provider_failure_returns_structured_execution(self) -> None:
        session_id = "repair-provider-failure-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "src" / "App.tsx").write_text("export default function App() { return <main />; }\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "ok": True,
                    "preview_audit": {
                        "ok": True,
                        "skipped": False,
                        "issue_details": [{"severity": "warning", "category": "product-depth", "detail": "thin"}],
                    },
                }

                events: list[tuple[str, dict]] = []
                with patch("api.main.run_agent_pipeline", side_effect=RuntimeError("nine_router key ditolak. Cek ulang API key di Settings.")):
                    repair = main_mod._run_backend_repair_pass(
                        main_mod.AgentReq(input="fix", project_root="demo", build_mode="full-agent"),
                        execution,
                        lambda event, data: events.append((event, data)),
                        repair_index=1,
                    )

                repair_execution = repair["execution"]
                self.assertFalse(repair_execution["ok"])
                self.assertEqual(repair_execution["failure_analysis"]["current_signature"], "repair-provider-error")
                self.assertIn("nine_router key ditolak", repair_execution["failure_analysis"]["summary"])
                self.assertIn("completion_report", repair_execution)
                self.assertTrue(any(data.get("tool") == "repair-model" for event, data in events if event == "tool_output"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_repair_prompt_includes_preview_repair_targets(self) -> None:
        session_id = "repair-preview-targets-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "src" / "App.tsx").write_text("export default function App() { return <main />; }\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "ok": False,
                    "preview_audit": {
                        "ok": False,
                        "skipped": False,
                        "repair_brief": "Preview audit mode=agent-browser, blocking=1.",
                        "evidence_pack": {
                            "repair_targets": [
                                {
                                    "kind": "responsive",
                                    "priority": "high",
                                    "likely_files": ["src/app.css"],
                                    "selectors": ["h1.hero"],
                                    "action": "Fix mobile overflow by changing CSS.",
                                }
                            ]
                        },
                        "issue_details": [{"severity": "blocking", "category": "responsive", "detail": "overflow"}],
                    },
                }
                captured: dict[str, str] = {}

                def fake_pipeline(req, ws_root, emit):
                    captured["input"] = req.input
                    return {
                        "spoken": "repair",
                        "log": "",
                        "changes": [{"path": "src/app.css", "new_content": "body { margin: 0; }\n"}],
                        "actions": [],
                        "trace": {"verification": []},
                    }

                with patch("api.main.run_agent_pipeline", side_effect=fake_pipeline), \
                    patch("api.main._auto_execute_agent_result", return_value={"ok": True, "preview_audit": {"ok": True, "skipped": False, "issue_details": []}}):
                    main_mod._run_backend_repair_pass(
                        main_mod.AgentReq(input="fix preview", project_root="demo", build_mode="full-agent"),
                        execution,
                        lambda _event, _data: None,
                        repair_index=1,
                    )

        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

        self.assertIn("PREVIEW REPAIR TARGETS", captured["input"])
        self.assertIn("src/app.css", captured["input"])
        self.assertIn("h1.hero", captured["input"])

    def test_quick_ts2741_repair_relaxes_missing_required_ui_prop(self) -> None:
        session_id = "quick-ts2741-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src" / "components" / "ui").mkdir(parents=True)
                (project / "src" / "pages").mkdir(parents=True)
                card = project / "src" / "components" / "ui" / "Card.tsx"
                card.write_text(
                    "import type { ReactNode } from 'react';\n"
                    "export default function Card(props: { title: string; children: ReactNode }) {\n"
                    "  const { title, children } = props;\n"
                    "  return <section><h2>{title}</h2><div>{children}</div></section>;\n"
                    "}\n",
                    encoding="utf-8",
                )
                (project / "src" / "pages" / "Dashboard.tsx").write_text(
                    "import Card from '../components/ui/Card';\n"
                    "export default function Dashboard() {\n"
                    "  return <Card><p>Total</p></Card>;\n"
                    "}\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "ok": False,
                                "command": "npm run build",
                                "stderr": "src/pages/Dashboard.tsx(3,10): error TS2741: Property 'title' is missing in type '{ children: Element; }' but required in type '{ title: string; children: ReactNode; }'.",
                            }
                        ],
                    },
                }

                with patch("api.main._run_harness_shell_actions_internal", return_value={"ok": True, "results": []}):
                    result = main_mod._try_quick_ts2741_missing_required_prop_repair(
                        main_mod.AgentReq(input="fix", project_root="demo"),
                        execution,
                        lambda _event, _data: None,
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["changed_paths"], ["src/components/ui/Card.tsx"])
                text = card.read_text(encoding="utf-8")
                self.assertIn("title?: string", text)
                self.assertIn("{title ? <h2>{title}</h2> : null}", text)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_preview_polish_repair_updates_metadata_and_tap_targets(self) -> None:
        session_id = "quick-preview-polish-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "index.html").write_text(
                    "<!doctype html>\n"
                    "<html><head><title>Build an AI tool app workspace with prompt panel</title></head>"
                    "<body><div id=\"root\"></div></body></html>\n",
                    encoding="utf-8",
                )
                (project / "src" / "app.css").write_text("button { border: 0; }\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "ok": True,
                    "apply": {"ok": True},
                    "validation": {
                        "ok": True,
                        "commands": ["npm run build"],
                        "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                    },
                    "preview_audit": {
                        "ok": True,
                        "skipped": False,
                        "issue_details": [
                            {"severity": "warning", "category": "metadata", "detail": "Preview page is missing a meta description."},
                            {"severity": "warning", "category": "mobile-tap-targets", "detail": "Target tap terlalu kecil."},
                        ],
                        "visual_summary": {
                            "title": "Build an AI tool app workspace with prompt panel",
                            "primary_heading": "OpsBoard AI helps operations teams clear incidents faster.",
                        },
                    },
                }
                rerun_shell = {
                    "ok": True,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                }
                clean_preview = {"ok": True, "skipped": False, "issue_details": [], "summary": "Preview clean."}
                events: list[tuple[str, dict]] = []
                with (
                    patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell),
                    patch("api.main._auto_execute_preview_audit", return_value=clean_preview),
                ):
                    result = main_mod._try_quick_preview_polish_repair(
                        main_mod.AgentReq(input="polish preview", project_root="demo", auto_execute=True),
                        execution,
                        lambda event, data: events.append((event, data)),
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["kind"], "preview-polish")
                self.assertEqual(result["preview_audit"], clean_preview)
                html = (project / "index.html").read_text(encoding="utf-8")
                self.assertIn("<title>OpsBoard AI helps operations teams clear incidents faster</title>", html)
                self.assertIn('<meta name="description"', html)
                self.assertIn("OpsBoard AI helps operations teams clear incidents faster", html)
                css = (project / "src" / "app.css").read_text(encoding="utf-8")
                self.assertIn("Appora quick polish: tap target floor", css)
                self.assertIn("min-height: 44px", css)
                self.assertTrue(any(data.get("tool") == "quick-repair" for event, data in events if event == "tool_output"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_preview_polish_repair_softens_overflow_prone_css(self) -> None:
        session_id = "quick-preview-overflow-polish-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "src" / "app.css").write_text(
                    ".brand { white-space: nowrap; width: 100vw; }\n"
                    ".rail { min-width: 720px; width: max-content; }\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "ok": True,
                    "apply": {"ok": True},
                    "validation": {
                        "ok": True,
                        "commands": ["npm run build"],
                        "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                    },
                    "preview_audit": {
                        "ok": True,
                        "skipped": False,
                        "issue_details": [
                            {
                                "severity": "warning",
                                "category": "responsive",
                                "detail": "Source has 4 CSS pattern(s) commonly causing mobile overflow.",
                            }
                        ],
                    },
                }
                rerun_shell = {
                    "ok": True,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                }
                clean_preview = {"ok": True, "skipped": False, "issue_details": [], "summary": "Preview clean."}
                with (
                    patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell),
                    patch("api.main._auto_execute_preview_audit", return_value=clean_preview),
                ):
                    result = main_mod._try_quick_preview_polish_repair(
                        main_mod.AgentReq(input="polish preview", project_root="demo", auto_execute=True),
                        execution,
                        lambda _event, _data: None,
                    )

                self.assertTrue(result["ok"])
                self.assertIn("src/app.css", result["changed_paths"])
                css = (project / "src" / "app.css").read_text(encoding="utf-8")
                self.assertNotIn("white-space: nowrap", css)
                self.assertNotIn("width: 100vw", css)
                self.assertNotIn("width: max-content", css)
                self.assertNotIn("min-width: 720px", css)
                self.assertIn("max-width: 100%", css)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_preview_polish_repair_targets_vite_styles_css(self) -> None:
        session_id = "quick-preview-styles-css-polish-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "src" / "styles.css").write_text(
                    ".task-table { min-width: 600px; }\n"
                    ".assignee { white-space: nowrap; }\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "ok": True,
                    "apply": {"ok": True},
                    "validation": {
                        "ok": True,
                        "commands": ["npm run build"],
                        "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                    },
                    "preview_audit": {
                        "ok": True,
                        "skipped": False,
                        "issue_details": [
                            {"severity": "warning", "category": "mobile-tap-targets", "detail": "Target kecil."},
                            {"severity": "warning", "category": "responsive", "detail": "overflow risk."},
                        ],
                        "visual_summary": {"title": "Task Tracker", "primary_heading": "Task Tracker"},
                    },
                }
                rerun_shell = {
                    "ok": True,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                }
                clean_preview = {"ok": True, "skipped": False, "issue_details": [], "summary": "Preview clean."}
                with (
                    patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell),
                    patch("api.main._auto_execute_preview_audit", return_value=clean_preview),
                ):
                    result = main_mod._try_quick_preview_polish_repair(
                        main_mod.AgentReq(input="polish preview", project_root="demo", auto_execute=True),
                        execution,
                        lambda _event, _data: None,
                    )

                self.assertTrue(result["ok"])
                self.assertIn("src/styles.css", result["changed_paths"])
                css = (project / "src" / "styles.css").read_text(encoding="utf-8")
                self.assertIn("Appora quick polish: tap target floor", css)
                self.assertIn("min-width: 0; max-width: 100%;", css)
                self.assertIn("white-space: normal;", css)
                self.assertIn('input[type="checkbox"]', css)
                self.assertIn("width: 44px;", css)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_vite_entrypoint_repair_restores_existing_main_file(self) -> None:
        session_id = "quick-vite-entrypoint-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                (project / "index.html").write_text(
                    "<!doctype html><html><body><div id='root'></div><script type=\"module\" src=\"/src/main.tsx\"></script></body></html>\n",
                    encoding="utf-8",
                )
                (project / "src" / "main.jsx").write_text("import './styles.css';\n", encoding="utf-8")
                (project / "src" / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {
                        "ok": False,
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "ok": False,
                                "stdout": "Error: Failed to resolve /src/main.tsx from /tmp/demo/index.html",
                                "stderr": "",
                            }
                        ],
                    }
                }
                rerun_shell = {
                    "ok": True,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                }

                with patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell):
                    result = main_mod._try_quick_vite_entrypoint_repair(
                        main_mod.AgentReq(input="fix build", project_root="demo", auto_execute=True),
                        execution,
                        lambda *_args: None,
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["changed_paths"], ["index.html"])
                html = (project / "index.html").read_text(encoding="utf-8")
                self.assertIn('/src/main.jsx', html)
                self.assertNotIn('/src/main.tsx', html)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_missing_h1_repair_promotes_existing_title_element(self) -> None:
        session_id = "quick-missing-h1-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src").mkdir(parents=True)
                app = project / "src" / "App.jsx"
                app.write_text(
                    "export default function App() {\n"
                    "  return <main><div className=\"header-title\">Task Tracker Tim Produk</div><p>Dashboard</p></main>;\n"
                    "}\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {"ok": True, "commands": ["npm run build"], "results": [{"ok": True}]},
                    "preview_audit": {
                        "ok": False,
                        "skipped": False,
                        "issue_details": [
                            {"severity": "blocking", "category": "content", "detail": "Preview page has no visible H1 heading."}
                        ],
                    },
                }
                rerun_shell = {
                    "ok": True,
                    "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                }
                clean_preview = {"ok": True, "skipped": False, "issue_details": [], "summary": "Preview clean."}

                with (
                    patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell),
                    patch("api.main._auto_execute_preview_audit", return_value=clean_preview),
                ):
                    result = main_mod._try_quick_missing_h1_repair(
                        main_mod.AgentReq(input="fix preview", project_root="demo", auto_execute=True),
                        execution,
                        lambda *_args: None,
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["changed_paths"], ["src/App.jsx"])
                content = app.read_text(encoding="utf-8")
                self.assertIn('<h1 className="header-title">Task Tracker Tim Produk</h1>', content)
                self.assertNotIn('<div className="header-title">Task Tracker Tim Produk</div>', content)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_missing_package_repair_installs_allowed_dependency_and_reruns_validation(self) -> None:
        session_id = "quick-missing-package-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "package.json").write_text(
                    json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "^18.3.1"}}),
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": root,
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "apply": {"ok": True},
                    "validation": {
                        "ok": False,
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "ok": False,
                                "command": "npm run build",
                                "stderr": "src/App.tsx(1,22): error TS2307: Cannot find module 'lucide-react' or its corresponding type declarations.",
                            }
                        ],
                    },
                }
                rerun_shell = {
                    "ok": True,
                    "results": [
                        {"ok": True, "command": "npm install --no-audit --no-fund lucide-react", "stdout": "added", "stderr": ""},
                        {"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""},
                    ],
                }
                events: list[tuple[str, dict]] = []
                with patch("api.main._run_harness_shell_actions_internal", return_value=rerun_shell):
                    result = main_mod._try_quick_missing_package_repair(
                        main_mod.AgentReq(input="fix build", project_root="demo", auto_execute=True),
                        execution,
                        lambda event, data: events.append((event, data)),
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["packages"], ["lucide-react"])
                self.assertIn("npm install --no-audit --no-fund lucide-react", result["commands"])
                self.assertTrue(any(data.get("tool") == "quick-repair" for event, data in events if event == "tool_output"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_run_agent_impl_can_auto_execute_apply_and_shell_harness(self) -> None:
        session_id = "auto-execute-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "src").mkdir()
                (project / "src" / "App.tsx").write_text("old\n", encoding="utf-8")
                (project / "ok.py").write_text("print('ok')\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                pipeline = {
                    "spoken": "Aku apply patch dan validasi dari backend harness.",
                    "log": "fake",
                    "changes": [{"path": "demo/src/App.tsx", "new_content": "agent edit\n"}],
                    "actions": [{"type": "shell", "command": "python3 -m compileall .", "reason": "validate"}],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }

                events: list[tuple[str, dict]] = []
                with patch("api.main.run_agent_pipeline", return_value=pipeline), \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="patch and validate", project_root="demo", auto_execute=True),
                        event_cb=lambda event, data: events.append((event, data)),
                    )

                self.assertTrue(result["execution"]["ok"])
                self.assertTrue(result["execution"]["apply"]["applied"])
                self.assertEqual(result["execution"]["shell"]["ran"], 1)
                self.assertTrue(result["execution"]["shell"]["results"][0]["ok"])
                self.assertTrue(result["execution"]["validation"]["ok"])
                self.assertEqual(result["execution"]["validation"]["ran"], 0)
                self.assertGreaterEqual(result["execution"]["validation"]["reused"], 1)
                self.assertTrue(result["execution"]["run_ledger"])
                ledger_phases = [item["phase"] for item in result["execution"]["run_ledger"]]
                self.assertIn("observe", ledger_phases)
                self.assertIn("edit", ledger_phases)
                self.assertIn("run", ledger_phases)
                self.assertIn("verify", ledger_phases)
                self.assertIn("complete", ledger_phases)
                step_kinds = [step["kind"] for step in result["execution"]["steps"]]
                self.assertIn("apply", step_kinds)
                self.assertIn("shell", step_kinds)
                self.assertIn("validation", step_kinds)

                tool_calls = [data for event, data in events if event == "tool_call" and data.get("kind") == "agent_harness"]
                tool_outputs = [data for event, data in events if event == "tool_output" and data.get("kind") == "agent_harness"]
                command_calls = [data for event, data in events if event == "tool_call" and data.get("kind") == "agent_harness_command"]
                command_outputs = [data for event, data in events if event == "tool_output" and data.get("kind") == "agent_harness_command"]
                command_chunks = [data for event, data in events if event == "tool_output" and data.get("kind") == "agent_harness_command_chunk"]
                self.assertTrue(any(item.get("tool") == "apply" and item.get("paths") == ["demo/src/App.tsx"] for item in tool_calls))
                shell_output = next(item for item in tool_outputs if item.get("tool") == "run-shell")
                validation_output = next(item for item in tool_outputs if item.get("tool") == "validate")
                self.assertIn("summary", shell_output)
                self.assertIn("commands", shell_output)
                self.assertIn("results", shell_output)
                self.assertEqual(shell_output["results"][0]["command"], "python3 -m compileall .")
                self.assertIn("summary", validation_output)
                self.assertIn("commands", validation_output)
                self.assertIn("results", validation_output)
                self.assertGreaterEqual(result["execution"]["validation"]["reused"], 1)
                self.assertTrue(any(item.get("tool") == "run-shell" and item.get("command") == "python3 -m compileall ." for item in command_calls))
                self.assertTrue(any(item.get("tool") == "run-shell" and item.get("status") == "passed" for item in command_outputs))
                self.assertTrue(any(item.get("tool") == "run-shell" and item.get("stream") == "stdout" for item in command_chunks))
                self.assertEqual((project / "src" / "App.tsx").read_text(encoding="utf-8"), "agent edit\n")
                with patch("api.agent_memory.has_supabase", return_value=False):
                    memory = retrieve_agent_memory(
                        root,
                        project_dir=project,
                        project_root="demo",
                        interaction_kind="command",
                        query="patch validate compile outcome",
                        active_rel="src/App.tsx",
                        open_files=["src/App.tsx"],
                        limit_short=6,
                        limit_long=0,
                    )
                self.assertIn("EXECUTION OUTCOME", memory.prompt)
                self.assertIn("validation=passed", memory.prompt)
                self.assertIn("python3 -m compileall .", memory.prompt)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_run_agent_impl_streams_unresolved_handoff_after_native_progress(self) -> None:
        session_id = "unresolved-handoff-stream-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }

                def fake_pipeline(_req, ws_root=None, emit=None):
                    self.assertEqual(ws_root, root)
                    emit("delta", {"spoken_chunk": "Aku cek dulu struktur routing."})
                    return {
                        "spoken": (
                            "Aku cek dulu struktur routing.\n\n"
                            "Belum selesai sampai lolos.\n"
                            "Blocker terakhir: has-work-output: Build request produced no file changes/actions.\n"
                            "Langkah lanjut yang harus dilakukan: patch file yang bikin preview blank."
                        ),
                        "log": "fake",
                        "changes": [],
                        "actions": [],
                        "intent": {"kind": "command", "should_write_files": True},
                        "trace": {
                            "verification": [
                                {
                                    "name": "has-work-output",
                                    "ok": False,
                                    "severity": "hard",
                                    "detail": "Build request produced no file changes/actions.",
                                }
                            ],
                            "warnings": [],
                            "task_state": {
                                "status": "blocked",
                                "blocking_checks": ["has-work-output"],
                            },
                        },
                    }

                events: list[tuple[str, dict]] = []
                with patch("api.main.run_agent_pipeline", side_effect=fake_pipeline), \
                    patch("api.main.has_supabase", return_value=False):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix preview blank", project_root="demo", build_mode="full-agent", auto_execute=False),
                        event_cb=lambda event, data: events.append((event, data)),
                    )

                streamed = "".join(str(data.get("spoken_chunk") or "") for event, data in events if event == "delta")
                self.assertIn("Aku cek dulu struktur routing.", streamed)
                self.assertIn("Belum selesai", streamed)
                self.assertIn("sampai lolos.", streamed)
                self.assertIn("Blocker", streamed)
                self.assertIn("has-work-output", streamed)
                self.assertIn("Langkah lanjut", streamed)
                self.assertIn("harus", streamed)
                self.assertIn("dilakukan", streamed)
                self.assertEqual(result["spoken"].count("Belum selesai sampai lolos."), 1)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_auto_execute_shell_only_repair_reruns_project_validation(self) -> None:
        session_id = "shell-only-repair-validation-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "package.json").write_text(
                    json.dumps({
                        "scripts": {
                            "prepare-agent": "node -e \"require('fs').writeFileSync('ready.txt','ok')\"",
                            "build": "node -e \"process.exit(require('fs').existsSync('ready.txt') ? 0 : 1)\"",
                        }
                    }),
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }

                events: list[tuple[str, dict]] = []
                execution = main_mod._auto_execute_agent_result(
                    main_mod.AgentReq(input="repair dependency then validate", project_root="demo", auto_execute=True),
                    [],
                    [{"type": "shell", "command": "npm run prepare-agent", "reason": "prepare project before validation"}],
                    lambda event, data: events.append((event, data)),
                    allow_repair=False,
                )

                self.assertTrue(execution["shell"]["ok"])
                self.assertTrue(execution["validation"]["ok"])
                self.assertEqual(execution["validation"]["commands"], ["npm run build"])
                self.assertTrue((project / "ready.txt").exists())
                self.assertTrue(any(data.get("tool") == "validate" for event, data in events if event == "tool_call"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_repair_loop_adopts_successful_validation_before_preview_repair(self) -> None:
        session_id = "repair-adopts-validation-before-preview-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "src").mkdir()
                (project / "src" / "App.tsx").write_text("old\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                preview_fail = {
                    "ok": False,
                    "skipped": False,
                    "issue_details": [
                        {"severity": "blocking", "category": "starter-residue", "detail": "Sisa template Vite."}
                    ],
                }
                preview_ok = {"ok": True, "skipped": False, "issue_details": []}
                repair_calls: list[dict] = []

                def fake_repair(_req, execution, _emit, *, repair_index):
                    repair_calls.append({
                        "repair_index": repair_index,
                        "validation_ok": (execution.get("validation") or {}).get("ok") if isinstance(execution.get("validation"), dict) else None,
                        "primary_failure": main_mod._execution_failure_analysis(execution).get("primary_failure"),
                    })
                    validation = {
                        "ok": True,
                        "project_root": "demo",
                        "commands": ["npm run build"],
                        "results": [{"ok": True, "command": "npm run build", "stdout": "built", "stderr": ""}],
                        "ran": 1,
                        "passed": 1,
                        "failed": 0,
                    }
                    if repair_index == 1:
                        return {
                            "changes": [],
                            "actions": [{"type": "shell", "command": "npm install"}],
                            "execution": {
                                "ok": False,
                                "apply": None,
                                "shell": {"ok": True, "ran": 1, "results": [{"ok": True, "command": "npm install"}]},
                                "validation": validation,
                                "preview_audit": preview_fail,
                                "repairs": [],
                                "failure_analysis": {},
                            },
                        }
                    return {
                        "changes": [{"path": "demo/src/App.tsx", "new_content": "fixed\n"}],
                        "actions": [],
                        "execution": {
                            "ok": True,
                            "apply": {"ok": True, "applied": True, "count": 1},
                            "shell": {"ok": True, "ran": 0, "results": []},
                            "validation": validation,
                            "preview_audit": preview_ok,
                            "repairs": [],
                            "failure_analysis": {},
                        },
                    }

                with patch("api.main.agent_harness_apply", return_value={"ok": True, "applied": True, "count": 1, "paths": ["demo/src/App.tsx"]}), \
                    patch("api.main._infer_validation_commands", return_value=["npm run build"]), \
                    patch("api.main._run_harness_shell_actions_internal", return_value={
                        "ok": False,
                        "results": [{"ok": False, "command": "npm run build", "returncode": 1, "stdout": "cannot find module react", "stderr": ""}],
                    }), \
                    patch("api.main._auto_execute_preview_audit", return_value=preview_fail), \
                    patch("api.main._run_backend_repair_pass", side_effect=fake_repair):
                    result = main_mod._auto_execute_agent_result(
                        main_mod.AgentReq(input="fix app", project_root="demo", auto_execute=True),
                        [{"path": "demo/src/App.tsx", "new_content": "new\n"}],
                        [],
                        lambda *_args: None,
                        max_repair_passes=2,
                    )

                self.assertEqual(len(repair_calls), 2)
                self.assertFalse(repair_calls[0]["validation_ok"])
                self.assertTrue(repair_calls[1]["validation_ok"])
                self.assertIn("preview audit", repair_calls[1]["primary_failure"])
                self.assertTrue(result["ok"])
                self.assertTrue(result["validation"]["ok"])
                self.assertTrue(result["preview_audit"]["ok"])
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_repairs_verifier_failure_before_apply(self) -> None:
        session_id = "auto-execute-verifier-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "README.md").write_text("old\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                first = {
                    "spoken": "Aku sudah siap patch.",
                    "log": "first",
                    "changes": [
                        {"path": "demo/README.md", "new_content": "bad\n"},
                        {"path": "demo/src/Card.tsx", "new_content": "export default function Card() { return null; }\n"},
                    ],
                    "actions": [],
                    "intent": {"kind": "command", "should_write_files": True},
                    "trace": {
                        "verification": [
                            {"name": "valid-change-paths", "ok": False, "detail": "Invalid path.", "severity": "hard"}
                        ],
                        "warnings": [],
                    },
                }
                repair = SimpleNamespace(
                    spoken="Verifier sudah diperbaiki dan patch siap apply.",
                    log="repair",
                    changes=[{"path": "demo/README.md", "new_content": "fixed\n"}],
                    actions=[],
                )

                events: list[tuple[str, dict]] = []
                with patch("api.main.run_agent_pipeline", return_value=first) as mocked_pipeline, \
                    patch("api.agent.suggest", return_value=repair), \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix readme", project_root="demo", auto_execute=True),
                        event_cb=lambda event, data: events.append((event, data)),
                    )

                self.assertEqual(mocked_pipeline.call_count, 1)
                self.assertTrue(result["verifier_repair"]["ok"])
                self.assertTrue(result["verifier_repair"]["targeted"])
                self.assertTrue(result["execution"]["ok"])
                self.assertTrue(result["execution"]["apply"]["applied"])
                self.assertEqual((project / "README.md").read_text(encoding="utf-8"), "fixed\n")
                self.assertEqual(
                    (project / "src" / "Card.tsx").read_text(encoding="utf-8"),
                    "export default function Card() { return null; }\n",
                )
                self.assertTrue(any(data.get("phase") == "verifier_repair" for event, data in events if event == "status"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_verifier_repair_uses_targeted_call_not_full_pipeline_restart(self) -> None:
        session_id = "auto-execute-targeted-verifier-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "README.md").write_text("old\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                first = {
                    "spoken": "Aku sudah siap patch.",
                    "log": "first",
                    "changes": [{"path": "demo/README.md", "new_content": "bad\n"}],
                    "actions": [],
                    "intent": {"kind": "command", "should_write_files": True},
                    "trace": {
                        "verification": [
                            {"name": "valid-change-paths", "ok": False, "detail": "Invalid path.", "severity": "hard"}
                        ],
                        "warnings": [],
                    },
                }
                targeted = SimpleNamespace(
                    spoken="Verifier fixed without pipeline restart.",
                    log="targeted",
                    changes=[{"path": "demo/README.md", "new_content": "fixed\n"}],
                    actions=[],
                )

                events: list[tuple[str, dict]] = []
                with patch("api.main.run_agent_pipeline", return_value=first) as mocked_pipeline, \
                    patch("api.agent.suggest", return_value=targeted) as mocked_suggest, \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix readme", project_root="demo", auto_execute=True),
                        event_cb=lambda event, data: events.append((event, data)),
                    )

                self.assertEqual(mocked_pipeline.call_count, 1)
                mocked_suggest.assert_called_once()
                self.assertTrue(result["verifier_repair"]["targeted"])
                self.assertTrue(result["execution"]["ok"])
                self.assertEqual((project / "README.md").read_text(encoding="utf-8"), "fixed\n")
                self.assertTrue(any(data.get("targeted") for event, data in events if event == "status" and data.get("phase") == "verifier_repair"))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_streaming_shell_buffers_output_chunks_without_losing_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chunks: list[tuple[str, str]] = []
            result = main_mod._run_shell_command_streaming(
                "python3 -c \"for i in range(20): print('line-%02d' % i)\"",
                Path(tmp),
                lambda stream, chunk: chunks.append((stream, chunk)),
            )

        self.assertTrue(result["ok"])
        self.assertIn("line-00", result["stdout"])
        self.assertIn("line-19", result["stdout"])
        stdout_chunks = [chunk for stream, chunk in chunks if stream == "stdout"]
        self.assertTrue(stdout_chunks)
        self.assertLess(len(stdout_chunks), 20)
        self.assertIn("line-19", "".join(stdout_chunks))

    def test_quick_missing_package_repair_allows_react_router_dom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps({"dependencies": {"react": "^19.0.0"}}), encoding="utf-8")
            execution = {
                "validation": {
                    "results": [
                        {
                            "stdout": "src/App.tsx(1,31): error TS2307: Cannot find module 'react-router-dom' or its corresponding type declarations.\n",
                            "stderr": "",
                        }
                    ]
                }
            }

            packages = main_mod._missing_external_packages_from_execution(execution, project)

        self.assertIn("react-router-dom", packages)

    def test_quick_ts2322_repair_removes_unsupported_jsx_prop(self) -> None:
        session_id = "quick-ts2322-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src" / "pages").mkdir(parents=True)
                (project / "src" / "pages" / "Pricing.tsx").write_text(
                    "import Button from '../components/Button';\n"
                    "export default function Pricing() {\n"
                    "  return <Button variant=\"primary\" style={{ width: '100%' }}>Start</Button>;\n"
                    "}\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "stdout": (
                                    "src/pages/Pricing.tsx(3,36): error TS2322: Type '{ children: string; variant: \"primary\"; style: { width: string; }; }' "
                                    "is not assignable to type 'IntrinsicAttributes'.\n"
                                    "  Property 'style' does not exist on type 'IntrinsicAttributes'.\n"
                                ),
                                "stderr": "",
                                "ok": False,
                            }
                        ],
                    }
                }

                with patch("api.main._run_harness_shell_actions_internal", return_value={"ok": True, "results": []}):
                    result = main_mod._try_quick_ts2322_unsupported_prop_repair(
                        main_mod.AgentReq(input="fix", project_root="demo"),
                        execution,
                        lambda *_args: None,
                    )

                self.assertIsInstance(result, dict)
                self.assertTrue(result["ok"])
                self.assertEqual(result["changed_paths"], ["src/pages/Pricing.tsx"])
                repaired = (project / "src" / "pages" / "Pricing.tsx").read_text(encoding="utf-8")
                self.assertNotIn("style=", repaired)
                self.assertIn("<Button variant=\"primary\">Start</Button>", repaired)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_quick_ts2322_repair_adds_supported_card_presentation_props(self) -> None:
        session_id = "quick-ts2322-card-props-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                (project / "src" / "components" / "ui").mkdir(parents=True)
                (project / "src" / "pages").mkdir(parents=True)
                card = project / "src" / "components" / "ui" / "Card.tsx"
                card.write_text(
                    "import { ReactNode } from 'react';\n"
                    "\n"
                    "interface CardProps {\n"
                    "  children: ReactNode;\n"
                    "  className?: string;\n"
                    "}\n"
                    "\n"
                    "export default function Card({ children, className = '' }: CardProps) {\n"
                    "  return (\n"
                    "    <div className={`card ${className}`.trim()}>\n"
                    "      {children}\n"
                    "    </div>\n"
                    "  );\n"
                    "}\n",
                    encoding="utf-8",
                )
                page = project / "src" / "pages" / "Home.tsx"
                page.write_text(
                    "import Card from '../components/ui/Card';\n"
                    "export default function Home() {\n"
                    "  return <Card title=\"Velocity\" eyebrow=\"Metric\"><p>Ready</p></Card>;\n"
                    "}\n",
                    encoding="utf-8",
                )
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                execution = {
                    "validation": {
                        "commands": ["npm run build"],
                        "results": [
                            {
                                "stdout": (
                                    "src/pages/Home.tsx(3,16): error TS2322: Type '{ children: Element; title: string; eyebrow: string; }' "
                                    "is not assignable to type 'IntrinsicAttributes & CardProps'.\n"
                                    "  Property 'title' does not exist on type 'IntrinsicAttributes & CardProps'.\n"
                                    "src/pages/Home.tsx(3,33): error TS2322: Type '{ children: Element; title: string; eyebrow: string; }' "
                                    "is not assignable to type 'IntrinsicAttributes & CardProps'.\n"
                                    "  Property 'eyebrow' does not exist on type 'IntrinsicAttributes & CardProps'.\n"
                                ),
                                "stderr": "",
                                "ok": False,
                            }
                        ],
                    }
                }

                with patch("api.main._run_harness_shell_actions_internal", return_value={"ok": True, "results": []}):
                    result = main_mod._try_quick_ts2322_unsupported_prop_repair(
                        main_mod.AgentReq(input="fix", project_root="demo"),
                        execution,
                        lambda *_args: None,
                    )

                self.assertIsInstance(result, dict)
                self.assertTrue(result["ok"])
                self.assertEqual(result["changed_paths"], ["src/components/ui/Card.tsx"])
                repaired_card = card.read_text(encoding="utf-8")
                repaired_page = page.read_text(encoding="utf-8")
                self.assertIn("title?: string", repaired_card)
                self.assertIn("eyebrow?: string", repaired_card)
                self.assertIn("{title ? <h2", repaired_card)
                self.assertIn("{eyebrow ? <div", repaired_card)
                self.assertIn("title=\"Velocity\"", repaired_page)
                self.assertIn("eyebrow=\"Metric\"", repaired_page)
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_records_preview_audit_step_when_preview_url_is_available(self) -> None:
        session_id = "auto-execute-preview-audit-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "src").mkdir()
                (project / "src" / "App.tsx").write_text("old\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                pipeline = {
                    "spoken": "Aku apply patch dan audit preview.",
                    "log": "fake",
                    "changes": [{"path": "demo/src/App.tsx", "new_content": "agent edit\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                audit_result = {
                    "ok": True,
                    "preview_url": "http://127.0.0.1:4173",
                    "audit_mode": "html",
                    "issue_details": [],
                    "issues": [],
                    "summary": "mode=html; blocking=0; warnings=0",
                }

                with patch("api.main.run_agent_pipeline", return_value=pipeline), \
                    patch("api.main.preview_audit", return_value=audit_result), \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="patch and audit", project_root="demo", preview_url="http://127.0.0.1:4173", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                self.assertTrue(result["execution"]["ok"])
                self.assertEqual(result["execution"]["preview_audit"]["summary"], "mode=html; blocking=0; warnings=0")
                self.assertIn("preview_audit", [step["kind"] for step in result["execution"]["steps"]])
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_can_start_preview_before_preview_audit(self) -> None:
        session_id = "auto-execute-start-preview-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "index.html").write_text("<h1>Old</h1>", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                pipeline = {
                    "spoken": "Aku apply patch, start preview, dan audit.",
                    "log": "fake",
                    "changes": [{"path": "demo/index.html", "new_content": "<title>Smoke</title><h1>Smoke</h1><p>Appora preview smoke has enough words for the audit check.</p>"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                audit_calls: list[str] = []

                def fake_preview_audit(req):
                    audit_calls.append(req.preview_url)
                    return {
                        "ok": True,
                        "preview_url": req.preview_url,
                        "audit_mode": "html",
                        "issue_details": [],
                        "issues": [],
                        "summary": "mode=html; blocking=0; warnings=0",
                    }

                with patch("api.main.run_agent_pipeline", return_value=pipeline), \
                    patch("api.main.run_start", return_value={"ok": True, "id": "run-1", "url": "http://localhost:4321", "direct_url": "http://localhost:4321", "project_root": "demo"}) as mocked_start, \
                    patch("api.main.preview_audit", side_effect=fake_preview_audit), \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="patch and auto preview audit", project_root="demo", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                mocked_start.assert_called_once()
                self.assertEqual(audit_calls, ["http://localhost:4321"])
                self.assertTrue(result["execution"]["ok"])
                self.assertEqual(result["execution"]["preview_audit"]["started_preview"]["id"], "run-1")
                self.assertIn("preview_audit", [step["kind"] for step in result["execution"]["steps"]])
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_skips_preview_start_for_non_frontend_change(self) -> None:
        session_id = "auto-execute-skip-preview-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                pipeline = {
                    "spoken": "Aku buat markdown.",
                    "log": "fake",
                    "changes": [{"path": "demo/NOTE.md", "new_content": "hello\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }

                with patch("api.main.run_agent_pipeline", return_value=pipeline), \
                    patch("api.main.run_start") as mocked_start, \
                    patch("api.main.preview_audit") as mocked_audit, \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="buat markdown", project_root="demo", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                mocked_start.assert_not_called()
                mocked_audit.assert_not_called()
                self.assertTrue(result["execution"]["ok"])
                self.assertIsNone(result["execution"]["preview_audit"])
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_runs_one_repair_pass_after_validation_failure(self) -> None:
        session_id = "auto-execute-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "bad.py").write_text("print(\n", encoding="utf-8")
                (project / "helper.py").write_text("value =\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                first = {
                    "spoken": "Aku coba apply tapi validasi gagal.",
                    "log": "first",
                    "changes": [{"path": "demo/bad.py", "new_content": "print(\n"}],
                    "actions": [{"type": "shell", "command": "python3 -m compileall helper.py", "cwd": "demo", "reason": "reproduce helper syntax"}],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                repair = {
                    "spoken": "Aku perbaiki syntax error.",
                    "log": "repair",
                    "changes": [
                        {"path": "demo/bad.py", "new_content": "print('ok')\n"},
                        {"path": "demo/helper.py", "new_content": "value = 1\n"},
                    ],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }

                with patch("api.main.run_agent_pipeline", side_effect=[first, repair]) as mocked_pipeline, \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix python syntax", project_root="demo", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                self.assertEqual(mocked_pipeline.call_count, 2)
                repair_req = mocked_pipeline.call_args_list[1].args[0]
                self.assertIn("Current file context after failed execution", repair_req.input)
                self.assertIn('"path": "demo/bad.py"', repair_req.input)
                self.assertIn('"path": "demo/helper.py"', repair_req.input)
                self.assertIn("print(", repair_req.input)
                self.assertIn("value =", repair_req.input)
                self.assertIn("Repair replay plan", repair_req.input)
                self.assertIn('"command": "python3 -m compileall helper.py"', repair_req.input)
                self.assertIn("include shell actions for non-validation replay commands", repair_req.input)
                self.assertTrue(result["execution"]["validation"]["ok"])
                self.assertEqual(len(result["execution"]["repairs"]), 1)
                repair_execution = result["execution"]["repairs"][0]["execution"]
                self.assertTrue(repair_execution["ok"])
                self.assertTrue(repair_execution["validation"]["ok"])
                self.assertTrue(repair_execution["replay"]["ok"])
                self.assertEqual(repair_execution["replay"]["results"][0]["command"], "python3 -m compileall helper.py")
                self.assertIn("replay", [step["kind"] for step in repair_execution["steps"]])
                self.assertIn("repair", [step["kind"] for step in result["execution"]["steps"]])
                self.assertEqual(result["execution"]["failure_analysis"]["failure_count"], 0)
                self.assertGreater(result["execution"]["failure_analysis"]["resolved_failure_count"], 0)
                self.assertEqual(result["execution"]["failure_analysis"]["resolved_by"], "repair-loop")
                self.assertIn("Resolved", result["execution"]["failure_analysis"]["summary"])
                self.assertEqual(result["execution"]["completion_report"]["state"], "complete")
                self.assertIn("completion", [step["kind"] for step in result["execution"]["steps"]])
                validation_criteria = [
                    item for item in result["execution"]["completion_report"]["criteria"]
                    if item["label"] == "validation"
                ]
                self.assertEqual(validation_criteria[0]["status"], "passed")
                self.assertEqual((project / "bad.py").read_text(encoding="utf-8"), "print('ok')\n")
                self.assertEqual((project / "helper.py").read_text(encoding="utf-8"), "value = 1\n")
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_can_run_multiple_repair_passes_until_valid(self) -> None:
        session_id = "auto-execute-multi-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "bad.py").write_text("print(\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                first = {
                    "spoken": "Aku coba apply tapi validasi gagal.",
                    "log": "first",
                    "changes": [{"path": "demo/bad.py", "new_content": "print(\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                bad_repair = {
                    "spoken": "Aku coba repair pertama.",
                    "log": "repair-one",
                    "changes": [{"path": "demo/bad.py", "new_content": "print('still bad'\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                good_repair = {
                    "spoken": "Aku repair lagi sampai valid.",
                    "log": "repair-two",
                    "changes": [{"path": "demo/bad.py", "new_content": "print('ok')\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }

                with patch("api.main.run_agent_pipeline", side_effect=[first, bad_repair, good_repair]) as mocked_pipeline, \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix python syntax fully", project_root="demo", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                self.assertEqual(mocked_pipeline.call_count, 3)
                self.assertEqual(len(result["execution"]["repairs"]), 2)
                self.assertFalse(result["execution"]["repairs"][0]["execution"]["ok"])
                self.assertTrue(result["execution"]["repairs"][1]["execution"]["ok"])
                self.assertTrue(result["execution"]["ok"])
                self.assertEqual(result["execution"]["completion_report"]["state"], "complete")
                self.assertTrue(result["execution"]["completion_report"]["criteria"])
                repair_steps = [step for step in result["execution"]["steps"] if step["kind"] == "repair"]
                self.assertEqual([step["repair_index"] for step in repair_steps], [1, 2])
                completion_steps = [step for step in result["execution"]["steps"] if step["kind"] == "completion"]
                self.assertTrue(completion_steps)
                self.assertEqual(completion_steps[-1]["state"], "complete")
                self.assertEqual((project / "bad.py").read_text(encoding="utf-8"), "print('ok')\n")
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_repair_prompt_marks_repeated_failure_signature(self) -> None:
        session_id = "auto-execute-repeated-repair-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "bad.py").write_text("print(\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                first = {
                    "spoken": "Aku coba apply tapi validasi gagal.",
                    "log": "first",
                    "changes": [{"path": "demo/bad.py", "new_content": "print(\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                bad_repair_one = {
                    "spoken": "Aku coba repair pertama.",
                    "log": "repair-one",
                    "changes": [{"path": "demo/bad.py", "new_content": "print('still bad'\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                bad_repair_two = {
                    "spoken": "Aku coba repair kedua.",
                    "log": "repair-two",
                    "changes": [{"path": "demo/bad.py", "new_content": "print('still bad again'\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                good_repair = {
                    "spoken": "Aku ganti strategi dan valid.",
                    "log": "repair-three",
                    "changes": [{"path": "demo/bad.py", "new_content": "print('ok')\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }

                with patch("api.main.run_agent_pipeline", side_effect=[first, bad_repair_one, bad_repair_two, good_repair]) as mocked_pipeline, \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix python syntax without looping", project_root="demo", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                self.assertEqual(mocked_pipeline.call_count, 4)
                second_repair_req = mocked_pipeline.call_args_list[2].args[0]
                self.assertIn('"repeated_failure": true', second_repair_req.input)
                self.assertIn('"suggested_next_move"', second_repair_req.input)
                self.assertEqual(len(result["execution"]["repairs"]), 3)
                self.assertTrue(result["execution"]["repairs"][0]["execution"]["failure_analysis"]["current_signature"])
                self.assertTrue(result["execution"]["repairs"][1]["execution"]["failure_analysis"]["current_signature"])
                self.assertIn("validation failed", result["execution"]["repairs"][1]["execution"]["failure_analysis"]["summary"])
                self.assertIn("Change strategy", result["execution"]["repairs"][1]["pre_repair_failure_analysis"]["suggested_next_move"])
                repair_steps = [step for step in result["execution"]["steps"] if step["kind"] == "repair"]
                self.assertTrue(any(step.get("repeated_failure") for step in repair_steps))
                completion_steps = [step for step in result["execution"]["steps"] if step["kind"] == "completion"]
                self.assertTrue(completion_steps)
                self.assertEqual(completion_steps[-1]["state"], "complete")
                self.assertTrue(result["execution"]["ok"])
                self.assertEqual((project / "bad.py").read_text(encoding="utf-8"), "print('ok')\n")
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)

    def test_backend_auto_execute_marks_repair_stop_when_budget_exhausted(self) -> None:
        session_id = "auto-execute-repair-stop-test"
        STATE.get("sessions", {}).pop(session_id, None)
        session_token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "demo"
                project.mkdir()
                (project / "bad.py").write_text("print(\n", encoding="utf-8")
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }
                first = {
                    "spoken": "Aku coba apply tapi validasi gagal.",
                    "log": "first",
                    "changes": [{"path": "demo/bad.py", "new_content": "print(\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }
                bad_repair = {
                    "spoken": "Aku coba repair tapi masih gagal.",
                    "log": "bad-repair",
                    "changes": [{"path": "demo/bad.py", "new_content": "print('still bad'\n"}],
                    "actions": [],
                    "intent": {"kind": "command"},
                    "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "verification": [], "warnings": []},
                }

                events: list[tuple[str, dict]] = []
                with patch("api.main.run_agent_pipeline", side_effect=[first, bad_repair, bad_repair, bad_repair]) as mocked_pipeline, \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="fix python syntax but stop clearly", project_root="demo", auto_execute=True),
                        event_cb=lambda event, data: events.append((event, data)),
                    )

                execution = result["execution"]
                self.assertEqual(mocked_pipeline.call_count, 4)
                self.assertFalse(execution["ok"])
                self.assertEqual(len(execution["repairs"]), 3)
                self.assertEqual(execution["repair_stop"]["reason"], "max_repair_passes_exhausted")
                self.assertEqual(execution["repair_stop"]["attempts"], 3)
                self.assertEqual(execution["repair_stop"]["max_repair_passes"], 3)
                self.assertIn("Read the failing validation output", execution["repair_stop"]["next_action"])
                self.assertIn("repair_stop", [step["kind"] for step in execution["steps"]])
                self.assertIn("blocked", [item["phase"] for item in execution["run_ledger"]])
                self.assertTrue(any(item.get("next_action") for item in execution["run_ledger"] if item["phase"] == "blocked"))
                self.assertEqual(execution["completion_report"]["state"], "blocked")
                criteria = {item["label"]: item for item in execution["completion_report"]["criteria"]}
                self.assertEqual(criteria["repair-budget"]["status"], "failed")
                self.assertTrue(any("Backend repair stopped" in item for item in execution["completion_report"]["residual_risks"]))
                tool_outputs = [data for event, data in events if event == "tool_output"]
                self.assertTrue(any(item.get("tool") == "repair" and item.get("repair_index") == 1 for item in tool_outputs))
                self.assertTrue(any(item.get("tool") == "repair-stop" and item.get("phase") == "repair_stop" for item in tool_outputs))
                self.assertTrue(any(item.get("tool") == "completion" and item.get("state") == "blocked" for item in tool_outputs))
                status_phases = [data.get("phase") for event, data in events if event == "status"]
                self.assertIn("repair_stop", status_phases)
                self.assertIn("completion", status_phases)
                done_events = [data for event, data in events if event == "done"]
                self.assertTrue(done_events)
                self.assertIn("Blocked", done_events[-1].get("message", ""))
                self.assertNotIn("Beres", done_events[-1].get("message", ""))
        finally:
            CURRENT_SESSION_ID.reset(session_token)
            STATE.get("sessions", {}).pop(session_id, None)


class CapabilityHonestyRegressionTests(unittest.TestCase):
    def test_capabilities_surface_supabase_readiness_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)
            (project_dir / "package.json").write_text(json.dumps({"name": "demo", "dependencies": {}}), encoding="utf-8")

            with patch("api.main._ws", return_value=ws_root), \
                patch("api.main.has_supabase", return_value=True), \
                patch("api.main.get_agent_memory_chunks_table_status", return_value="missing"), \
                patch("api.main._browser_preview_audit_ready", return_value=False), \
                patch("api.main._resolve_node_binary", return_value=None), \
                patch("api.main.discover_mcp_servers", return_value=[]):
                caps = agent_capabilities(project_root="demo", include_live_tools=False)

        self.assertTrue(caps["supports"]["supabase_memory_backend"])
        self.assertFalse(caps["supports"]["supabase_rag_ready"])
        self.assertEqual(caps["memory"]["retrieval_backend"], "local-hash-vector-chunks")
        self.assertEqual(caps["memory"]["supabase_rag_status"], "missing")
        self.assertIn("agent_memory_chunks", caps["memory"]["supabase_warning"])
        self.assertTrue(caps["supports"]["vector_memory_retrieval"])
        self.assertTrue(caps["supports"]["preview_quality_checks"])
        self.assertTrue(caps["supports"]["repo_symbol_tools"])
        self.assertTrue(caps["supports"]["route_analysis_tool"])
        self.assertTrue(caps["supports"]["quality_scan_tool"])
        self.assertIn("mcp", caps["supports"]["tool_actions"])
        self.assertIn("shell", caps["supports"]["tool_actions"])
        self.assertIn("tool", caps["supports"]["tool_actions"])
        self.assertIn("component_index", caps["boundaries"]["local_tool_names"])
        self.assertIn("route_map", caps["boundaries"]["local_tool_names"])
        self.assertIn("quality_scan", caps["boundaries"]["local_tool_names"])
        self.assertIn("skill_catalog", caps["boundaries"]["local_tool_names"])
        self.assertIn("skill_read", caps["boundaries"]["local_tool_names"])


class SupabaseReadinessRegressionTests(unittest.TestCase):
    def test_status_reports_missing_table_as_not_live_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            project_dir = ws_root / "demo"
            project_dir.mkdir(parents=True)

            with patch("api.main._ws", return_value=ws_root), \
                patch("api.main.has_supabase", return_value=True), \
                patch("api.main.get_agent_memory_chunks_table_status", return_value="missing"), \
                patch("api.main.get_agent_memory_chunks_summary", return_value=None):
                status = supabase_rag_status(project_root="demo")

        self.assertFalse(status["live_ready"])
        self.assertEqual(status["table_status"], "missing")
        self.assertIn("agent_memory_chunks", status["warning"])

    def test_settings_detect_frontend_supabase_even_without_service_role(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VITE_SUPABASE_URL": "https://demo.supabase.co",
                "VITE_SUPABASE_ANON_KEY": "anon-demo-key",
            },
            clear=True,
        ), patch("api.settings.load_env", return_value=None), patch("api.settings.dotenv_values", return_value={}):
            settings = load_settings()

        self.assertEqual(settings.supabase_url, "https://demo.supabase.co")
        self.assertTrue(settings.supabase_frontend_ready)
        self.assertFalse(settings.supabase_enabled)
        self.assertTrue(settings.supabase_anon_key_set)
        self.assertTrue(settings.supabase_missing_env)
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", settings.supabase_missing_env)


class ProviderCatalogRegressionTests(unittest.TestCase):
    def test_nine_router_catalog_contains_gateway_combos_and_aliases(self) -> None:
        models = list_provider_models("nine_router")
        catalog = provider_catalog()

        self.assertEqual(models[0], "free-forever")
        self.assertEqual(catalog["nine_router"]["recommended_model"], models[0])
        for model in [
            "always-on",
            "maximize-claude",
            "openclaw-free",
            "coding-auto",
            "cheap-auto",
            "quality-auto",
            "kr/claude-sonnet-4.5",
            "oc/<auto>",
            "cx/gpt-5.4",
            "cc/claude-opus-4-7",
            "gh/claude-sonnet-4.6",
            "cu/gpt-5.3-codex",
            "glm/glm-5",
            "minimax/minimax-m2.7",
            "kimi/kimi-k2.6",
            "openrouter/openrouter/free",
            "deepseek/deepseek-v4-pro",
        ]:
            self.assertIn(model, models)

    def test_agent_runtime_only_considers_nine_router_connected(self) -> None:
        snapshot = {
            "nine_router": {"connected": True},
            "openai": {"connected": True},
            "openrouter": {"connected": True},
            "gemini": {"connected": True},
            "groq": {"connected": False},
        }
        with patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0):
            order = agent_mod._fallback_provider_order("openai")

        self.assertEqual(order, ["nine_router"])

    def test_generate_json_uses_only_nine_router_model(self) -> None:
        snapshot = {
            "nine_router": {"connected": True},
            "openai": {"connected": True},
            "openrouter": {"connected": True},
        }
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "openai"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "free-forever"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "nine_router")
        self.assertEqual(model, "free-forever")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(attempted, [("nine_router", "free-forever")])

    def test_generate_json_prioritizes_working_nine_router_models_for_appora_alias(self) -> None:
        snapshot = {"nine_router": {"connected": True}}
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "nine_router"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "appora"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "nine_router")
        self.assertEqual(model, "gemini/gemini-3.1-flash-lite-preview")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(attempted, [("nine_router", "gemini/gemini-3.1-flash-lite-preview")])
        self.assertNotIn(("nine_router", "appora"), attempted)

    def test_generate_json_does_not_send_appora_alias_as_router_model(self) -> None:
        snapshot = {"nine_router": {"connected": True}}
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            raise RuntimeError("No active credentials for provider: openai")

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "nine_router"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "appora"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            with self.assertRaises(RuntimeError):
                agent_mod._generate_json(system="system", user="user")

        self.assertNotIn(("nine_router", "appora"), attempted)
        self.assertIn(("nine_router", "gemini/gemini-3.1-flash-lite-preview"), attempted)

    def test_generate_json_falls_back_through_nine_router_priority_models(self) -> None:
        snapshot = {"nine_router": {"connected": True}}
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            if model == "gemini/gemini-3.1-flash-lite-preview":
                raise RuntimeError("timed out")
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "nine_router"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "appora"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "nine_router")
        self.assertEqual(model, "qd/qmodel_latest")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(
            attempted,
            [
                ("nine_router", "gemini/gemini-3.1-flash-lite-preview"),
                ("nine_router", "qd/qmodel_latest"),
            ],
        )
        fallback = data.get("_voiceide_provider_fallback")
        self.assertEqual(fallback["selected_provider"], "nine_router")
        self.assertEqual(fallback["used_provider"], "nine_router")
        self.assertEqual(fallback["selected_model"], "appora")
        self.assertEqual(fallback["used_model"], "qd/qmodel_latest")
        self.assertIn("gemini/gemini-3.1-flash-lite-preview", fallback["skipped"][0])

    def test_generate_json_falls_back_after_non_json_model_output(self) -> None:
        snapshot = {"nine_router": {"connected": True}}
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            if model == "gemini/gemini-3.1-flash-lite-preview":
                raise RuntimeError("LLM did not return valid JSON: explanation text")
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "nine_router"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "appora"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            _provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(model, "qd/qmodel_latest")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(len(attempted), 2)

    def test_generate_json_can_skip_low_priority_models_after_no_work(self) -> None:
        snapshot = {"nine_router": {"connected": True}}
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "nine_router"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "appora"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            _provider, model, data = agent_mod._generate_json(system="system", user="user", model_skip_count=1)

        self.assertEqual(model, "qd/qmodel_latest")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(attempted, [("nine_router", "qd/qmodel_latest")])

    def test_generate_json_tries_explicit_nine_router_model_before_priority_fallbacks(self) -> None:
        snapshot = {"nine_router": {"connected": True}}
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "nine_router"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "nvidia/deepseek-ai/deepseek-v4-flash"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "nine_router")
        self.assertEqual(model, "nvidia/deepseek-ai/deepseek-v4-flash")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(attempted, [("nine_router", "nvidia/deepseek-ai/deepseek-v4-flash")])

    def test_route_plan_treats_9router_aliases_as_pass_through(self) -> None:
        from api.agent_router import build_route_plan

        plan = build_route_plan(
            route_name="free-forever",
            selected_provider="nine_router",
            connected_providers={"nine_router"},
            cooldown_remaining=lambda _provider: 0,
        )

        self.assertTrue(any(attempt.provider == "nine_router" and attempt.model == "kr/claude-sonnet-4.5" for attempt in plan.attempts))
        self.assertFalse(plan.skipped)

    def test_direct_model_attempt_accepts_subscription_alias(self) -> None:
        from api.agent_router import build_direct_model_attempt

        attempt, reason = build_direct_model_attempt("kr/claude-sonnet-4.5", selected_provider="nine_router")

        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.provider if attempt else "", "nine_router")
        self.assertEqual(attempt.model if attempt else "", "kr/claude-sonnet-4.5")
        self.assertIsNone(reason)

    def test_generate_json_defaults_to_nine_router_when_none_selected(self) -> None:
        snapshot = {
            "nine_router": {"connected": True},
            "openrouter": {"connected": True},
            "gemini": {"connected": False},
        }

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", None), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "free-forever"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", return_value={"spoken": "ok", "changes": [], "actions": []}):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "nine_router")
        self.assertEqual(model, "free-forever")
        self.assertEqual(data["spoken"], "ok")

    def test_generate_json_uses_hosted_nine_router_model_preference(self) -> None:
        snapshot = {
            "nine_router": {"connected": True},
            "openrouter": {"connected": True},
            "gemini": {"connected": True},
        }

        with patch("api.agent.get_user_preferences", return_value=UserPreferencesRecord(profile_id="sb-user-123", llm_provider="gemini", nine_router_model="kr/claude-sonnet-4.5", gemini_model="gemini-3-flash-preview")), \
            patch.object(agent_mod.settings_mod.settings, "llm_provider", "openrouter"), \
            patch.object(agent_mod.settings_mod.settings, "nine_router_model", "free-forever"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", return_value={"spoken": "ok", "changes": [], "actions": []}):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                provider, model, data = agent_mod._generate_json(system="system", user="user")
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertEqual(provider, "nine_router")
        self.assertEqual(model, "kr/claude-sonnet-4.5")

    def test_nine_router_status_reports_managed_free_router(self) -> None:
        from api import oauth_runtime

        isolated_settings = SimpleNamespace(nine_router_base_url="http://127.0.0.1:20128/v1", nine_router_api_key="", nine_router_api_key_set=False)
        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
            },
            clear=True,
        ), patch("api.settings.settings", isolated_settings), \
            patch("api.oauth_runtime.has_provider_secret", return_value=False):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                status = oauth_runtime.nine_router_status()
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertTrue(status["connected"])
        self.assertEqual(status["source"], "appora_managed_free")
        self.assertEqual(status["auth_type"], "managed_free")
        self.assertTrue(status["managed_free"])
        self.assertEqual(status["base_url"], "https://router.appora.ai/v1")

    def test_managed_free_router_is_used_for_free_forever_without_user_key(self) -> None:
        from api import oauth_runtime

        calls: list[tuple[str, str]] = []

        def fake_post(url, payload, headers, *, provider=None):
            calls.append((url, headers.get("Authorization", "")))
            return 200, {"choices": [{"message": {"content": "{\"spoken\":\"ok\",\"changes\":[],\"actions\":[]}"}}]}, ""

        isolated_settings = SimpleNamespace(nine_router_base_url="http://127.0.0.1:20128/v1", nine_router_api_key="", nine_router_api_key_set=False)
        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_FREE_DAILY_MESSAGES": "1000",
            },
            clear=True,
        ), patch("api.settings.settings", isolated_settings), \
            patch("api.oauth_runtime.has_provider_secret", return_value=False), \
            patch("api.oauth_runtime._post_json", side_effect=fake_post):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                result = oauth_runtime.nine_router_generate_json(model="free-forever", system="system", user="user")
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertIn("\"spoken\":\"ok\"", result["text"])
        self.assertEqual(calls, [("https://router.appora.ai/v1/chat/completions", "Bearer managed-key")])

    def test_managed_free_router_is_used_for_free_provider_aliases_without_user_key(self) -> None:
        from api import oauth_runtime

        calls: list[tuple[str, str, str]] = []

        def fake_post(url, payload, headers, *, provider=None):
            calls.append((url, str(payload.get("model")), headers.get("Authorization", "")))
            return 200, {"choices": [{"message": {"content": "{\"spoken\":\"ok\",\"changes\":[],\"actions\":[]}"}}]}, ""

        isolated_settings = SimpleNamespace(nine_router_base_url="http://127.0.0.1:20128/v1", nine_router_api_key="", nine_router_api_key_set=False)
        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_FREE_DAILY_MESSAGES": "1000",
            },
            clear=True,
        ), patch("api.settings.settings", isolated_settings), \
            patch("api.oauth_runtime.has_provider_secret", return_value=False), \
            patch("api.oauth_runtime._post_json", side_effect=fake_post):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                result = oauth_runtime.nine_router_generate_json(model="kr/claude-sonnet-4.5", system="system", user="user")
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertIn("\"spoken\":\"ok\"", result["text"])
        self.assertEqual(calls, [("https://router.appora.ai/v1/chat/completions", "kr/claude-sonnet-4.5", "Bearer managed-key")])

    def test_managed_free_router_can_resolve_combo_to_configured_free_route(self) -> None:
        from api import oauth_runtime

        calls: list[str] = []

        def fake_post(url, payload, headers, *, provider=None):
            calls.append(str(payload.get("model")))
            return 200, {"choices": [{"message": {"content": "{\"spoken\":\"ok\",\"changes\":[],\"actions\":[]}"}}]}, ""

        isolated_settings = SimpleNamespace(nine_router_base_url="http://127.0.0.1:20128/v1", nine_router_api_key="", nine_router_api_key_set=False)
        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_MANAGED_FREE_MODEL": "kr/claude-sonnet-4.5",
                "APPORA_FREE_DAILY_MESSAGES": "3",
            },
            clear=True,
        ), patch("api.settings.settings", isolated_settings), \
            patch("api.oauth_runtime.has_provider_secret", return_value=False), \
            patch("api.oauth_runtime._post_json", side_effect=fake_post):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                result = oauth_runtime.nine_router_generate_json(model="free-forever", system="system", user="user")
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertIn("\"spoken\":\"ok\"", result["text"])
        self.assertEqual(calls, ["kr/claude-sonnet-4.5"])

    def test_streaming_spoken_extractor_preserves_incremental_spaces(self) -> None:
        chunks: list[str] = []
        extractor = agent_mod._StreamingSpokenExtractor(chunks.append)

        for part in ['{"spoken":"Halo', " ", "bro", '","changes":[]}', " trailing"]:
            extractor.feed(part)

        self.assertEqual(chunks, ["Halo", " ", "bro"])
        self.assertEqual("".join(chunks), "Halo bro")

    def test_nine_router_post_json_accepts_sse_chunk_response(self) -> None:
        from api import oauth_runtime

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'data: {"choices":[{"delta":{"content":"{\\"spoken\\":\\"OK"}}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"\\",\\"changes\\":[]}"}}]}\n\n'
                    b"data: [DONE]\n\n"
                )

        with patch("api.oauth_runtime.urlopen", return_value=FakeResponse()):
            status, data, raw = oauth_runtime._post_json(
                "https://router.test/v1/chat/completions",
                {"model": "ollama/gpt-oss:120b", "messages": [], "stream": False},
                {"Authorization": "Bearer test"},
                provider="nine_router",
            )

        self.assertEqual(status, 200)
        self.assertIn("data:", raw)
        self.assertEqual(data, {"choices": [{"message": {"content": '{"spoken":"OK","changes":[]}'}}]})

    def test_nine_router_post_json_accepts_prefixed_json_response(self) -> None:
        from api import oauth_runtime

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'   {"choices":[{"message":{"content":"{\\"spoken\\":\\"OK\\",\\"changes\\":[]}"}}]}'

        with patch("api.oauth_runtime.urlopen", return_value=FakeResponse()):
            status, data, _raw = oauth_runtime._post_json(
                "https://router.test/v1/chat/completions",
                {"model": "openrouter/google/gemma-4-31b-it:free", "messages": [], "stream": False},
                {"Authorization": "Bearer test"},
                provider="nine_router",
            )

        self.assertEqual(status, 200)
        self.assertEqual((data or {}).get("choices", [])[0]["message"]["content"], '{"spoken":"OK","changes":[]}')

    def test_nine_router_rate_limit_does_not_global_cooldown_all_routes(self) -> None:
        from api import oauth_runtime
        from urllib.error import HTTPError

        oauth_runtime._PROVIDER_COOLDOWN_UNTIL.pop("nine_router", None)

        def fake_urlopen(_req, timeout=180):
            raise HTTPError(
                "https://router.test/v1/chat/completions",
                429,
                "Too Many Requests",
                {},
                None,
            )

        with patch("api.oauth_runtime.urlopen", side_effect=fake_urlopen):
            status, _data, _raw = oauth_runtime._post_json(
                "https://router.test/v1/chat/completions",
                {"model": "openrouter/openrouter/free", "messages": [], "stream": False},
                {"Authorization": "Bearer test"},
                provider="nine_router",
            )

        self.assertEqual(status, 429)
        self.assertEqual(oauth_runtime.get_provider_cooldown_remaining("nine_router"), 0)

    def test_nine_router_generate_json_streams_sse_content(self) -> None:
        from api import oauth_runtime

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def __iter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"content":"{\\"spoken\\":\\"Halo"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":" "}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"bro\\",\\"changes\\":[]}"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ])

        streamed: list[str] = []
        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_FREE_DAILY_MESSAGES": "1000",
            },
            clear=False,
        ), patch("api.oauth_runtime.has_provider_secret", return_value=False), \
            patch("api.oauth_runtime.urlopen", return_value=FakeResponse()):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                result = oauth_runtime.nine_router_generate_json(
                    model="free-forever",
                    system="system",
                    user="user",
                    on_text_delta=streamed.append,
                )
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertEqual(streamed, ['{"spoken":"Halo', " ", 'bro","changes":[]}'])
        self.assertEqual(result["text"], '{"spoken":"Halo bro","changes":[]}')

    def test_managed_free_router_does_not_unlock_premium_alias_without_user_key(self) -> None:
        from api import oauth_runtime

        isolated_settings = SimpleNamespace(nine_router_base_url="http://127.0.0.1:20128/v1", nine_router_api_key="", nine_router_api_key_set=False)
        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
            },
            clear=True,
        ), patch("api.settings.settings", isolated_settings), \
            patch("api.oauth_runtime.has_provider_secret", return_value=False):
            result = oauth_runtime.nine_router_generate_json(model="cx/gpt-5.4", system="system", user="user")

        self.assertEqual(result["text"], "")
        self.assertIn("9Router", result["error_message"])

    def test_provider_key_prefers_decryptable_hosted_secret_over_env(self) -> None:
        from api import oauth_runtime

        with patch("api.oauth_runtime.has_provider_secret", return_value=True), \
            patch("api.oauth_runtime.get_provider_secret", return_value="sk-user"), \
            patch("api.oauth_runtime.os.getenv", return_value="sk-env"):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                key = oauth_runtime._provider_key_from_env_or_secret("openrouter")
                status = oauth_runtime.openrouter_status()
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertEqual(key, "sk-user")
        self.assertTrue(status["connected"])
        self.assertEqual(status["source"], "hosted_secret")

    def test_provider_status_reports_unreadable_hosted_secret_without_env_fallback(self) -> None:
        from api import oauth_runtime

        with patch("api.oauth_runtime.has_provider_secret", return_value=True), \
            patch("api.oauth_runtime.get_provider_secret", return_value=None), \
            patch("api.oauth_runtime.os.getenv", return_value="sk-env"):
            token = CURRENT_PROFILE_ID.set("sb-user-123")
            try:
                key = oauth_runtime._provider_key_from_env_or_secret("openrouter")
                status = oauth_runtime.openrouter_status()
            finally:
                CURRENT_PROFILE_ID.reset(token)

        self.assertEqual(key, "")
        self.assertFalse(status["connected"])
        self.assertEqual(status["source"], "hosted_secret_unreadable")
        self.assertIn("tidak bisa decrypt", status["hint"])


class WorkspaceBoundaryRegressionTests(unittest.TestCase):
    def test_safe_join_rejects_prefix_sibling_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            sibling = base / "workspace-evil"
            root.mkdir()
            sibling.mkdir()

            with self.assertRaises(ValueError):
                safe_join(root, "../workspace-evil/secret.txt")

    def test_hosted_sensitive_routes_require_verified_user(self) -> None:
        with patch("api.main.has_supabase", return_value=True), \
            patch("api.main._is_serverless_runtime", return_value=True):
            requires_verified_user = main_mod._requires_verified_hosted_user("/api/fs/list")

        self.assertTrue(requires_verified_user)

    def test_local_sensitive_routes_allow_header_fallback_even_with_supabase_env(self) -> None:
        with patch("api.main.has_supabase", return_value=True), \
            patch("api.main._is_serverless_runtime", return_value=False):
            requires_verified_user = main_mod._requires_verified_hosted_user("/api/fs/list")

        self.assertFalse(requires_verified_user)


class PreviewRunnerRegressionTests(unittest.TestCase):
    def test_preview_runner_uses_preview_script_without_installing_preview_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            project.mkdir(parents=True)
            (project / "package.json").write_text(
                json.dumps({"scripts": {"preview": "vite preview"}, "dependencies": {"vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="installed\n", stderr="")

            def fake_popen(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(pid=12345, stdout=["ready\n"])

            with patch("api.main._ws", return_value=root), \
                patch("api.main._hydrate_hosted_project", return_value=None), \
                patch("api.main._ensure_runner_capacity", return_value=None), \
                patch("api.main._resolve_package_manager", return_value=("npm", ["npm"])), \
                patch("api.main._next_port", return_value=4321), \
                patch("api.main._is_serverless_runtime", return_value=False), \
                patch("subprocess.run", side_effect=fake_run), \
                patch("subprocess.Popen", side_effect=fake_popen):
                result = main_mod.run_start(main_mod.RunStartReq(project_root="demo"), Request({"type": "http", "method": "POST", "path": "/api/run/start", "headers": []}))

        self.assertTrue(result["ok"])
        self.assertIn(["npm", "install"], commands)
        self.assertIn(["npm", "run", "preview", "--", "--host", "127.0.0.1", "--strictPort", "--port", "4321"], commands)
        self.assertNotIn(["npm", "install", "preview"], commands)

    def test_preview_runner_prefers_dev_script_for_live_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            project.mkdir(parents=True)
            (project / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite", "preview": "vite preview"}, "dependencies": {"vite": "^7.0.0"}}),
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_popen(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(pid=12345, stdout=["ready\n"])

            with patch("api.main._ws", return_value=root), \
                patch("api.main._hydrate_hosted_project", return_value=None), \
                patch("api.main._ensure_runner_capacity", return_value=None), \
                patch("api.main._resolve_package_manager", return_value=("npm", ["npm"])), \
                patch("api.main._next_port", return_value=4322), \
                patch("api.main._is_serverless_runtime", return_value=False), \
                patch("subprocess.run", side_effect=fake_run), \
                patch("subprocess.Popen", side_effect=fake_popen):
                result = main_mod.run_start(main_mod.RunStartReq(project_root="demo"), Request({"type": "http", "method": "POST", "path": "/api/run/start", "headers": []}))

        self.assertTrue(result["ok"])
        self.assertIn(["npm", "run", "dev", "--", "--host", "127.0.0.1", "--strictPort", "--port", "4322"], commands)
        self.assertNotIn(["npm", "run", "preview", "--", "--host", "127.0.0.1", "--strictPort", "--port", "4322"], commands)

    def test_preview_runner_uses_vite_server_for_build_only_vite_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            project.mkdir(parents=True)
            (project / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"vite": "^7.0.0", "react": "^19.0.0"}}),
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="installed\n", stderr="")

            def fake_popen(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(pid=12345, stdout=["ready\n"])

            with patch("api.main._ws", return_value=root), \
                patch("api.main._hydrate_hosted_project", return_value=None), \
                patch("api.main._ensure_runner_capacity", return_value=None), \
                patch("api.main._resolve_package_manager", return_value=("npm", ["npm"])), \
                patch("api.main._next_port", return_value=4324), \
                patch("api.main._is_serverless_runtime", return_value=False), \
                patch("subprocess.run", side_effect=fake_run), \
                patch("subprocess.Popen", side_effect=fake_popen):
                result = main_mod.run_start(main_mod.RunStartReq(project_root="demo"), Request({"type": "http", "method": "POST", "path": "/api/run/start", "headers": []}))

        self.assertTrue(result["ok"])
        self.assertIn(["npm", "install"], commands)
        self.assertIn(["npm", "exec", "vite", "--", "--host", "127.0.0.1", "--strictPort", "--port", "4324"], commands)
        self.assertNotIn([sys.executable, "-m", "http.server", "4324", "--bind", "127.0.0.1"], commands)

    def test_preview_runner_reuses_root_node_modules_for_appora_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            project.mkdir(parents=True)
            (project / "package.json").write_text(
                json.dumps({
                    "apporaTemplate": True,
                    "scripts": {"dev": "vite"},
                    "dependencies": {"react": "^19.0.0", "react-dom": "^19.0.0"},
                    "devDependencies": {"vite": "^7.0.0", "typescript": "^5.0.0", "@vitejs/plugin-react": "^5.0.0"},
                }),
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def fake_popen(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(pid=12345, stdout=["ready\n"])

            with patch("api.main._ws", return_value=root), \
                patch("api.main._hydrate_hosted_project", return_value=None), \
                patch("api.main._ensure_runner_capacity", return_value=None), \
                patch("api.main._resolve_package_manager", return_value=("npm", ["npm"])), \
                patch("api.main._next_port", return_value=4323), \
                patch("api.main._is_serverless_runtime", return_value=False), \
                patch("subprocess.run") as fake_run, \
                patch("subprocess.Popen", side_effect=fake_popen):
                result = main_mod.run_start(main_mod.RunStartReq(project_root="demo"), Request({"type": "http", "method": "POST", "path": "/api/run/start", "headers": []}))
            node_modules_exists = (project / "node_modules").exists()

        self.assertTrue(result["ok"])
        fake_run.assert_not_called()
        self.assertTrue(node_modules_exists)
        self.assertIn(["npm", "run", "dev", "--", "--host", "127.0.0.1", "--strictPort", "--port", "4323"], commands)


class ProjectTemplateRegressionTests(unittest.TestCase):
    def test_template_registry_renders_runnable_react_project(self) -> None:
        templates = list_project_templates()
        template_ids = {item["id"] for item in templates}

        self.assertIn("saas-dashboard", template_ids)
        self.assertIn("landing-pricing", template_ids)
        self.assertIn("portfolio", template_ids)
        self.assertIn("admin-crud", template_ids)
        self.assertIn("ai-tool-app", template_ids)

        files = render_project_template(template_id="ai-tool-app", project_root="demo", project_name="Demo AI")
        portfolio_files = render_project_template(template_id="portfolio", project_root="portfolio", project_name="Demo Portfolio")

        self.assertIn("package.json", files)
        self.assertIn("index.html", files)
        self.assertIn("src/App.tsx", files)
        self.assertIn("src/main.tsx", files)
        self.assertIn("README.md", files)
        self.assertIn(".voiceide/memory/project.md", files)
        self.assertIn('"apporaTemplate": true', files["package.json"])
        self.assertNotIn("react-router-dom", files["package.json"])
        self.assertNotIn("BrowserRouter", files["src/main.tsx"])
        self.assertIn("currentPath={path}", files["src/App.tsx"])
        self.assertIn("Template: AI Tool App", files[".voiceide/memory/project.md"])
        self.assertIn("Selected work", portfolio_files["src/pages/Home.tsx"])

    def test_create_project_uses_selected_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            with patch("api.projects.store.has_supabase", return_value=False):
                project = create_project(
                    workspace_root=workspace,
                    owner_id="user-1",
                    req=ProjectCreateReq(name="Ops Console", template_id="admin-crud"),
                )

            root = workspace / project.root
            self.assertTrue((root / "package.json").exists())
            self.assertTrue((root / "src" / "App.tsx").exists())
            self.assertIn("Admin CRUD", (root / "README.md").read_text(encoding="utf-8"))
            self.assertIn("Template: Admin CRUD", (root / ".voiceide" / "memory" / "project.md").read_text(encoding="utf-8"))

    def test_local_saved_project_crud_uses_workspace_relative_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            state_path = workspace / ".projects.json"

            with patch("api.projects.store.PROJECTS_STATE_PATH", state_path), \
                patch("api.projects.store.has_supabase", return_value=False):
                project = create_project(
                    workspace_root=workspace,
                    owner_id="user-1",
                    req=ProjectCreateReq(name="Demo", template_id="blank"),
                )
                (workspace / project.root / "src").mkdir()
                (workspace / project.root / "src" / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")
                saved = save_project_snapshot(workspace_root=workspace, owner_id="user-1", project_id=project.id)
                copy = duplicate_project(
                    workspace_root=workspace,
                    owner_id="user-1",
                    project_id=saved.id,
                    req=ProjectDuplicateReq(name="Demo Copy"),
                )
                listed = list_projects(workspace_root=workspace, owner_id="user-1")

            self.assertTrue((workspace / copy.root / "src" / "App.tsx").exists())
            self.assertEqual({item.name for item in listed}, {"Demo Copy", "Demo"})


class AgentEvalRegressionTests(unittest.TestCase):
    def test_offline_appora_contract_eval_passes_core_scenarios(self) -> None:
        result = run_appora_contract_eval()

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["scenarios"]), 5)
        self.assertTrue(all(item["changes"] >= 2 for item in result["scenarios"]))

    def test_template_registry_eval_guarantees_runnable_starters(self) -> None:
        result = validate_template_registry()

        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(len(result["templates"]), 5)

    def test_memory_failure_recall_eval_does_not_repeat_broken_approach(self) -> None:
        result = run_memory_failure_recall_eval()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["remembered_failure"])
        self.assertTrue(result["did_not_repeat_css_only"])


class AgentBenchmarkRegressionTests(unittest.TestCase):
    def test_aider_benchmark_adapter_builds_official_polyglot_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "aider-test"
            config = AiderBenchmarkConfig(
                workspace=workspace,
                run_name="smoke",
                model="openai/kr/claude-haiku-4.5",
                edit_format="whole",
                threads=1,
                num_tests=2,
                keywords="python",
                openai_api_base="http://host.docker.internal:20128/v1",
            )

            setup_commands = build_setup_commands(config)
            run_command = build_docker_run_command(config)
            dry = run_aider_benchmark(config, command_only=True)

        self.assertIn(["git", "clone", "https://github.com/Aider-AI/aider.git", str(workspace / "aider")], setup_commands)
        self.assertIn("aider-benchmark", run_command)
        self.assertIn("OPENAI_API_BASE=http://host.docker.internal:20128/v1", run_command)
        self.assertIn("./benchmark/benchmark.py", run_command[-1])
        self.assertIn("--exercises-dir polyglot-benchmark", run_command[-1])
        self.assertIn("--num-tests 2", run_command[-1])
        self.assertIn("--keywords python", run_command[-1])
        self.assertEqual(dry["mode"], "aider-polyglot")
        self.assertEqual(dry["commands"]["run"], run_command)
        self.assertEqual(dry["commands"]["preflight"], build_docker_preflight_command(config))

    def test_aider_benchmark_adapter_supports_varied_language_runs(self) -> None:
        config = AiderBenchmarkConfig(
            workspace=Path(".tmp-aider-test"),
            run_name="varied",
            num_tests=6,
            keywords="word-search,robot-simulator,tree-building",
            languages="javascript,go,python",
            new_run=True,
        )

        run_command = build_docker_run_command(config)

        self.assertIn("--keywords word-search,robot-simulator,tree-building", run_command[-1])
        self.assertIn("--languages javascript,go,python", run_command[-1])
        self.assertIn("--new", run_command[-1])

    def test_aider_benchmark_adapter_profiles_appora_model_for_repair_context(self) -> None:
        config = AiderBenchmarkConfig(workspace=Path(".tmp-aider-test"), run_name="smoke")

        run_command = build_docker_run_command(config)
        joined = " ".join(run_command)

        self.assertEqual(config.model, "openai/openrouter/google/gemma-4-31b-it:free")
        self.assertIn("AIDER_MODEL_SETTINGS_FILE=/aider/.appora/aider-model-settings.yml", run_command)
        self.assertIn("AIDER_MODEL_METADATA_FILE=/aider/.appora/aider-model-metadata.json", run_command)
        self.assertIn("PYTHONPATH=/aider/.appora", run_command)
        self.assertIn("APPORA_AIDER_MODEL_NAME=openai/openrouter/google/gemma-4-31b-it:free", run_command)
        self.assertIn("AIDER_MAX_CHAT_HISTORY_TOKENS=8192", run_command)
        self.assertIn("APPORA_AIDER_MAX_TEST_ERROR_CHARS=24000", run_command)
        self.assertIn("APPORA_AIDER_MAX_TEST_CONTRACT_CHARS=18000", run_command)
        self.assertIn("AIDER_WEAK_MODEL=openai/openrouter/google/gemma-4-31b-it:free", run_command)
        self.assertIn("--model openai/openrouter/google/gemma-4-31b-it:free", run_command[-1])
        self.assertIn("--edit-format whole", run_command[-1])
        self.assertIn("--tries 3", run_command[-1])
        self.assertIn("python3 /aider/.appora/patch_benchmark.py", run_command[-1])
        self.assertIn(".appora/aider-model-settings.yml", joined)

        profile = write_appora_aider_model_profile(config)
        settings = Path(profile["settings_path"]).read_text(encoding="utf-8")
        sitecustomize = Path(profile["sitecustomize_path"]).read_text(encoding="utf-8")
        benchmark_patch = Path(profile["benchmark_patch_path"]).read_text(encoding="utf-8")
        self.assertIn("final trailing newlines", settings)
        self.assertIn("default the end/range parameter to the start value", settings)
        self.assertIn("check user-defined/custom definitions before builtins", settings)
        self.assertIn("execute literal numbers/strings in stored definitions as literals", settings)
        self.assertIn("snapshot/expand the previous definition", settings)
        self.assertIn("models.Model.__init__ = _appora_model_init", sitecustomize)
        self.assertIn("APPORA_AIDER_MODEL_NAME", sitecustomize)
        self.assertIn('kwargs["auto_lint"] = False', sitecustomize)
        self.assertIn("AIDER_MAX_CHAT_HISTORY_TOKENS", sitecustomize)
        self.assertIn("_appora_compact_test_errors", benchmark_patch)
        self.assertIn("_appora_public_test_contract", benchmark_patch)
        self.assertIn("Public test contract", benchmark_patch)

    def test_aider_benchmark_preflight_uses_real_router_chat_model(self) -> None:
        config = AiderBenchmarkConfig(
            workspace=Path(".tmp-aider-test"),
            run_name="smoke",
            model="openai/qd/qmodel_latest",
        )

        preflight = build_docker_preflight_command(config)
        joined = " ".join(preflight)

        self.assertIn("APPORA_AIDER_ROUTER_MODEL=qd/qmodel_latest", preflight)
        self.assertIn("/chat/completions", joined)
        self.assertIn("router_chat_preflight_status=", joined)
        self.assertNotIn("APPORA_AIDER_ROUTER_MODEL=openai/qd/qmodel_latest", preflight)

    def test_aider_benchmark_preflight_checks_container_router_path_before_run(self) -> None:
        config = AiderBenchmarkConfig(workspace=Path(".tmp-aider-test"), run_name="smoke")
        calls: list[list[str]] = []

        def fake_run(command, *, cwd=None, timeout=None, env=None):
            calls.append(command)
            return {
                "command": command,
                "cwd": str(cwd) if cwd else None,
                "returncode": 1,
                "ok": False,
                "stdout": "",
                "stderr": "Connection refused",
            }

        with patch("api.aider_benchmarks._run_command", side_effect=fake_run):
            result = run_aider_benchmark(config, run=True)

        self.assertFalse(result["ok"], result)
        self.assertEqual(len(calls), 1)
        self.assertIn("preflight failed", result["summary"])
        self.assertIn("host.docker.internal", " ".join(calls[0]))

    def test_start_9router_defaults_to_network_bind_for_docker_benchmarks(self) -> None:
        script = (Path.cwd() / "scripts" / "start-9router.sh").read_text(encoding="utf-8")

        self.assertIn('HOST="${NINE_ROUTER_HOST:-0.0.0.0}"', script)

    def test_vite_build_disables_memory_heavy_minification(self) -> None:
        config = (Path.cwd() / "vite.config.ts").read_text(encoding="utf-8")

        self.assertIn("minify: false", config)

    def test_editor_wrapper_avoids_bundling_monaco_for_memory_stable_builds(self) -> None:
        editor = (Path.cwd() / "src" / "features" / "workspace" / "components" / "editor" / "MonacoEditor.tsx").read_text(encoding="utf-8")
        package_json = (Path.cwd() / "package.json").read_text(encoding="utf-8")
        vite_config = (Path.cwd() / "vite.config.ts").read_text(encoding="utf-8")

        self.assertNotIn("@monaco-editor/react", editor)
        self.assertNotIn("monaco-editor", editor)
        self.assertNotIn("@monaco-editor/react", package_json)
        self.assertNotIn("monaco-editor", package_json)
        self.assertNotIn("@monaco-editor/react", vite_config)
        self.assertNotIn("monaco-editor", vite_config)

    def test_quick_layout_switch_does_not_persist_global_settings_or_restart_vite(self) -> None:
        app = (Path.cwd() / "src" / "app" / "App.tsx").read_text(encoding="utf-8")
        match = re.search(r"const quickSwitchBuildMode = \(mode: BuildMode\) => \{(?P<body>.*?)\n  \};", app, re.S)

        self.assertIsNotNone(match)
        body = match.group("body") if match else ""
        self.assertIn("setBuildMode(mode)", body)
        self.assertIn("setBuildModeDraft(mode)", body)
        self.assertNotIn("updateSettings", body)

    def test_aider_benchmark_stats_parser_extracts_core_metrics(self) -> None:
        stats = _parse_aider_stats(
            """
            hint: these noisy benchmark logs can contain colon separators
            unrelated_key: should_not_be_parsed
            - dirname: 2024-07-04-14-32-08--claude
              test_cases: 225
              model: claude-3.5-sonnet
              edit_format: diff
              pass_rate_1: 57.1
              percent_cases_well_formed: 99.2
              syntax_errors: 1
              total_cost: 3.6346
            """
        )

        self.assertNotIn("hint", stats)
        self.assertNotIn("unrelated_key", stats)
        self.assertEqual(stats["test_cases"], 225)
        self.assertEqual(stats["model"], "claude-3.5-sonnet")
        self.assertEqual(stats["pass_rate_1"], 57.1)
        self.assertEqual(stats["syntax_errors"], 1)

    def test_aider_benchmark_run_fails_when_official_pass_rate_is_zero(self) -> None:
        config = AiderBenchmarkConfig(workspace=Path(".tmp-aider-test"), run_name="smoke")
        stdout = """
        ────── /benchmarks/2026-06-09-16-40-01--appora-aider-smoke-router-public ───────
        - dirname: 2026-06-09-16-40-01--appora-aider-smoke-router-public
          test_cases: 1
          model: openai/appora
          edit_format: whole
          pass_rate_1: 0.0
          pass_rate_2: 0.0
          pass_num_1: 0
          pass_num_2: 0
          percent_cases_well_formed: 100.0
        """

        with patch("api.aider_benchmarks._run_command", return_value={
            "command": ["docker", "run"],
            "cwd": None,
            "returncode": 0,
            "ok": True,
            "stdout": stdout,
            "stderr": "",
        }):
            result = run_aider_benchmark(config, run=True)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stats"]["pass_rate_1"], 0.0)
        self.assertIn("failed official solve metrics", result["summary"])

    def test_aider_benchmark_run_fails_when_multi_case_result_is_partial(self) -> None:
        config = AiderBenchmarkConfig(workspace=Path(".tmp-aider-test"), run_name="smoke", num_tests=3)
        stdout = """
        ────── /benchmarks/2026-06-09-17-34-00--appora-aider-3case-nvim ───────
        - dirname: 2026-06-09-17-34-00--appora-aider-3case-nvim
          test_cases: 3
          model: openai/appora
          edit_format: whole
          pass_rate_1: 33.3
          pass_rate_2: 66.7
          pass_num_1: 1
          pass_num_2: 2
          percent_cases_well_formed: 100.0
        """

        with patch("api.aider_benchmarks._run_command", return_value={
            "command": ["docker", "run"],
            "cwd": None,
            "returncode": 0,
            "ok": True,
            "stdout": stdout,
            "stderr": "",
        }):
            result = run_aider_benchmark(config, run=True)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stats"]["test_cases"], 3)
        self.assertEqual(result["stats"]["pass_num_2"], 2)
        self.assertIn("failed official solve metrics", result["summary"])

    def test_aider_benchmark_run_fails_when_harness_reuses_existing_run_name(self) -> None:
        config = AiderBenchmarkConfig(workspace=Path(".tmp-aider-test"), run_name="smoke", num_tests=3)
        calls = [
            {
                "command": ["docker", "run"],
                "cwd": None,
                "returncode": 0,
                "ok": True,
                "stdout": "router_preflight_status=200\n",
                "stderr": "",
            },
            {
                "command": ["docker", "run"],
                "cwd": None,
                "returncode": 0,
                "ok": True,
                "stdout": "Prior runs of smoke exist, use --new or name one explicitly\n/benchmarks/2026-06-09--smoke\n",
                "stderr": "",
            },
        ]

        with patch("api.aider_benchmarks._run_command", side_effect=calls):
            result = run_aider_benchmark(config, run=True)

        self.assertFalse(result["ok"], result)
        self.assertIn("existing run name", result["summary"])

    def test_aider_benchmark_env_loads_local_dotenv_key_when_shell_env_missing(self) -> None:
        config = AiderBenchmarkConfig(openai_api_key_env="NINE_ROUTER_API_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_dir = root / "api"
            api_dir.mkdir()
            (api_dir / ".env").write_text("NINE_ROUTER_API_KEY=local-router-key\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True), patch("api.aider_benchmarks.Path.cwd", return_value=root):
                env = _benchmark_env(config)

        self.assertEqual(env["OPENAI_API_KEY"], "local-router-key")

    def test_aider_benchmark_command_timeout_returns_structured_failure(self) -> None:
        with patch("api.aider_benchmarks.subprocess.run", side_effect=subprocess.TimeoutExpired(["cmd"], 3, output="partial")):
            result = _run_command(["cmd"], timeout=3)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["returncode"], None)
        self.assertEqual(result["timeout"], 3)
        self.assertEqual(result["stdout"], "partial")
        self.assertIn("timed out", result["error"])

    def test_failure_analysis_guides_single_assertion_not_raised_repairs(self) -> None:
        execution = {
            "validation": {
                "ok": False,
                "results": [{
                    "ok": False,
                    "command": "pytest",
                    "returncode": 1,
                    "stdout": "FAILED test_properties_without_delimiter\nE AssertionError: ValueError not raised\ninput_string = \"(;A)\"",
                    "stderr": "",
                }],
            },
            "repairs": [],
        }

        analysis = main_mod._execution_failure_analysis(execution)

        self.assertIn("ValueError not raised", analysis["evidence_excerpt"])
        self.assertIn("add the missing validation branch", analysis["suggested_next_move"])

    def test_repair_state_keeps_latest_failing_validation_evidence(self) -> None:
        parent = {
            "ok": False,
            "validation": {"ok": False, "results": [{"ok": False, "stdout": "20 failed, 3 passed"}]},
        }
        repair_execution = {
            "ok": False,
            "validation": {"ok": False, "results": [{"ok": False, "stdout": "1 failed, 22 passed"}]},
        }

        main_mod._merge_repair_execution_state(parent, repair_execution)

        self.assertFalse(parent["ok"])
        self.assertIn("1 failed, 22 passed", parent["validation"]["results"][0]["stdout"])

    def test_agent_benchmark_dry_run_lists_live_scenarios_without_calling_llm(self) -> None:
        result = run_agent_benchmark_suite(live=False)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "dry-run")
        self.assertGreaterEqual(len(result["scenarios"]), 7)
        self.assertTrue(all(item["status"] == "ready" for item in result["scenarios"]))
        self.assertTrue(all("prompt" in item for item in result["scenarios"]))
        scenario_ids = {item["id"] for item in result["scenarios"]}
        self.assertIn("shadcn_dashboard_repair", scenario_ids)
        self.assertIn("shadcn_blank_vite_init", scenario_ids)
        self.assertIn("plain_css_avoid_tailwind_drift", scenario_ids)
        self.assertIn("shadcn_missing_button_import", scenario_ids)

    def test_agent_benchmark_seeds_shadcn_specific_projects(self) -> None:
        scenario = next(item for item in AGENT_BENCHMARK_SCENARIOS if item.id == "shadcn_missing_button_import")
        with tempfile.TemporaryDirectory() as tmp:
            from api.agent_benchmarks import _write_benchmark_project

            project_dir = _write_benchmark_project(Path(tmp), scenario)

            self.assertTrue((project_dir / "components.json").exists())
            self.assertIn("@/components/ui/button", (project_dir / "src" / "App.tsx").read_text(encoding="utf-8"))
            self.assertFalse((project_dir / "src" / "components" / "ui" / "button.tsx").exists())

    def test_live_agent_benchmark_invokes_runtime_and_scores_result(self) -> None:
        calls = []

        def fake_run_agent_impl(req, event_cb=None, job_id=None):
            calls.append(req)
            if event_cb:
                event_cb("status", {"phase": "context", "message": "Membaca struktur proyek."})
                event_cb("tool_call", {"name": "view_file_structure", "message": "Repo map."})
                event_cb("tool_output", {"name": "view_file_structure", "summary": "src/App.tsx"})
            return {
                "spoken": "Dashboard task tracker sudah dibentuk dan build lolos.",
                "changes": [{
                    "path": "benchmark-task-tracker-ui/src/App.tsx",
                    "new_content": (
                        "export default function App() { return <main>"
                        "<h1>Task tracker dashboard</h1>"
                        "<p>Metric cards show task priority, owner, status, and progress.</p>"
                        "<p>Empty state helps teams create the first task.</p>"
                        "</main>; }"
                    ),
                }],
                "actions": [{"type": "shell", "command": "npm run build"}],
                "execution": {"ok": True, "completion_report": {"summary": "Build passed."}},
                "trace": {"task_state": {"state": "completed"}, "verification": []},
            }

        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.agent_benchmarks.auth_snapshot", return_value={"nine_router": {"connected": True, "source": ".env", "auth_type": "byok", "base_url": "http://127.0.0.1:20128/v1"}}), \
            patch("api.agent_benchmarks.main_mod._run_agent_impl", side_effect=fake_run_agent_impl):
            result = run_agent_benchmark_suite(
                live=True,
                scenario_ids=["task_tracker_ui"],
                workspace_root=Path(tmp),
                isolate_scenarios=False,
            )
            self.assertTrue((Path(tmp) / "benchmark-task-tracker-ui" / "package.json").exists())
            self.assertTrue((Path(tmp) / "benchmark-task-tracker-ui" / "src" / "App.tsx").exists())

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "live")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].build_mode, "full-agent")
        self.assertTrue(calls[0].auto_execute)
        scenario = result["scenarios"][0]
        self.assertTrue(scenario["ok"], scenario)
        self.assertGreaterEqual(scenario["score"], 80)
        self.assertEqual(scenario["metrics"]["event_count"], 3)
        self.assertTrue(scenario["metrics"]["execution_ok"])
        self.assertEqual(scenario["metrics"]["requirement_coverage"], 1.0)
        self.assertIn("observability", scenario)
        self.assertEqual(scenario["observability"]["summary"]["tool_call_count"], 1)

    def test_live_agent_benchmark_penalizes_generic_or_wrong_domain_results(self) -> None:
        scenario = next(item for item in AGENT_BENCHMARK_SCENARIOS if item.id == "task_tracker_ui")
        scored = _score_live_result(
            {
                "spoken": "Aku bikin landing laundry premium.",
                "changes": [{
                    "path": "src/App.tsx",
                    "new_content": "<main><h1>Laundry premium</h1><p>Pricing and testimonial.</p></main>",
                }],
                "actions": [{"type": "shell", "command": "npm run build"}],
                "execution": {"ok": True},
                "trace": {"verification": [], "passes": 5},
            },
            [{"event": "tool_call"}, {"event": "command_start"}],
            scenario=scenario,
            duration_seconds=260,
        )

        self.assertLess(scored["score"], scenario.min_score, scored)
        self.assertLess(scored["metrics"]["requirement_coverage"], 0.65)
        self.assertIn("laundry", scored["metrics"]["matched_forbidden_terms"])
        self.assertGreater(scored["metrics"]["autonomous_passes"], scenario.max_autonomous_passes)

    def test_live_agent_benchmark_scores_final_project_files_for_requirement_coverage(self) -> None:
        scenario = next(item for item in AGENT_BENCHMARK_SCENARIOS if item.id == "preview_blank_repair")
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / scenario.project_root
            (project_dir / "src" / "pages").mkdir(parents=True)
            (project_dir / "src" / "pages" / "Home.tsx").write_text(
                "export default function Home(){ return <section><h1>Portfolio home preview</h1><p>Project work contact build.</p></section> }\n",
                encoding="utf-8",
            )
            scored = _score_live_result(
                {
                    "spoken": "Fixed AppShell render.",
                    "changes": [{"path": f"{scenario.project_root}/src/App.tsx", "new_content": "import Home from './pages/Home';\n"}],
                    "actions": [{"type": "shell", "command": "npm run build"}],
                    "execution": {"ok": True},
                    "trace": {"verification": []},
                },
                [{"event": "tool_call"}, {"event": "command_start"}],
                scenario=scenario,
                duration_seconds=40,
                project_dir=project_dir,
            )

        self.assertEqual(scored["metrics"]["missing_required_terms"], [])
        self.assertEqual(scored["metrics"]["requirement_coverage"], 1.0)

    def test_live_agent_benchmark_blocks_when_nine_router_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.agent_benchmarks.auth_snapshot", return_value={"nine_router": {"connected": False, "source": None}}), \
            patch("api.agent_benchmarks.main_mod._run_agent_impl") as run_agent:
            result = run_agent_benchmark_suite(
                live=True,
                scenario_ids=["task_tracker_ui"],
                workspace_root=Path(tmp),
                require_ready=True,
            )

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["blocked"], result)
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["scenarios"], [])
        self.assertFalse(result["readiness"]["connected"])
        run_agent.assert_not_called()

    def test_live_agent_benchmark_route_preflight_and_output_report(self) -> None:
        def fake_run_agent_impl(req, event_cb=None, job_id=None):
            if event_cb:
                event_cb("command_start", {"phase": "verify", "message": "npm run build"})
            return {
                "spoken": "Landing sudah siap.",
                "changes": [{
                    "path": "benchmark-nontechnical-landing/src/pages/Home.tsx",
                    "new_content": (
                        "export default function Home() { return <main>"
                        "<h1>Premium laundry booking</h1>"
                        "<p>Clear price cards, testimonial quotes, and premium CTA.</p>"
                        "</main>; }"
                    ),
                }],
                "actions": [{"type": "shell", "command": "npm run build"}],
                "execution": {"ok": True},
                "trace": {"verification": []},
            }

        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.agent_benchmarks.settings_mod.settings", SimpleNamespace(nine_router_model="free-forever", nine_router_base_url="https://router.appora.ai/v1")), \
            patch("api.agent_benchmarks.auth_snapshot", return_value={"nine_router": {"connected": True, "source": "appora_managed_free", "auth_type": "managed_free", "managed_free": True, "base_url": "https://router.appora.ai/v1"}}), \
            patch("api.agent_benchmarks.test_nine_router_route", return_value={"ok": True, "status": 200, "summary": "Connected. 9Router accepted the selected model/combo.", "model": "free-forever", "response": "OK"}), \
            patch("api.agent_benchmarks.main_mod._run_agent_impl", side_effect=fake_run_agent_impl):
            report_path = Path(tmp) / "reports" / "agent-benchmark.json"
            result = run_agent_benchmark_suite(
                live=True,
                scenario_ids=["nontechnical_landing"],
                workspace_root=Path(tmp),
                check_route=True,
                output_path=report_path,
            )
            self.assertTrue(report_path.exists())
            saved = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["blocked"])
        self.assertTrue(result["readiness"]["ok"])
        self.assertTrue(result["readiness"]["route_test"]["ok"])
        self.assertEqual(saved["mode"], "live")
        self.assertEqual(saved["readiness"]["model"], "free-forever")
        self.assertNotIn("api_key", json.dumps(saved))

    def test_live_agent_benchmark_marks_scenario_timeout(self) -> None:
        def fake_timeout(_req, event_cb=None, job_id=None):
            if event_cb:
                event_cb("error", {"phase": "stream", "message": "Scenario exceeded 7s timeout"})
            raise RuntimeError("400: Scenario exceeded 7s timeout")

        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.agent_benchmarks.auth_snapshot", return_value={"nine_router": {"connected": True, "source": ".env", "auth_type": "byok", "base_url": "http://127.0.0.1:20128/v1"}}), \
            patch("api.agent_benchmarks.main_mod._run_agent_impl", side_effect=fake_timeout):
            result = run_agent_benchmark_suite(
                live=True,
                scenario_ids=["task_tracker_ui"],
                workspace_root=Path(tmp),
                scenario_timeout_seconds=7,
                isolate_scenarios=False,
            )

        scenario = result["scenarios"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(scenario["status"], "timeout")
        self.assertEqual(scenario["timeout_seconds"], 7)

    def test_live_agent_benchmark_hard_times_out_blocking_worker(self) -> None:
        def fake_blocking(_req, event_cb=None, job_id=None):
            time.sleep(5)
            return {
                "spoken": "too late",
                "changes": [],
                "actions": [],
                "execution": {"ok": False},
                "trace": {"verification": []},
            }

        with tempfile.TemporaryDirectory() as tmp, \
            patch("api.agent_benchmarks.auth_snapshot", return_value={"nine_router": {"connected": True, "source": ".env", "auth_type": "byok", "base_url": "http://127.0.0.1:20128/v1"}}), \
            patch("api.agent_benchmarks.main_mod._run_agent_impl", side_effect=fake_blocking):
            started = time.monotonic()
            result = run_agent_benchmark_suite(
                live=True,
                scenario_ids=["task_tracker_ui"],
                workspace_root=Path(tmp),
                scenario_timeout_seconds=1,
            )
            elapsed = time.monotonic() - started

        scenario = result["scenarios"][0]
        self.assertLess(elapsed, 4.0)
        self.assertFalse(result["ok"])
        self.assertEqual(scenario["status"], "timeout")
        self.assertEqual(scenario["timeout_seconds"], 1)


class AgentObservabilityRegressionTests(unittest.TestCase):
    def test_observability_summarizes_agent_events_and_failures(self) -> None:
        events = [
            {"event_type": "status", "payload": {"phase": "starting", "message": "Mulai"}, "created_at": 100},
            {
                "event_type": "tool_call",
                "payload": {
                    "kind": "agent_harness_command",
                    "tool": "validate",
                    "phase": "executing_validation",
                    "command": "npm run build",
                    "summary": "validation: running `npm run build`",
                },
                "created_at": 101,
            },
            {
                "event_type": "tool_output",
                "payload": {
                    "kind": "agent_harness_command",
                    "tool": "validate",
                    "phase": "executing_validation",
                    "command": "npm run build",
                    "ok": False,
                    "returncode": 1,
                    "stderr_preview": "TS2307: Cannot find module './Missing'",
                    "summary": "validation: failed `npm run build`",
                },
                "created_at": 103,
            },
        ]
        result = {
            "changes": [{"path": "src/App.tsx"}],
            "actions": [{"type": "shell", "command": "npm run build"}],
            "execution": {"ok": False, "completion_report": {"summary": "Build failed."}},
        }

        observability = build_agent_observability(events, result=result)

        self.assertFalse(observability["ok"])
        self.assertEqual(observability["summary"]["event_count"], 3)
        self.assertEqual(observability["summary"]["command_count"], 1)
        self.assertEqual(observability["summary"]["failed_command_count"], 1)
        self.assertEqual(observability["summary"]["changes"], 1)
        self.assertEqual(observability["summary"]["actions"], 1)
        self.assertIn("executing_validation", observability["summary"]["phase_counts"])
        self.assertEqual(observability["commands"][0]["command"], "npm run build")
        self.assertFalse(observability["commands"][0]["ok"])
        self.assertIn("TS2307", observability["failure_points"][0]["detail"])

    def test_run_agent_impl_attaches_observability_to_result(self) -> None:
        session_id = "observability-run-agent-test"
        STATE.get("sessions", {}).pop(session_id, None)
        token = CURRENT_SESSION_ID.set(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "demo").mkdir()
                STATE["sessions"][session_id] = {
                    "workspace": str(root),
                    "runners": {},
                    "agent_jobs": {},
                    "oauth_pending": {},
                    "google_user": None,
                }

                def fake_pipeline(_req, ws_root, emit):
                    emit("status", {"phase": "planning", "message": "Planning"})
                    emit("tool_call", {"tool": "repo-map", "phase": "tooling", "summary": "Read repo"})
                    emit("tool_output", {"tool": "repo-map", "phase": "tooling", "ok": True, "summary": "Read 3 files"})
                    return {
                        "spoken": "Aku sudah cek.",
                        "log": "",
                        "changes": [],
                        "actions": [],
                        "intent": {"kind": "inspection"},
                        "trace": {},
                    }

                with patch("api.main.run_agent_pipeline", side_effect=fake_pipeline):
                    result = main_mod._run_agent_impl(main_mod.AgentReq(input="cek aja", project_root="demo", build_mode="full-agent"))

            observability = result["observability"]
            self.assertTrue(observability["ok"])
            self.assertEqual(observability["summary"]["tool_call_count"], 1)
            self.assertEqual(observability["timeline"][0]["phase"], "starting")
            self.assertIn("tooling", observability["summary"]["phase_counts"])
        finally:
            CURRENT_SESSION_ID.reset(token)
            STATE.get("sessions", {}).pop(session_id, None)


class AgentLongHorizonPlannerRegressionTests(unittest.TestCase):
    def test_long_horizon_plan_builds_checkpoints_and_completion_criteria(self) -> None:
        horizon = build_long_horizon_plan(
            goal="Rombak app jadi project management SaaS siap produksi dengan dashboard dan billing.",
            user_input="rombak besar app ini jadi project management SaaS siap produksi",
            base_plan=[
                {"stage": "scope", "title": "Understand task boundary", "files": ["src/App.tsx"]},
                {"stage": "tool_loop", "title": "Inspect repo", "files": []},
                {"stage": "act", "title": "Implement", "files": ["src/App.tsx", "src/app.css"]},
                {"stage": "verify", "title": "Validate", "files": []},
            ],
            intent_kind="command",
            should_write_files=True,
            is_full_agent=True,
            project_root="demo",
        )

        self.assertTrue(horizon["enabled"])
        self.assertEqual(horizon["status"], "planned")
        self.assertEqual(horizon["current_checkpoint"], "context")
        self.assertGreaterEqual(len(horizon["checkpoints"]), 4)
        self.assertTrue(any(item["id"] == "implementation" for item in horizon["checkpoints"]))
        self.assertTrue(any("validation" in item.lower() for item in horizon["completion_criteria"]))
        self.assertIn("demo", horizon["project_root"])

    def test_long_horizon_progress_marks_blocked_or_ready_for_execution(self) -> None:
        horizon = build_long_horizon_plan(
            goal="Build dashboard",
            user_input="build dashboard",
            base_plan=[
                {"stage": "scope", "title": "Scope"},
                {"stage": "act", "title": "Act"},
                {"stage": "verify", "title": "Verify"},
            ],
            intent_kind="command",
            should_write_files=True,
            is_full_agent=True,
            project_root="demo",
        )

        blocked = update_long_horizon_progress(horizon, changes_count=0, actions_count=0, blocking_checks=["has-work-output"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["current_checkpoint"], "implementation")
        self.assertEqual(blocked["checkpoints"][1]["status"], "blocked")

        ready = update_long_horizon_progress(horizon, changes_count=2, actions_count=1, blocking_checks=[])
        self.assertEqual(ready["status"], "ready_for_execution")
        self.assertEqual(ready["current_checkpoint"], "validation")
        self.assertEqual(ready["checkpoints"][1]["status"], "done")


class PatchApplyRegressionTests(unittest.TestCase):
    def test_apply_many_preflight_reports_stale_patch_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src" / "App.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")
            stale_hash = _sha256_text("old\n")
            target.write_text("user edit\n", encoding="utf-8")

            result = _preflight_apply_many(
                root,
                ApplyManyReq(
                    overwrite=True,
                    ops=[
                        WriteOp(
                            path="src/App.tsx",
                            content="agent edit\n",
                            expected_sha256=stale_hash,
                            expected_exists=True,
                        )
                    ],
                ),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["conflicts"][0]["reason"], "stale_hash")
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")

    def test_apply_many_preflight_accepts_matching_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src" / "App.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")

            result = _preflight_apply_many(
                root,
                ApplyManyReq(
                    overwrite=True,
                    ops=[
                        WriteOp(
                            path="src/App.tsx",
                            content="agent edit\n",
                            expected_sha256=_sha256_text("old\n"),
                            expected_exists=True,
                        )
                    ],
                ),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["conflicts"], [])

    def test_apply_many_rejects_stale_agent_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src" / "App.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")
            stale_hash = _sha256_text("old\n")
            target.write_text("user edit\n", encoding="utf-8")

            req = ApplyManyReq(
                overwrite=True,
                ops=[
                    WriteOp(
                        path="src/App.tsx",
                        content="agent edit\n",
                        expected_sha256=stale_hash,
                        expected_exists=True,
                    )
                ],
            )

            with patch("api.main._ws", return_value=root):
                with self.assertRaises(Exception) as raised:
                    fs_apply_many(req)

            self.assertEqual(getattr(raised.exception, "status_code", None), 409)
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")

    def test_apply_many_accepts_matching_agent_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src" / "App.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")

            req = ApplyManyReq(
                overwrite=True,
                ops=[
                    WriteOp(
                        path="src/App.tsx",
                        content="agent edit\n",
                        expected_sha256=_sha256_text("old\n"),
                        expected_exists=True,
                    )
                ],
            )

            with patch("api.main._ws", return_value=root), patch("api.main._persist_hosted_file", return_value=None):
                result = fs_apply_many(req)

            self.assertEqual(result["count"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "agent edit\n")

    def test_agent_harness_apply_creates_checkpoint_and_rejects_stale_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo" / "src" / "App.tsx"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")

            with patch("api.main._ws", return_value=root), patch("api.main._persist_hosted_file", return_value=None):
                applied = main_mod.agent_harness_apply(
                    main_mod.AgentHarnessApplyReq(
                        project_root="demo",
                        label="Applying",
                        changes=[
                            main_mod.AgentHarnessApplyChange(
                                path="demo/src/App.tsx",
                                content="agent edit\n",
                                expected_sha256=_sha256_text("old\n"),
                                expected_exists=True,
                            )
                        ],
                    )
                )

                target.write_text("user edit\n", encoding="utf-8")
                stale = main_mod.agent_harness_apply(
                    main_mod.AgentHarnessApplyReq(
                        project_root="demo",
                        label="Applying",
                        changes=[
                            main_mod.AgentHarnessApplyChange(
                                path="demo/src/App.tsx",
                                content="second agent edit\n",
                                expected_sha256=_sha256_text("agent edit\n"),
                                expected_exists=True,
                            )
                        ],
                    )
                )

            self.assertTrue(applied["ok"])
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["count"], 1)
            self.assertTrue((root / applied["checkpoint_path"]).exists())
            self.assertFalse(stale["ok"])
            self.assertFalse(stale["applied"])
            self.assertEqual(stale["conflicts"][0]["reason"], "stale_hash")
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")


class TranscriptPurityRegressionTests(unittest.TestCase):
    def test_workflow_only_appends_spoken_chunks_to_assistant_bubbles(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        workflow_path = repo_root / "src" / "features" / "agent" / "workflow.ts"
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
        live_append_lines = [
            line.strip()
            for line in lines
            if "appendAssistantLiveText(" in line and "appendAssistantLiveText:" not in line
        ]

        self.assertEqual(live_append_lines, [
            'appendAssistantLiveText(spokenChunk, "default", true);',
            'appendAssistantLiveText(spokenChunk, "default", false);',
        ])
        self.assertIn("if (nativeStream)", workflow_path.read_text(encoding="utf-8"))

    def test_workflow_has_no_hardcoded_assistant_milestones(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        workflow_text = (repo_root / "src" / "features" / "agent" / "workflow.ts").read_text(encoding="utf-8")

        self.assertNotIn("pushAssistantMilestone", workflow_text)
        self.assertNotIn("Konteksnya sudah kebaca", workflow_text)
        self.assertIn('event.event === "tool_call"', workflow_text)
        self.assertIn('event.event === "tool_output"', workflow_text)

    def test_frontend_does_not_double_apply_failed_backend_execution(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        workflow_text = (repo_root / "src" / "features" / "agent" / "workflow.ts").read_text(encoding="utf-8")

        self.assertIn("const backendAutoExecuted = res.execution?.auto_execute === true && !res.execution?.skipped;", workflow_text)
        self.assertNotIn("res.execution?.ok !== false && !res.execution?.skipped", workflow_text)
        self.assertIn("const backendExecutionBlocked = backendAutoExecuted", workflow_text)
        self.assertIn("} else if (!backendAutoExecuted && needsRepair()", workflow_text)
        self.assertIn("Backend execution blocked after autonomous repair loop", workflow_text)
        self.assertIn('repair_stop: "Repair budget habis, agent mencatat blocker…"', workflow_text)
        self.assertIn('completion: "Agent menyusun completion report…"', workflow_text)
        self.assertIn('verifier_repair: { phase: "repair", kind: "verifier_repair", label: "Verifier repair" }', workflow_text)

    def test_agent_contract_requires_model_native_progress(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runtime_text = (repo_root / "api" / "agent_runtime.py").read_text(encoding="utf-8")
        agent_text = (repo_root / "api" / "agent.py").read_text(encoding="utf-8")

        self.assertIn("CODEX-STYLE PROGRESS", runtime_text)
        self.assertIn("If you return `tool` or `mcp` actions", runtime_text)
        self.assertIn("If you need to call tools or run project actions", agent_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

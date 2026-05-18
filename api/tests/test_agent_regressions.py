from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request

from api.agent_intent import classify_agent_intent
from api.agent_evals import run_clara_contract_eval, validate_template_registry
from api import agent as agent_mod
from api import main as main_mod
from api.agent_mcp import MCPServerInfo, MCPToolCallResult, MCPToolInfo, discover_mcp_servers, execute_mcp_tool, suggest_mcp_actions
from api.agent_memory import get_agent_memory_overview, remember_agent_run, retrieve_agent_memory
from api.agent_runtime import _autonomous_continue_node, _intent_with_active_work_context, _looks_like_plan_only_reply, _max_tool_loops_for_run, _plan_node, _remember_project_work_state, _route_after_verify, _should_run_deep_preflight, _should_run_refinement, _strict_agentic_retry_node, _verify_node, prepare_agent_context
from api.agent_skills import detect_project_stack, resolve_agent_skills
from api.agent_tools import execute_local_tool
from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE
from api.auth_identity import AuthenticatedUser
from api.fs import safe_join
from api.hybrid import build_hybrid_seed
from api.project_templates import list_project_templates, render_project_template
from api.projects import ProjectCreateReq, ProjectDuplicateReq, create_project, duplicate_project, list_projects, save_project_snapshot
from api.main import ApplyManyReq, WriteOp, _browser_preview_audit_ready, _build_preview_audit_result, _build_quality_checks, _command_policy_decision, _extract_preview_snapshot_from_html, _preflight_apply_many, _sha256_text, agent_capabilities, fs_apply_many, supabase_rag_status
from api.preferences import UserPreferencesRecord
from api.preferences_router import build_preferences_router
from api.secrets_store import get_provider_secret, has_provider_secret
from api.settings import load_settings
from api.settings_router import SettingsUpdateReq, build_settings_router
from api.supabase_store import upsert_profile
from api.oauth_runtime import CURRENT_PROFILE_ID, list_models as list_provider_models, provider_catalog


class AgentIntentRegressionTests(unittest.TestCase):
    def test_intent_boundaries(self) -> None:
        cases = [
            ("fix navbar spacing and add loading state", "command", True, True),
            ("audit code agent dari graph rag dan lain lain laporin ke gw", "inspection", False, False),
            ("jelasin flow graph agent ini", "inspection", False, False),
            ("gimana statusnya bro?", "conversation", False, False),
            ("review lalu perbaiki auth flow ini", "command", True, True),
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


class AgentRuntimeContextRegressionTests(unittest.TestCase):
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

    def test_blocked_task_state_routes_to_autonomous_continue(self) -> None:
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

            self.assertEqual(_route_after_verify(state), "autonomous_continue")
            continued = _autonomous_continue_node(state)
            self.assertEqual(continued["autonomous_iterations"], 1)
            self.assertEqual(continued["changes"], [])
            self.assertIn("AUTONOMOUS TASK LOOP EVIDENCE", continued["context"].extra_context)

            state["autonomous_iterations"] = 2
            self.assertEqual(_route_after_verify(state), "finalize")

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


class AgentVerifierRegressionTests(unittest.TestCase):
    def _ctx(self, ws_root: Path, prompt: str = "fix app imports"):
        req = SimpleNamespace(
            input=prompt,
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
        self.assertEqual(audit["visual_summary"]["mode"], "browser")
        self.assertTrue(audit["visual_summary"]["top_blockers"])
        self.assertIn("Top issues:", audit["repair_brief"])

    def test_browser_audit_is_runtime_capability_not_project_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "package.json").write_text('{"dependencies":{}}', encoding="utf-8")

            self.assertTrue(_browser_preview_audit_ready(project_dir))


class CommandPolicyRegressionTests(unittest.TestCase):
    def test_command_policy_allows_project_validation_commands(self) -> None:
        for command in [
            "npm run build",
            "npm test",
            "npm i typescript --save-dev",
            "pnpm add lucide-react",
            "yarn add @vitejs/plugin-react",
            "bun add clsx",
            "npm install && npm run build",
            "python3 -m compileall api",
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

        for command in ["npm run build; rm -rf src", "npm run build | bash", "npm install -g vercel"]:
            with self.subTest(command=command):
                decision = _command_policy_decision(command)
                self.assertFalse(decision.ok)
                self.assertIn(decision.risk_level, {"approval_required", "blocked"})

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
                ":root { --color-bg: #fff; }\n@media (max-width: 700px) { main { display: grid; } }\n/* TODO: remove old spacing token */\n",
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


class HybridSeedRegressionTests(unittest.TestCase):
    def test_saas_brief_defaults_to_app_workspace_not_dashboard(self) -> None:
        files = build_hybrid_seed(
            project_root="demo",
            project_name="Acme Flow",
            instruction="Build a modern SaaS product with onboarding, workspace, and integrations.",
        )

        app_tsx = files["demo/src/App.tsx"]
        self.assertIn('path="/workspace"', app_tsx)
        self.assertIn('path="/integrations"', app_tsx)
        self.assertNotIn('path="/dashboard"', app_tsx)
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

        self.assertIn('path="/contact"', files["demo/src/App.tsx"])
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
        self.assertIn('path="/docs"', app_tsx)
        self.assertNotIn('path="/workspace"', app_tsx)
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
        self.assertIn('path="/dashboard"', app_tsx)
        self.assertNotIn('path="/workspace"', app_tsx)
        self.assertNotIn('path="/contact"', app_tsx)
        self.assertIn("demo/src/pages/Dashboard.tsx", files)
        self.assertNotIn("demo/src/pages/Workspace.tsx", files)
        self.assertNotIn("demo/src/pages/Integrations.tsx", files)
        self.assertNotIn("demo/src/pages/AppSettings.tsx", files)


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
        with patch("api.supabase_store.get_supabase_admin", return_value=fake_client):
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
        with patch("api.secrets_store._require_supabase", return_value=fake_client), \
            patch("api.secrets_store._decrypt", side_effect=lambda value: "sk-demo" if value == "cipher-demo" else None):
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
            patch("api.settings_router.resolve_request_user", return_value=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123")), \
            patch("api.settings_router.has_supabase", return_value=True), \
            patch("api.settings_router.upsert_provider_secret", side_effect=lambda profile_id, provider, api_key: saved_secret_updates.append((profile_id, provider))), \
            patch("api.settings_router.upsert_user_preferences", side_effect=lambda profile_id, req: saved_pref_profile_ids.append(profile_id)):
            resp = update_endpoint(SettingsUpdateReq(llm_provider="openai", nine_router_api_key="sk-demo"))

        self.assertTrue(resp["ok"])
        self.assertEqual(saved_secret_updates, [("sb-user-123", "nine_router")])
        self.assertEqual(saved_pref_profile_ids, ["sb-user-123"])

    def test_hosted_preferences_router_uses_internal_profile_id(self) -> None:
        router = build_preferences_router()
        get_endpoint = next(route.endpoint for route in router.routes if getattr(route, "path", "") == "/api/preferences/user" and "GET" in getattr(route, "methods", set()))

        seen_profile_ids: list[str] = []

        with patch("api.preferences_router.get_user_preferences", side_effect=lambda profile_id: seen_profile_ids.append(profile_id) or UserPreferencesRecord(profile_id=profile_id)):
            resp = get_endpoint(user=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123"))

        self.assertEqual(resp.preferences.profile_id, "sb-user-123")
        self.assertEqual(seen_profile_ids, ["sb-user-123"])


class AgentAutoExecuteRegressionTests(unittest.TestCase):
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

                with patch("api.main.run_agent_pipeline", return_value=pipeline), \
                    patch("api.main.has_supabase", return_value=False), \
                    patch("api.main._persist_hosted_file", return_value=None):
                    result = main_mod._run_agent_impl(
                        main_mod.AgentReq(input="patch and validate", project_root="demo", auto_execute=True),
                        event_cb=lambda *_args: None,
                    )

                self.assertTrue(result["execution"]["ok"])
                self.assertTrue(result["execution"]["apply"]["applied"])
                self.assertEqual(result["execution"]["shell"]["ran"], 1)
                self.assertTrue(result["execution"]["shell"]["results"][0]["ok"])
                self.assertTrue(result["execution"]["validation"]["ok"])
                self.assertGreaterEqual(result["execution"]["validation"]["ran"], 1)
                step_kinds = [step["kind"] for step in result["execution"]["steps"]]
                self.assertIn("apply", step_kinds)
                self.assertIn("shell", step_kinds)
                self.assertIn("validation", step_kinds)
                self.assertEqual((project / "src" / "App.tsx").read_text(encoding="utf-8"), "agent edit\n")
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
                self.assertFalse(result["execution"]["validation"]["ok"])
                self.assertEqual(len(result["execution"]["repairs"]), 1)
                repair_execution = result["execution"]["repairs"][0]["execution"]
                self.assertTrue(repair_execution["ok"])
                self.assertTrue(repair_execution["validation"]["ok"])
                self.assertTrue(repair_execution["replay"]["ok"])
                self.assertEqual(repair_execution["replay"]["results"][0]["command"], "python3 -m compileall helper.py")
                self.assertIn("replay", [step["kind"] for step in repair_execution["steps"]])
                self.assertIn("repair", [step["kind"] for step in result["execution"]["steps"]])
                self.assertEqual(result["execution"]["completion_report"]["state"], "complete")
                self.assertIn("completion", [step["kind"] for step in result["execution"]["steps"]])
                validation_criteria = [
                    item for item in result["execution"]["completion_report"]["criteria"]
                    if item["label"] == "validation"
                ]
                self.assertEqual(validation_criteria[0]["status"], "superseded")
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

        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
            },
            clear=False,
        ), patch("api.oauth_runtime.has_provider_secret", return_value=False):
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

        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_FREE_DAILY_MESSAGES": "1000",
            },
            clear=False,
        ), patch("api.oauth_runtime.has_provider_secret", return_value=False), \
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

        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_FREE_DAILY_MESSAGES": "1000",
            },
            clear=False,
        ), patch("api.oauth_runtime.has_provider_secret", return_value=False), \
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

        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
                "APPORA_MANAGED_FREE_MODEL": "kr/claude-sonnet-4.5",
                "APPORA_FREE_DAILY_MESSAGES": "3",
            },
            clear=False,
        ), patch("api.oauth_runtime.has_provider_secret", return_value=False), \
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

        with patch.dict(
            "os.environ",
            {
                "APPORA_MANAGED_9ROUTER_BASE_URL": "https://router.appora.ai/v1",
                "APPORA_MANAGED_9ROUTER_API_KEY": "managed-key",
            },
            clear=False,
        ), patch("api.oauth_runtime.has_provider_secret", return_value=False):
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
        with patch("api.main.has_supabase", return_value=True):
            requires_verified_user = main_mod._requires_verified_hosted_user("/api/fs/list")

        self.assertTrue(requires_verified_user)


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
        self.assertIn("react-router-dom", files["package.json"])
        self.assertIn("Template: AI Tool App", files[".voiceide/memory/project.md"])
        self.assertIn("Selected work", portfolio_files["src/pages/Home.tsx"])

    def test_create_project_uses_selected_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            with patch("api.projects.has_supabase", return_value=False):
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

            with patch("api.projects.PROJECTS_STATE_PATH", state_path), \
                patch("api.projects.has_supabase", return_value=False):
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
    def test_offline_clara_contract_eval_passes_core_scenarios(self) -> None:
        result = run_clara_contract_eval()

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["scenarios"]), 5)
        self.assertTrue(all(item["changes"] >= 2 for item in result["scenarios"]))

    def test_template_registry_eval_guarantees_runnable_starters(self) -> None:
        result = validate_template_registry()

        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(len(result["templates"]), 5)


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
        workflow_path = repo_root / "src" / "agent" / "workflow.ts"
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
        live_append_lines = [
            line.strip()
            for line in lines
            if "appendAssistantLiveText(" in line and "appendAssistantLiveText:" not in line
        ]

        self.assertEqual(live_append_lines, ['appendAssistantLiveText(spokenChunk, "default", nativeStream);'])

    def test_workflow_has_no_hardcoded_assistant_milestones(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        workflow_text = (repo_root / "src" / "agent" / "workflow.ts").read_text(encoding="utf-8")

        self.assertNotIn("pushAssistantMilestone", workflow_text)
        self.assertNotIn("Konteksnya sudah kebaca", workflow_text)
        self.assertIn('event.event === "tool_call"', workflow_text)
        self.assertIn('event.event === "tool_output"', workflow_text)

    def test_agent_contract_requires_model_native_progress(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runtime_text = (repo_root / "api" / "agent_runtime.py").read_text(encoding="utf-8")
        agent_text = (repo_root / "api" / "agent.py").read_text(encoding="utf-8")

        self.assertIn("CODEX-STYLE PROGRESS", runtime_text)
        self.assertIn("If you return `tool` or `mcp` actions", runtime_text)
        self.assertIn("If you need to call tools or run project actions", agent_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

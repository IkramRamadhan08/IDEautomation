from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.agent_intent import classify_agent_intent
from api import agent as agent_mod
from api.agent_mcp import MCPServerInfo, MCPToolCallResult, MCPToolInfo, discover_mcp_servers, execute_mcp_tool, suggest_mcp_actions
from api.agent_memory import get_agent_memory_overview, remember_agent_run, retrieve_agent_memory
from api.agent_runtime import _max_tool_loops_for_run, _should_run_deep_preflight, prepare_agent_context
from api.agent_skills import detect_project_stack, resolve_agent_skills
from api.agent_tools import execute_local_tool
from api.auth_identity import AuthenticatedUser
from api.auth_policy import require_hosted_user
from api.fs import safe_join
from api.hybrid import build_hybrid_seed
from api.project_templates import list_project_templates, render_project_template
from api.projects import ProjectCreateReq, create_project
from api.main import ApplyManyReq, WriteOp, _browser_preview_audit_ready, _build_quality_checks, _extract_preview_snapshot_from_html, _sha256_text, agent_capabilities, app as main_app, fs_apply_many, supabase_rag_status
from api.preferences import UserPreferencesRecord
from api.preferences_router import build_preferences_router
from api.secrets_store import get_provider_secret, has_provider_secret
from api.settings import load_settings
from api.settings_router import build_settings_router
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


class AgentRuntimeContextRegressionTests(unittest.TestCase):
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

    def test_free_tier_build_still_allows_one_local_tool_loop(self) -> None:
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
                self.assertEqual(_max_tool_loops_for_run(ctx), 1)


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

    def test_browser_audit_is_runtime_capability_not_project_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "package.json").write_text('{"dependencies":{}}', encoding="utf-8")

            self.assertTrue(_browser_preview_audit_ready(project_dir))


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
    def test_streaming_agent_keeps_profile_context_in_worker_thread(self) -> None:
        seen_profile_ids: list[str | None] = []

        def fake_run_agent_impl(req, event_cb=None, job_id=None):
            seen_profile_ids.append(CURRENT_PROFILE_ID.get())
            if event_cb:
                event_cb("done", {"result": {"ok": True, "reply": "hi", "actions": [], "changes": [], "trace": {"passes": 1, "memory_hits": [], "skills": [], "mcp_servers": [], "mcp_tools_used": [], "warnings": []}}})
            return {"ok": True}

        with patch("api.main.resolve_request_user", return_value=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123")), \
            patch("api.main._run_agent_impl", side_effect=fake_run_agent_impl):
            client = TestClient(main_app)
            with client.stream("POST", "/api/agent", json={"input": "hello", "stream": True}, headers={"Authorization": "Bearer test-token", "X-VoiceIDE-Session": "sess-1"}) as resp:
                self.assertEqual(resp.status_code, 200)
                list(resp.iter_text())

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
        app = FastAPI()
        app.include_router(build_settings_router(session_state=lambda: {"workspace": None}, env_set=lambda *_args, **_kwargs: None, env_unset=lambda *_args, **_kwargs: None, reload_settings=lambda: None))

        saved_secret_profile_ids: list[str] = []
        saved_pref_profile_ids: list[str] = []

        with patch("api.settings_router.resolve_request_user", return_value=AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123")), \
            patch("api.settings_router.has_supabase", return_value=True), \
            patch("api.settings_router.os.getenv", side_effect=lambda key, default=None: "secret-ready" if key == "VOICEIDE_SECRET_KEY" else default), \
            patch("api.settings_router.upsert_provider_secret", side_effect=lambda profile_id, provider, api_key: saved_secret_profile_ids.append(profile_id)), \
            patch("api.settings_router.upsert_user_preferences", side_effect=lambda profile_id, req: saved_pref_profile_ids.append(profile_id)):
            client = TestClient(app)
            resp = client.put("/api/settings", json={"llm_provider": "openai", "openai_api_key": "sk-demo"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(saved_secret_profile_ids, ["sb-user-123"])
        self.assertEqual(saved_pref_profile_ids, ["sb-user-123"])

    def test_hosted_preferences_router_uses_internal_profile_id(self) -> None:
        app = FastAPI()
        app.include_router(build_preferences_router())
        app.dependency_overrides[require_hosted_user] = lambda: AuthenticatedUser(user_id="sb-user-123", auth_source="supabase", supabase_user_id="00000000-0000-0000-0000-000000000123")

        seen_profile_ids: list[str] = []

        with patch("api.preferences_router.get_user_preferences", side_effect=lambda profile_id: seen_profile_ids.append(profile_id) or UserPreferencesRecord(profile_id=profile_id)):
            client = TestClient(app)
            resp = client.get("/api/preferences/user")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen_profile_ids, ["sb-user-123"])


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
    def test_openrouter_free_router_is_default_and_paid_models_remain_available(self) -> None:
        models = list_provider_models("openrouter")
        catalog = provider_catalog()

        self.assertEqual(models[0], "openrouter/free")
        self.assertEqual(catalog["openrouter"]["recommended_model"], models[0])
        self.assertTrue(any(model.endswith(":free") for model in catalog["openrouter"]["free_tier_models"]))
        self.assertIn("x-ai/grok-4.3", models)

    def test_openai_is_familiar_credit_path_not_fake_free_tier(self) -> None:
        catalog = provider_catalog()

        self.assertEqual(catalog["openai"]["recommended_model"], "gpt-5.5")
        self.assertEqual(catalog["openai"]["free_tier_models"], [])
        self.assertIn("trial/account credits", catalog["openai"]["positioning"])

    def test_groq_is_available_as_free_plan_path(self) -> None:
        models = list_provider_models("groq")
        catalog = provider_catalog()

        self.assertEqual(catalog["groq"]["recommended_model"], models[0])
        self.assertTrue(catalog["groq"]["free_tier_models"])
        self.assertIn("free-plan", catalog["groq"]["positioning"])

    def test_multi_provider_catalog_has_web_builder_choices(self) -> None:
        catalog = provider_catalog()
        for provider in ["gemini", "together", "cerebras", "xai"]:
            models = list_provider_models(provider)
            self.assertTrue(models, provider)
            self.assertEqual(catalog[provider]["recommended_model"], models[0])

        self.assertTrue(catalog["gemini"]["free_tier_models"])
        self.assertTrue(catalog["cerebras"]["free_tier_models"])

    def test_free_tier_fallback_order_prefers_connected_free_routing(self) -> None:
        snapshot = {
            "openai": {"connected": True},
            "openrouter": {"connected": True},
            "gemini": {"connected": True},
            "groq": {"connected": False},
        }
        with patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0):
            order = agent_mod._fallback_provider_order("openai")

        self.assertEqual(order[:3], ["openai", "openrouter", "gemini"])

    def test_generate_json_falls_back_to_connected_provider_on_rate_limit(self) -> None:
        snapshot = {
            "openai": {"connected": True},
            "openrouter": {"connected": True},
        }
        attempted: list[tuple[str, str]] = []

        def fake_once(provider: str, model: str, *, system: str, user: str):
            attempted.append((provider, model))
            if provider == "openai":
                raise RuntimeError("OpenAI sedang kena rate limit.")
            return {"spoken": "ok", "changes": [], "actions": []}

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", "openai"), \
            patch.object(agent_mod.settings_mod.settings, "openai_model", "gpt-5.5"), \
            patch.object(agent_mod.settings_mod.settings, "openrouter_model", "openrouter/free"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", side_effect=fake_once):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "openrouter/free")
        self.assertEqual(data["spoken"], "ok")
        self.assertEqual(data["_voiceide_provider_fallback"]["selected_provider"], "openai")
        self.assertNotIn(("openai", "gpt-5.5"), attempted)

    def test_free_mode_uses_provider_free_models_instead_of_paid_defaults(self) -> None:
        with patch.object(agent_mod.settings_mod.settings, "openrouter_model", "x-ai/grok-4.3"), \
            patch.object(agent_mod.settings_mod.settings, "gemini_model", "gemini-3-pro-preview"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True):
            openrouter_models = agent_mod._candidate_models_for_provider("openrouter")
            gemini_models = agent_mod._candidate_models_for_provider("gemini")
            openai_models = agent_mod._candidate_models_for_provider("openai")

        self.assertEqual(openrouter_models[0], "openrouter/free")
        self.assertTrue(all(model == "openrouter/free" or model.endswith(":free") for model in openrouter_models))
        self.assertIn("gemini-3-flash-preview", gemini_models)
        self.assertNotIn("gemini-3-pro-preview", gemini_models)
        self.assertEqual(openai_models, [])

    def test_generate_json_can_use_connected_provider_when_none_selected(self) -> None:
        snapshot = {
            "openrouter": {"connected": True},
            "gemini": {"connected": False},
        }

        with patch.object(agent_mod.settings_mod.settings, "llm_provider", None), \
            patch.object(agent_mod.settings_mod.settings, "openrouter_model", "openrouter/free"), \
            patch.object(agent_mod.settings_mod.settings, "friendly_free_tier_mode", True), \
            patch("api.agent.auth_snapshot", return_value=snapshot), \
            patch("api.agent.require_provider_connected", return_value=None), \
            patch("api.agent.get_provider_cooldown_remaining", return_value=0), \
            patch("api.agent._throttle_llm_calls", return_value=None), \
            patch("api.agent._generate_json_once", return_value={"spoken": "ok", "changes": [], "actions": []}):
            provider, model, data = agent_mod._generate_json(system="system", user="user")

        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "openrouter/free")
        self.assertEqual(data["spoken"], "ok")

    def test_generate_json_uses_hosted_user_preferences_for_provider_and_model(self) -> None:
        snapshot = {
            "openrouter": {"connected": True},
            "gemini": {"connected": True},
        }

        with patch("api.agent.get_user_preferences", return_value=UserPreferencesRecord(profile_id="sb-user-123", llm_provider="gemini", gemini_model="gemini-3-flash-preview")), \
            patch.object(agent_mod.settings_mod.settings, "llm_provider", "openrouter"), \
            patch.object(agent_mod.settings_mod.settings, "openrouter_model", "x-ai/grok-4.3"), \
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

        self.assertEqual(provider, "gemini")
        self.assertEqual(model, "gemini-3-flash-preview")
        self.assertEqual(data["spoken"], "ok")

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
            client = TestClient(main_app)
            resp = client.post("/api/fs/list", json={"path": "."})

        self.assertEqual(resp.status_code, 401)
        self.assertIn("verified login", resp.text)


class ProjectTemplateRegressionTests(unittest.TestCase):
    def test_template_registry_renders_runnable_react_project(self) -> None:
        templates = list_project_templates()
        template_ids = {item["id"] for item in templates}

        self.assertIn("saas-dashboard", template_ids)
        self.assertIn("landing-pricing", template_ids)
        self.assertIn("admin-crud", template_ids)
        self.assertIn("ai-tool-app", template_ids)

        files = render_project_template(template_id="ai-tool-app", project_root="demo", project_name="Demo AI")

        self.assertIn("package.json", files)
        self.assertIn("index.html", files)
        self.assertIn("src/App.tsx", files)
        self.assertIn("src/main.tsx", files)
        self.assertIn("README.md", files)
        self.assertIn(".voiceide/memory/project.md", files)
        self.assertIn("react-router-dom", files["package.json"])
        self.assertIn("Template: AI Tool App", files[".voiceide/memory/project.md"])

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


class PatchApplyRegressionTests(unittest.TestCase):
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

        self.assertEqual(live_append_lines, ["appendAssistantLiveText(spokenChunk);"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

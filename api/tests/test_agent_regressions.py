from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.agent_intent import classify_agent_intent
from api.agent_mcp import MCPServerInfo, MCPToolCallResult, MCPToolInfo, discover_mcp_servers, execute_mcp_tool, suggest_mcp_actions
from api.agent_memory import retrieve_agent_memory
from api.agent_skills import detect_project_stack, resolve_agent_skills
from api.agent_tools import execute_local_tool
from api.auth_identity import AuthenticatedUser
from api.auth_policy import require_hosted_user
from api.hybrid import build_hybrid_seed
from api.main import _build_quality_checks, _extract_preview_snapshot_from_html, agent_capabilities, supabase_rag_status
from api.preferences import UserPreferencesRecord
from api.preferences_router import build_preferences_router
from api.secrets_store import get_provider_secret, has_provider_secret
from api.settings import load_settings
from api.settings_router import build_settings_router
from api.supabase_store import upsert_profile


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
        self.assertIn("mcp", caps["supports"]["tool_actions"])
        self.assertIn("shell", caps["supports"]["tool_actions"])
        self.assertIn("tool", caps["supports"]["tool_actions"])


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

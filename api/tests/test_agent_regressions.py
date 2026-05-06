from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.agent_intent import classify_agent_intent
from api.agent_mcp import MCPToolInfo, suggest_mcp_actions
from api.agent_memory import retrieve_agent_memory
from api.main import _build_quality_checks, _extract_preview_snapshot_from_html, agent_capabilities


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

# Appora Next Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Appora measurably closer to a professional coding agent by proving the tool loop, benchmark flow, browser evidence, database/Git safety, and UI quality through repeatable tests.

**Architecture:** Keep the current Appora linear runtime. Do not add LangGraph or another orchestration framework. Improve reliability by tightening tool-choice behavior, adding stronger evals, making benchmark results actionable, and documenting honest capability boundaries.

**Tech Stack:** FastAPI backend, Python unittest regression suite, React/Vite/TypeScript frontend, Playwright/Firefox preview audit, SQLite local DB tool, guarded Git tool, Aider benchmark adapter, Supabase readiness docs.

---

## Tomorrow Definition Of Done

By the end of June 11, 2026:

- Appora has one live benchmark report from the current runtime, not only unit tests.
- At least one failing benchmark/task weakness is converted into a regression test.
- The agent tool loop can demonstrate read -> edit/apply -> format/lint -> validate on a real sample project.
- README and docs still describe Appora honestly, without claiming top-tier parity.
- No `.tmp-*`, `node_modules`, secrets, or benchmark artifacts are committed.
- `npm run test:agent-regression` and `npx tsc -b` pass before push.

---

## Files To Touch

- Modify: `api/agent_runtime.py`
  - Improve tool-use instruction and tool-result handling if benchmark shows the model underuses tools.
- Modify: `api/agent_tools.py`
  - Tighten any unsafe/weak edge found in `database_client`, `format_lint`, or `git_manager`.
- Modify: `api/agent_benchmarks.py`
  - Improve internal live benchmark scoring/report output if needed.
- Modify: `api/aider_benchmarks.py`
  - Improve Aider adapter output parsing, preflight, or failure summaries.
- Modify: `api/tests/test_agent_regressions.py`
  - Add regression tests for every concrete bug found tomorrow.
- Modify: `scripts/preview-audit.mjs`
  - Only if browser evidence still misses visible problems.
- Modify: `README.md`
  - Only if capability status changes after benchmark evidence.
- Create: `docs/reports/agent-benchmark-report-2026-06-11.md`
  - Human-readable benchmark report with commands, model, pass/fail, failure patterns, and next fixes.

---

### Task 1: Clean Baseline Before Work

**Files:**
- Read only: `.gitignore`
- Read only: `README.md`
- Read only: `api/tests/test_agent_regressions.py`

- [ ] **Step 1: Confirm branch and dirty state**

Run:

```bash
git status -sb
git branch --show-current
```

Expected:

```text
## main...origin/main
main
```

If only `.tmp-agent-bench/...` files are dirty, leave them alone. They are local benchmark artifacts.

- [ ] **Step 2: Confirm ignored benchmark artifacts**

Run:

```bash
git check-ignore -v .tmp-agent-bench/latest-agent-benchmark.json .tmp-aider-test 2>/dev/null
```

Expected:

```text
.gitignore:...:.tmp-agent-bench*/
.gitignore:...:.tmp-aider-test/
```

- [ ] **Step 3: Run baseline tests**

Run:

```bash
npm run test:agent-regression
npx tsc -b
```

Expected:

```text
Ran 283 tests
OK
```

`npx tsc -b` should exit `0` with no output.

---

### Task 2: Run A Small Live Internal Agent Benchmark

**Files:**
- Modify if needed: `api/agent_benchmarks.py`
- Create: `docs/reports/agent-benchmark-report-2026-06-11.md`

- [ ] **Step 1: Ensure 9Router is still reachable**

Do not stop the router.

Run:

```bash
curl -sS http://127.0.0.1:20128/v1/models | head -c 400
```

Expected:

```text
{...
```

If this fails, start router without killing an existing one:

```bash
npm run router
```

- [ ] **Step 2: Run smoke benchmark**

Run:

```bash
npm run bench:agent:internal:live:smoke
```

Expected:

```text
...latest-agent-benchmark...
```

The command must produce or update `.tmp-agent-bench/latest-agent-benchmark.json`.

- [ ] **Step 3: Extract score and failure summary**

Run:

```bash
api/.venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path(".tmp-agent-bench/latest-agent-benchmark.json")
data = json.loads(p.read_text())
print(json.dumps({
  "ok": data.get("ok"),
  "summary": data.get("summary"),
  "scenarios": [
    {
      "id": s.get("id"),
      "score": s.get("score"),
      "ok": s.get("ok"),
      "missing": s.get("metrics", {}).get("missing_required_terms"),
      "forbidden": s.get("metrics", {}).get("matched_forbidden_terms"),
    }
    for s in data.get("scenarios", [])
  ],
}, indent=2))
PY
```

Expected:

```text
{
  "ok": ...
}
```

- [ ] **Step 4: Write benchmark report**

Create `docs/reports/agent-benchmark-report-2026-06-11.md` with this structure:

```markdown
# Appora Agent Benchmark Report - 2026-06-11

## Commands

- `npm run bench:agent:internal:live:smoke`

## Environment

- Router: 9Router local at `http://127.0.0.1:20128/v1`
- Model: record the exact model from benchmark JSON
- Workspace: `.tmp-agent-bench`

## Result

- Overall OK: true/false
- Scenario scores:
  - `task_tracker_ui`: score, pass/fail

## Observed Failures

- List concrete missing requirements or runtime failures.

## Fixes To Implement

- List only fixes backed by benchmark evidence.

## Follow-up Verification

- `npm run test:agent-regression`
- `npx tsc -b`
- rerun benchmark command
```

---

### Task 3: Convert One Benchmark Weakness Into A Regression Test

**Files:**
- Modify: `api/tests/test_agent_regressions.py`
- Modify only if test fails: `api/agent_runtime.py`, `api/agent_tools.py`, `api/agent_benchmarks.py`, or `scripts/preview-audit.mjs`

- [ ] **Step 1: Pick one concrete failure**

Use a specific benchmark failure, for example:

```text
Missing required terms: empty state, owner, priority
```

Do not fix vague impressions. Pick one measurable failure.

- [ ] **Step 2: Write failing regression test**

If the failure is shallow task coverage, add a test near the existing task-depth gate tests:

```python
def test_task_depth_gate_blocks_missing_priority_owner_empty_state_for_tracker(self) -> None:
    prompt = "Build a task tracker dashboard with task list, priority, owner, status, metrics summary, empty state, loading state, error state, and responsive layout."
    changes = [
        {
            "path": "src/App.tsx",
            "new_content": "export default function App(){ return <main><h1>Tasks</h1><p>Status dashboard</p></main> }",
        },
        {
            "path": "src/App.css",
            "new_content": "@media (max-width:720px){main{padding:12px}}",
        },
    ]

    issues = agent_runtime_mod._task_depth_gate_issues(prompt, changes)

    self.assertTrue(issues)
    self.assertIn("priority", issues[0].lower())
    self.assertIn("owner", issues[0].lower())
```

- [ ] **Step 3: Run test and verify RED**

Run:

```bash
api/.venv/bin/python -m unittest api.tests.test_agent_regressions.AgentToolsRegressionTests.test_task_depth_gate_blocks_missing_priority_owner_empty_state_for_tracker
```

Expected:

```text
FAIL
```

- [ ] **Step 4: Implement minimal fix**

If the test targets task depth, update `api/agent_runtime.py` in `_task_depth_gate_issues`:

```python
requirement_terms = [
    "overview", "incidents", "incident", "deployments", "deployment",
    "customers", "customer", "automation", "reports", "report",
    "search", "environment", "env", "health", "kpi", "kpis",
    "timeline", "pipeline", "sla", "table", "filters", "filter",
    "detail", "panel", "form", "validation", "success", "empty",
    "loading", "error", "responsive", "sidebar", "topbar",
    "workspace", "command center", "priority", "owner", "status",
    "metrics", "summary", "task list",
]
```

Adapt the exact code to the current helper, preserving existing passing tests.

- [ ] **Step 5: Run GREEN**

Run:

```bash
api/.venv/bin/python -m unittest api.tests.test_agent_regressions.AgentToolsRegressionTests.test_task_depth_gate_blocks_missing_priority_owner_empty_state_for_tracker
```

Expected:

```text
OK
```

---

### Task 4: Prove Tool Loop On A Real Sample Project

**Files:**
- Modify: `api/tests/test_agent_regressions.py`
- Modify if needed: `api/agent_runtime.py`
- Modify if needed: `api/agent_tools.py`

- [ ] **Step 1: Add a regression for tool-loop trace**

Add a test that simulates local tool actions:

```python
def test_tooling_node_promotes_applied_tool_changes_into_runtime_state(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp)
        project_dir = ws_root / "demo"
        (project_dir / "src").mkdir(parents=True)
        (project_dir / "src" / "App.tsx").write_text("export default function App(){ return <main>Old</main> }\\n", encoding="utf-8")

        ctx = prepare_agent_context(
            ws_root=ws_root,
            project_root="demo",
            user_input="replace old with ready",
            build_mode="full-agent",
            active_rel="src/App.tsx",
            preview_url=None,
        )
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
```

- [ ] **Step 2: Run test**

Run:

```bash
api/.venv/bin/python -m unittest api.tests.test_agent_regressions.AgentRuntimeContextRegressionTests.test_tooling_node_promotes_applied_tool_changes_into_runtime_state
```

Expected:

```text
OK
```

If import access or class location is wrong, place the test in the nearest existing class that already uses `_execute_tooling_node`.

---

### Task 5: Tighten Database And Git Safety

**Files:**
- Modify: `api/agent_tools.py`
- Modify: `api/tests/test_agent_regressions.py`

- [ ] **Step 1: Add SQL safety regression**

Add:

```python
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
```

- [ ] **Step 2: Add Git remote safety regression**

Add:

```python
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
```

- [ ] **Step 3: Run targeted tests**

Run:

```bash
api/.venv/bin/python -m unittest api.tests.test_agent_regressions.AgentToolsRegressionTests
```

Expected:

```text
OK
```

---

### Task 6: Browser Evidence Smoke Test

**Files:**
- Modify if needed: `scripts/preview-audit.mjs`
- Modify if needed: `api/main.py`
- Create/update: `docs/reports/agent-benchmark-report-2026-06-11.md`

- [ ] **Step 1: Confirm Firefox Playwright path**

Run:

```bash
test -x "$HOME/.cache/ms-playwright/firefox-1511/firefox/firefox" && echo firefox-ready
```

Expected:

```text
firefox-ready
```

- [ ] **Step 2: Run preview audit script against an active local preview**

If no preview server is running, start one in a separate shell:

```bash
npm run dev -- --host 127.0.0.1 --port 4187
```

Then run:

```bash
node scripts/preview-audit.mjs http://127.0.0.1:4187 --json
```

Expected:

```text
{"ok":...
```

- [ ] **Step 3: Record evidence**

Add to `docs/reports/agent-benchmark-report-2026-06-11.md`:

```markdown
## Browser Evidence Smoke

- Command: `node scripts/preview-audit.mjs http://127.0.0.1:4187 --json`
- Result: pass/fail
- Screenshot evidence: yes/no
- DOM snapshot evidence: yes/no
- Console errors: count
```

---

### Task 7: Final Verification And Push

**Files:**
- Modify: only files touched by the tasks above

- [ ] **Step 1: Check staged scope**

Run:

```bash
git status --short
git diff --stat
```

Expected:

```text
No .tmp-* files staged or committed.
```

- [ ] **Step 2: Full validation**

Run:

```bash
npm run test:agent-regression
npx tsc -b
git diff --check
```

Expected:

```text
Ran ... tests
OK
```

`npx tsc -b` and `git diff --check` should exit `0`.

- [ ] **Step 3: Commit**

Run:

```bash
git add README.md docs/reports/agent-benchmark-report-2026-06-11.md api/agent_runtime.py api/agent_tools.py api/agent_benchmarks.py api/aider_benchmarks.py api/main.py api/tests/test_agent_regressions.py scripts/preview-audit.mjs
git commit -m "Tighten Appora benchmark and tool evidence"
```

Only stage files that actually changed. Do not stage `.tmp-*`.

- [ ] **Step 4: Push**

Use the GitHub credentials from `Projects/key.txt` without printing the token.

Run:

```bash
git push origin main
```

Expected:

```text
main -> main
```

---

## Self-Review Checklist

- [ ] Plan uses concrete files and commands.
- [ ] Plan has no placeholder words or vague implementation gaps.
- [ ] Each task has a validation command.
- [ ] Benchmark evidence is separated from marketing claims.
- [ ] Secrets are never printed.
- [ ] Router is not killed during benchmark work.

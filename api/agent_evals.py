from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from api.agent import AgentSuggestion
from api.agent_runtime import run_agent_pipeline
from api.project_templates import list_project_templates, render_project_template


@dataclass(frozen=True)
class ClaraEvalScenario:
    id: str
    prompt: str
    template_id: str
    expected_files: tuple[str, ...]


CLARA_EVAL_SCENARIOS: tuple[ClaraEvalScenario, ...] = (
    ClaraEvalScenario(
        id="portfolio_polish",
        prompt="Bikin portfolio modern yang siap dipakai freelance designer, lengkap section work dan contact.",
        template_id="portfolio",
        expected_files=("src/pages/Home.tsx", "src/app.css"),
    ),
    ClaraEvalScenario(
        id="saas_dashboard",
        prompt="Build SaaS dashboard untuk founder non teknis, ada metrics, activity, empty state, dan settings-ready copy.",
        template_id="saas-dashboard",
        expected_files=("src/pages/Dashboard.tsx", "src/app.css"),
    ),
    ClaraEvalScenario(
        id="admin_crud",
        prompt="Bikin admin CRUD inventory yang searchable, table-first, ada status, create state, dan validasi copy.",
        template_id="admin-crud",
        expected_files=("src/pages/Dashboard.tsx", "src/app.css"),
    ),
    ClaraEvalScenario(
        id="ai_tool",
        prompt="Bikin AI tool app untuk generate campaign brief, ada prompt panel, result, history, dan usage state.",
        template_id="ai-tool-app",
        expected_files=("src/pages/Dashboard.tsx", "src/app.css"),
    ),
    ClaraEvalScenario(
        id="landing_pricing",
        prompt="Bikin landing page pricing yang conversion-ready, ada FAQ, CTA, pricing, dan copy yang jelas.",
        template_id="landing-pricing",
        expected_files=("src/pages/Home.tsx", "src/app.css"),
    ),
)


def _write_template_project(workspace: Path, scenario: ClaraEvalScenario) -> Path:
    project_root = scenario.id
    files = render_project_template(template_id=scenario.template_id, project_root=project_root, project_name=scenario.id.replace("_", " ").title())
    project_dir = workspace / project_root
    project_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project_dir


def _fake_clara_suggest(scenario: ClaraEvalScenario):
    calls = {"count": 0}
    primary_file = scenario.expected_files[0]
    css_file = scenario.expected_files[1]

    def fake_suggest(**_: Any) -> AgentSuggestion:
        calls["count"] += 1
        page_content = f'''export default function EvalPage() {{
  const rows = ["Planning", "Build", "Preview", "Ship"];
  return (
    <main className="evalSurface">
      <section className="evalHero">
        <p className="eyebrow">Clara Autopilot Eval</p>
        <h1>{scenario.id.replace("_", " ").title()}</h1>
        <p>Production-minded starter shaped from a rough non-technical prompt.</p>
        <button type="button" aria-label="Start build">Start Build</button>
      </section>
      <section className="evalGrid" aria-label="Workflow stages">
        {{rows.map((row) => <article key={{row}}><h2>{{row}}</h2><p>Clear state, accessible copy, and preview-ready UI.</p></article>)}}
      </section>
    </main>
  );
}}
'''
        css_content = """.evalSurface { display: grid; gap: 24px; padding: 32px; }
.evalHero { display: grid; gap: 12px; max-width: 760px; }
.evalHero h1 { font-size: clamp(2rem, 6vw, 4rem); margin: 0; }
.evalGrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
.evalGrid article { border: 1px solid rgba(148, 163, 184, 0.28); border-radius: 12px; padding: 16px; }
"""
        changes = [
            {"path": primary_file, "new_content": page_content},
            {"path": css_file, "new_content": css_content},
        ]
        if calls["count"] >= 3:
            changes[1]["new_content"] = css_content + ".evalHero button { width: fit-content; min-height: 40px; }\n"
        return AgentSuggestion(
            spoken="Clara sudah bikin surface yang preview-ready dan bisa dilanjutkan.",
            log="eval implementation: generated files and refinement",
            changes=changes,
            actions=[{"type": "shell", "command": "npm run build", "reason": "Validate generated app before handoff."}],
        )

    return fake_suggest


def run_clara_contract_eval() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        for scenario in CLARA_EVAL_SCENARIOS:
            _write_template_project(workspace, scenario)
            req = SimpleNamespace(
                input=scenario.prompt,
                mode="type",
                active_file="",
                selection=None,
                current_content=None,
                open_files=[],
                project_root=scenario.id,
                build_mode="full-agent",
                preview_url=None,
                editor_status=None,
                asset_paths=[],
            )
            with patch("api.agent_runtime.suggest", side_effect=_fake_clara_suggest(scenario)), \
                patch("api.agent_runtime.settings_mod.settings.friendly_free_tier_mode", True), \
                patch("api.agent_runtime.settings_mod.settings.agent_refinement_mode", "auto"):
                output = run_agent_pipeline(req, ws_root=workspace)

            trace = output.get("trace") if isinstance(output.get("trace"), dict) else {}
            changes = output.get("changes") if isinstance(output.get("changes"), list) else []
            actions = output.get("actions") if isinstance(output.get("actions"), list) else []
            local_tools = trace.get("local_tools_used") if isinstance(trace.get("local_tools_used"), list) else []
            verification = trace.get("verification") if isinstance(trace.get("verification"), list) else []
            failed_verification = [item for item in verification if isinstance(item, dict) and not item.get("ok", True)]
            changed_paths = {str(item.get("path") or "") for item in changes if isinstance(item, dict)}
            expected_paths = {path for path in scenario.expected_files} | {f"{scenario.id}/{path}" for path in scenario.expected_files}
            ok = (
                len(changes) >= 2
                and all(any(candidate in changed_paths for candidate in {path, f"{scenario.id}/{path}"}) for path in scenario.expected_files)
                and any(isinstance(item, dict) and item.get("type") == "shell" for item in actions)
                and len(local_tools) >= 1
                and not failed_verification
            )
            results.append({
                "id": scenario.id,
                "ok": ok,
                "changes": len(changes),
                "actions": len(actions),
                "expected_paths": sorted(expected_paths),
                "changed_paths": sorted(changed_paths),
                "local_tools": [item.get("tool") for item in local_tools if isinstance(item, dict)],
                "passes": trace.get("passes"),
                "failed_verification": failed_verification,
            })
    return {"ok": all(item["ok"] for item in results), "scenarios": results}


def validate_template_registry() -> dict[str, Any]:
    templates = list_project_templates()
    required = {"package.json", "index.html", "src/main.tsx", "src/App.tsx", "src/app.css", "README.md", ".voiceide/memory/project.md"}
    results: list[dict[str, Any]] = []
    for template in templates:
        template_id = str(template.get("id") or "")
        files = render_project_template(template_id=template_id, project_root=template_id, project_name=str(template.get("name") or template_id))
        package_json = json.loads(files.get("package.json") or "{}")
        scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
        missing = sorted(required - set(files))
        ok = not missing and "dev" in scripts and "build" in scripts
        results.append({
            "id": template_id,
            "ok": ok,
            "file_count": len(files),
            "missing": missing,
            "has_dev_script": "dev" in scripts,
            "has_build_script": "build" in scripts,
        })
    return {"ok": all(item["ok"] for item in results), "templates": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Appora offline agent reliability evals.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    args = parser.parse_args()

    result = {
        "ok": True,
        "clara_contract": run_clara_contract_eval(),
        "template_registry": validate_template_registry(),
    }
    result["ok"] = bool(result["clara_contract"]["ok"] and result["template_registry"]["ok"])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Appora Agent Eval:", "PASS" if result["ok"] else "FAIL")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

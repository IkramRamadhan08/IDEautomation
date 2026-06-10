# Appora Agent Benchmark Report - 2026-06-11

## Commands

- `npm run bench:agent:aider -- --run --num-tests 6 --threads 1 --timeout 1200 --json --run-name appora-aider-varied-20260610-070132 --keywords word-search,robot-simulator,tree-building,ledger,rest-api,sgf-parsing`
- `node scripts/preview-audit.mjs http://127.0.0.1:4187 --json`

## Environment

- Router: 9Router local at `http://127.0.0.1:20128/v1`
- Model: `openai/appora`
- Workspace: `.tmp-aider-benchmark`
- Run directory: `.tmp-aider-benchmark/aider/tmp.benchmarks/2026-06-10-00-01-59--appora-aider-varied-20260610-070132`
- Docker access: local Docker via sudo wrapper using credentials from `Projects/key.txt`; secrets were not printed.

## Result

- Overall OK: false
- Aider Polyglot varied smoke:
  - Test cases: 6
  - Pass rate try 1: 50.0%
  - Pass rate try 2: 66.7%
  - Pass rate try 3: 66.7%
  - Well-formed cases: 83.3%
  - Malformed responses: 1
  - User asks: 2
  - Error outputs: 1

## Scenario Scores

- `javascript/word-search`: pass, outcomes `[true]`
- `javascript/rest-api`: pass, outcomes `[true]`
- `python/tree-building`: repaired by try 2, outcomes `[false, true]`
- `java/tree-building`: pass, outcomes `[true]`
- `java/sgf-parsing`: fail, outcomes `[false, false, false]`
- `go/robot-simulator`: fail, outcomes `[false, false, false]`

## Observed Failures

- `java/sgf-parsing` produced one malformed response, two user asks, one error output, and failed all three tries.
- `go/robot-simulator` failed all three tries. The final compiler errors show the agent guessed field names and numeric types instead of reading the exercise-defined API contract closely enough.
- A prior smoke attempt reused an existing run name and the harness printed `Prior runs ... use --new or name one explicitly`; the adapter previously treated this as OK because the process exited 0 without stats.

## Fixes Implemented

- Added Aider adapter support for `--languages` and `--new` so benchmark runs can be intentionally varied and can force fresh dated directories.
- Added regression coverage that reused Aider run names are reported as failures instead of false OK.
- Added regression coverage for varied benchmark command generation with explicit keywords/languages.
- Added tool-loop regression proving applied local tool changes are promoted into runtime `changes`.
- Added SQL query-mode write blocking and Git remote permission regressions.

## Browser Evidence Smoke

- Command: `node scripts/preview-audit.mjs http://127.0.0.1:4187 --json`
- Result: pass with warnings
- Screenshot evidence: yes
- DOM snapshot evidence: yes
- Console errors: 8
- Notes: errors were CORS/API calls to `http://127.0.0.1:8787` while only the Vite frontend preview was running.
- Desktop overflow: false
- Mobile overflow: false

## Follow-up Verification

- `npm run test:agent-regression`
- `npx tsc -b`
- `git diff --check`
- Rerun a varied Aider benchmark with a fresh `--run-name` or `--new`.

# Appora Runtime V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Appora's custom agent runtime more professional without adding external orchestration frameworks, Agents SDK orchestration, or sandbox/approval work in this slice.

**Architecture:** Keep the explicit linear `AgentDriver` and add Codex-style operational boundaries around it: a run controller, compact run ledger, internal hook events, and a read-only scout for complex tasks. The browser/app layer continues consuming the existing `trace` shape with additional optional fields.

**Tech Stack:** Python FastAPI backend, existing `api.agent_runtime` pipeline, existing local tools, unittest regression suite, Vite/TypeScript frontend build.

---

### Task 1: Runtime Controller And Ledger

**Files:**
- Modify: `api/agent_runtime.py`
- Test: `api/tests/test_agent_regressions.py`

- [x] Add a small `AgentRunController` dataclass with bounded driver steps, tool calls, and LLM calls.
- [x] Add compact ledger helpers that append phase/kind/status/detail rows to `PreparedAgentContext`.
- [x] Expose `run_controller` and `run_ledger` in final trace.

### Task 2: Runtime Hooks

**Files:**
- Modify: `api/agent_runtime.py`
- Test: `api/tests/test_agent_regressions.py`

- [x] Add internal hook events for driver phase entry, tool calls, and stop/finalize.
- [x] Keep hooks no-op and in-process for now; do not execute user commands or approval policy in this slice.

### Task 3: Read-Only Scout

**Files:**
- Modify: `api/agent_runtime.py`
- Test: `api/tests/test_agent_regressions.py`

- [x] Add a bounded scout that runs a few read-only local tools for complex/full-agent tasks.
- [x] Feed only compact scout summaries back into context and trace.
- [x] Keep all scout tools read-only and project-scoped.

### Task 4: Verification

**Files:**
- Modify: `api/tests/test_agent_regressions.py`

- [x] Add targeted tests for controller stop state, trace ledger, hook events, and scout results.
- [x] Run targeted tests, full agent regression, TypeScript check, and production build.

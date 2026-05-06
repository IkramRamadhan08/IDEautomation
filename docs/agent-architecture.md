# Agent Architecture

Voice IDE agent is being reset around explicit boundaries instead of procedural orchestration.

## Runtime boundaries

1. **Graph runtime**
   - owns state transitions
   - decides which phase runs next
   - keeps refinement optional and explicit

2. **Intent boundary**
   - classifies whether the user is giving a build command, having a conversation, or mixing both
   - prevents the app-builder agent from editing files during normal chat/status checks
   - keeps spoken explanation separate from explicit implementation work

3. **Memory / RAG**
   - **short-term memory**: recent agent runs per session/user plus project-scoped short memory for the same user
   - retrieval is biased toward the same interaction kind so build memories do not swamp conversational context and vice versa
   - **long-term memory**: durable project docs like README, PRD, docs, project memory notes
   - long-term docs are chunked for retrieval, and when Supabase is configured the chunk store can sync to `agent_memory_chunks` for a shared retrieval backend
   - if Supabase is configured but `public.agent_memory_chunks` is missing or unreachable, the runtime now surfaces an explicit warning and falls back to local chunks honestly
   - retrieval is injected into agent context, not hidden in random helper code

4. **Skill registry**
   - built-in delivery skills
   - optional custom project skills from `.voiceide/skills/*.md`
   - can detect installed project stack signals like component libraries or browser tooling from `package.json`
   - matched by request/context before drafting

5. **MCP registry + execution loop**
   - discovers declared MCP servers from workspace or project config
   - surfaces capability boundaries to the agent runtime
   - can execute MCP tool calls during the graph loop, then feed real tool results back into the next draft pass

6. **Browser validation boundary**
   - preview audit can fall back to static HTML inspection
   - when Playwright + Node runtime are available, preview audit can inspect the live DOM in a real browser context
   - browser-backed validation should stay honest about fallbacks and runtime gaps

## Current goal

Make the agent a trustworthy app-builder, not just a code chatbot.

### North star

Voice IDE agent should:
- distinguish chat from build work
- operate inside bounded runtime/tool limits
- understand the project's existing stack
- validate outcomes through the browser when possible
- expose per-run trace for retrieved memory, chosen skills, and MCP/tool usage
- report capabilities honestly instead of pretending unfinished runtime features already exist

### Delivery target

Make the agent more reliable by separating:
- intent classification, including read-only inspection / audit mode
- context building
- memory retrieval
- skill resolution
- MCP capability discovery
- drafting
- refinement
- finalization
- run memory persistence
- browser-backed preview validation

## Current limits

- MCP now has a bounded execution loop, but still depends on valid project/workspace MCP config
- RAG is lexical/project-local for now, not embedding-based
- headless browser and WebContainer are surfaced honestly as capability flags, but are not yet generalized runtimes unless the project/environment actually provides them
- frontend still has some state coupling that should be moved into dedicated controllers/hooks

## Why this is better

Because the agent stops being a giant request handler with side-effects everywhere.
Each layer can be reasoned about, debugged, tested, and upgraded on its own.

# Agent Architecture

Voice IDE agent is being reset around explicit boundaries instead of procedural orchestration.

## Runtime boundaries

1. **Graph runtime**
   - owns state transitions
   - decides which phase runs next
   - keeps refinement optional and explicit

2. **Memory / RAG**
   - **short-term memory**: recent agent runs per session/user
   - **long-term memory**: durable project docs like README, PRD, docs, project memory notes
   - retrieval is injected into agent context, not hidden in random helper code

3. **Skill registry**
   - built-in delivery skills
   - optional custom project skills from `.voiceide/skills/*.md`
   - matched by request/context before drafting

4. **MCP registry**
   - discovers declared MCP servers from workspace or project config
   - surfaces capability boundaries to the agent runtime
   - execution loops can be added later without collapsing the rest of the architecture

## Current goal

Make the agent more reliable by separating:
- context building
- memory retrieval
- skill resolution
- MCP capability discovery
- drafting
- refinement
- finalization
- run memory persistence

## Current limits

- MCP discovery exists before full MCP tool-execution loops
- RAG is lexical/project-local for now, not embedding-based
- frontend still has some state coupling that should be moved into dedicated controllers/hooks

## Why this is better

Because the agent stops being a giant request handler with side-effects everywhere.
Each layer can be reasoned about, debugged, tested, and upgraded on its own.

# Agent Notes

## Project Direction

`xueli` is intentionally a lightweight project.

The goal is not to become a heavyweight all-in-one bot platform. The goal is to evolve into:

`a lightweight conversational core + thin multi-platform adapters + an open API access layer`

## Current Priorities

1. Keep the runtime core small and readable.
2. Continue tightening the boundary between standard platform models and legacy OneBot models.
3. Preserve the unified conversation planning pipeline for both private and group chat.
4. Keep `PromptPlan` as the contract between planning and reply generation.
5. Strengthen higher-level integration tests before adding new large concepts.

## Non-Goals For Now

1. Do not build a heavy plugin runtime yet.
2. Do not split into many repositories or processes unless there is a concrete need.
3. Do not overbuild the WebUI.
4. Do not add platform-specific business logic into the core unless unavoidable.

## Architecture Rule Of Thumb

When adding a new feature, ask:

1. Is this core logic or adapter logic?
2. Would this still make sense if QQ disappeared tomorrow?
3. Can a future HTTP/WebSocket/software integration reuse this unchanged?

If the answer is no, the code probably belongs in an adapter boundary instead of the core.

## Implementation Strategy

1. Keep current QQ/NapCat behavior working.
2. Introduce platform-agnostic models alongside existing OneBot models.
3. Migrate internal logic gradually instead of rewriting everything.
4. Prefer small compatibility-preserving changes.
5. Prefer converging on neutral names like `conversation_*` instead of leaving `group_*` compatibility layers around.

## Current Architecture Notes

1. `BotRuntime` is the runtime facade and `MessageHandler` is the high-level orchestration layer.
2. `ConversationPlanner` decides `reply / wait / ignore` and can emit a structured `PromptPlan`.
3. `ReplyPipeline` acts more like a prompt compiler than a static prompt builder.
4. Adapters are created through `src/adapters/registry.py`, so API access is a first-class adapter path.
5. Current follow-up work should favor naming convergence, service extraction, and integration coverage over broad new feature branches.

# Agent Notes

## Project Direction

`xueli` is intentionally a lightweight project.

The goal is not to become a heavyweight all-in-one bot platform. The goal is to evolve into:

`a lightweight conversational core + thin multi-platform adapters + an open API access layer`

## Current Priorities

1. Keep the core small and readable.
2. Remove implicit QQ-only assumptions from the core.
3. Standardize inbound events and outbound actions.
4. Treat API access as another adapter, not a separate business path.
5. Add only the minimum runtime and test structure needed to keep the system reliable.

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

## First Active Workstream

1. Add platform-agnostic event and action models.
2. Add a OneBot-to-standard-event normalizer.
3. Start routing internal session identity through the standard model.
4. Back it with focused tests.

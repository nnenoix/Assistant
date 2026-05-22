# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## Project context (READ FIRST in a fresh session)

This is the **Google Workspace Chat Agent** — local Python FastAPI + pywebview
app driving Drive/Sheets/Docs/Slides/Gmail/Calendar/Apps Script/Forms/Tasks/
Contacts via Claude. 226 tools registered. 365 unit tests passing.

**Before doing anything substantive, read `docs/HANDOFF.md`** — current state
across 13 completed phases + Phase 14 plan (production-scale 100×7M chars).

**Phase 14 is approved but not yet implemented.** Tasks #33-#40 in the task
list describe sub-phases. Detailed plan in `docs/PHASE_14_PLAN.md`.

**Key environment for testing:**
```powershell
$env:LIVE_GOOGLE_TESTS = "1"  # enable integration tests against egor.titt@gmail.com / CLAUDE-TEST/
```

**Stress test bottleneck found:** `verify_claim` serial loop = p50 47s for 50 refs.
Phase 14D (parallelization) is the cheapest, highest-leverage start point.

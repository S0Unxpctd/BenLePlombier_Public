# Synthetic Test Fixtures for System Prompt v2

These are **SYNTHETIC** test fixtures, not real customer quotes. They serve as structural-invariant tests for the system prompt refactor (v2).

## Purpose

Each fixture validates that the prompt v2:
- Respects the line-item budget (5–7–10 caps)
- Detects and flags incoherent scenarios
- Applies price calibration from the reference grid
- Produces minimal flags

## Usage

These fixtures can be used in:
- Unit tests for prompt coherence checking
- Prompt regression tests after future updates
- Manual validation of the v2 response format

## Warning for Maintainers

If you loosen a threshold (e.g., increasing max lignes from 7 to 12, or removing the coherence check), you MUST:
1. Re-run the fixtures against the updated prompt
2. Verify expectations still hold or update them explicitly
3. Get sign-off from the product team before merging

Do NOT silently slip threshold changes; they change the shape of the output that the user sees.

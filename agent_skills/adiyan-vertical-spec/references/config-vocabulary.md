# Adiyan configuration vocabulary (v1, scoped to business-persona use)

This is the complete, exact list of fields the vertical-spec YAML is allowed to set. Every value here is a real, currently-live field in Adiyan's config store - none of it is aspirational. Do not invent a field name that isn't listed here; Adiyan validates every key against its actual configuration and rejects unrecognized ones rather than silently ignoring them.

Adiyan has over 80 configurable fields across 20+ internal agents in total. This reference deliberately covers only the ones relevant to shaping a business's customer-facing persona - things like scheduler timing, book-reading voice settings, or internal agent-to-agent URLs are out of scope for this skill and must never appear in generated output.

## `orchestrator` (writes the final reply text a customer sees)

| Field | Type | What it controls |
|---|---|---|
| `summon_phrase` | string | The word/phrase a customer's message must contain (anywhere, case-insensitive) for Adiyan to respond at all. Default is `@adiyan`. Keep it short, no spaces work best. |
| `card_description` | string | One-sentence public description of what this Adiyan number does - shown in its own discovery metadata. |
| `business_persona_context` | string (multi-line) | Free-text instructions, written directly TO Adiyan, describing how to behave toward this business's customers - tone, hard rules, boundaries, fallback phrasing. This is the main lever the interview produces. |

## `analysis` (does the actual reasoning behind an answer)

| Field | Type | What it controls |
|---|---|---|
| `strict_grounding` | boolean | If `true`, Adiyan will only state things a document it actually retrieved says - it says "I don't know" rather than guess. If `false`, it can use its own judgment, estimate, and reason more freely. Default is `true`. |
| `business_persona_context` | string (multi-line) | Same content as orchestrator's field above - both need it, since one agent reasons about the answer and the other writes the customer-facing reply. Always set both to the identical value. |

## `scheduler`, `journal`, `adiyan_reader`, `config_agent`, `micro_habits` (each generates its own customer-facing content)

| Field | Type | What it controls |
|---|---|---|
| `business_persona_context` | string (multi-line) | Same content as orchestrator's field above. Set it here too whenever a customer's request could reach one of these agents directly - `scheduler` composes reminder text and resolves schedule requests, `journal` writes reflection prompts, `adiyan_reader` generates book-reading quiz questions and speech-formatted text, `config_agent` and `micro_habits` generate their own replies the same way orchestrator and analysis do. Not every business needs all five set - only set the ones a real customer conversation could plausibly reach for this business (e.g. a coach whose customers ask for reminders should set `scheduler` too; a restaurant that never touches scheduling can leave it out). When in doubt, set the same value everywhere the field is accepted - it's free-text and costs nothing to repeat. |

`memory` has no `business_persona_context` field and never will under this mechanism - it only stores and retrieves conversation history, and never generates any text an agent or customer sees, so there is nothing for a persona to shape there.

## Not yet supported by this skill (do not attempt to set these)

These exist in Adiyan's config store but are intentionally out of scope for a business-persona spec - either too risky to auto-generate (they're full prompt templates with required `{placeholders}` that break the whole stage if malformed) or irrelevant to a customer-facing persona (internal timing, ports, agent URLs):

- Any field ending in `_prompt_template` (e.g. `humanize_prompt_template`, `decide_next_step_prompt_template`, `resolve_schedule_prompt_template`) - these contain required placeholders like `{original_message}` that Adiyan's own code fills in by name; a rewritten version missing one would break that stage entirely.
- `host`, `port`, `mcp_servers`, any `*_url` field - infrastructure, never business content.
- `strict_grounding` is only settable under `analysis` - no other agent exposes it.
- `skill_*_description` / `skill_*_examples` pairs - these control Adiyan's internal message-routing logic. Changing them risks breaking which requests get handled at all, not just how they sound.

If a business owner's answer seems to call for one of these (e.g. "I want it to always negotiate down to a floor price during price talks" - which really wants to change `decide_next_step_prompt_template`'s actual reasoning, not just its tone), fold the *intent* into `business_persona_context` as a plain instruction instead ("Never agree to a price below ₹X without checking with a human first") rather than attempting to rewrite the underlying prompt template. The persona-context field is read by the same reasoning step, so a clearly-stated rule there is followed even though it isn't rewriting the template's own wording.

## `vertical_id`

Not itself a field under an agent - the top-level identifier for this whole business profile. Lowercase letters, numbers, and hyphens only, no spaces, no leading/trailing hyphen (e.g. `vizag-travel-co`, `sunrise-bakery`). This is how Adiyan tells one business's overrides apart from another's, and from the platform defaults every other Adiyan deployment uses.

---
name: adiyan-vertical-spec
description: Interviews a business owner about their business, tone, and customer-facing rules, then produces a single vertical-config YAML file for Adiyan (a local, WhatsApp-based AI agent) to load. Use when a business owner wants Adiyan to act as their business's customer-facing assistant - answering, negotiating, and replying in that business's own voice and rules, rather than as a generic assistant.
license: MIT
metadata:
  author: bharani
  target-platform: adiyan
  spec-version: "1"
---

# Adiyan Vertical Spec

## What this produces

A single YAML file the business owner uploads to their Adiyan WhatsApp number. Adiyan reads it, creates a "vertical" (an isolated business-configuration profile), and makes it live - immediately reachable by its own wake phrase (`summon_phrase`), alongside the plain `@adiyan` default and any other business vertical already uploaded. Multiple businesses can run on the same Adiyan number at once this way; a customer's message is routed to whichever vertical's own phrase it actually contains. The owner's own messages to Adiyan are unaffected regardless of which phrase they use; only customer-facing replies change.

Each vertical needs its own, genuinely unique `summon_phrase` - Adiyan refuses to apply a spec whose phrase is already used by another live vertical, so double-check question 2's answer isn't a phrase already claimed (e.g. by asking the owner, or checking with `get_active_vertical` if you have access to the deployment).

Read `references/config-vocabulary.md` before drafting the YAML - it lists exactly which fields exist and what each one actually controls. **Never invent a field name that isn't in that reference.** Adiyan will reject anything it doesn't recognize.

## The interview

Ask these one at a time, in a natural conversational tone - don't dump all eight at once. Wait for each answer before moving to the next, and ask a natural follow-up if an answer is too vague to use (e.g. "tone: nice" needs a follow-up; "warm but efficient, we're busy during peak season" doesn't).

1. **What's the business, in a sentence or two?** What do you sell or do, and where are you based? (This shapes how Adiyan introduces itself and what currency/local context it assumes.)

2. **What should customers call your assistant?** E.g. "@VizagTravel" instead of the default "@adiyan" - this is the exact word/phrase a customer's message must contain for it to reply. Keep it short, no spaces work best.

3. **What's the tone?** Warm and casual, formal and professional, or persuasive and sales-forward? Give one real example line if you can - a phrase you'd actually want it to say.

4. **Strict facts only, or can it use judgment?** Should the assistant ONLY ever state things a document you've uploaded actually says (safest, but it'll say "I don't know" more often), or can it also estimate, recommend, and use general reasoning even without a document backing every claim?

5. **Any hard rules it must always follow?** E.g. "never quote below ₹3000 for a 2-day package," "always offer a callback if the customer seems upset," "never mention competitor names." List as many as matter - these become non-negotiable behavior, not suggestions.

6. **What should it say when it genuinely doesn't know something?** In your own brand's voice - e.g. "Let me check with the team and get back to you" rather than a generic "I don't have that information."

7. **Anything it should never do or say?** Boundaries matter as much as capabilities - e.g. never discuss refund exceptions over chat, always escalate legal questions to a human.

8. **A short unique ID for your business** - lowercase letters, numbers, and hyphens only (e.g. `vizag-travel-co`). If they don't have one, propose one from the business name and confirm it.

9. **Would customers ever ask this number for a reminder/scheduling, a reflective journaling prompt, or a book being read aloud?** Most businesses only need the core assistant (covered by questions 1-8) - skip this unless the owner's use case plausibly involves one of those. If unsure, default to setting `business_persona_context` everywhere it's accepted anyway; it's free-text and costs nothing to repeat.

## Producing the output

Once the interview is answered, write the YAML using this exact shape - do not add, rename, or restructure keys beyond what's shown. `orchestrator` and `analysis` are always present; the other five agents are optional, added only per question 9:

```yaml
vertical_id: <the id from question 8>
business_name: <from question 1>
generated_at: <today's date, ISO format>

agents:
  orchestrator:
    constants:
      summon_phrase: "<the wake phrase from question 2, lowercase>"
      card_description: "<one sentence describing the business, for Adiyan's own agent card>"
      business_persona_context: |
        <a well-written paragraph combining questions 1, 3, 5, 6, 7 - written as an
        instruction TO Adiyan, second person, e.g. "You are the assistant for
        Vizag Travel Co, a budget travel agency in Vizag. Speak warmly but stay
        efficient. Never quote below ₹3000 for a 2-day package. If you don't know
        something, say 'Let me check with the team and get back to you' - never
        guess. Never discuss refund exceptions in chat; escalate those to a human.">

  analysis:
    constants:
      strict_grounding: <true or false, from question 4>
      business_persona_context: |
        <the SAME paragraph as orchestrator's above - both agents need it, since
        one reasons and the other writes the final reply>

  # Optional, only when question 9 says a touchpoint applies - each block is
  # identical in shape, just business_persona_context on its own:
  scheduler:
    constants:
      business_persona_context: |
        <the SAME paragraph as orchestrator's above>

  journal:
    constants:
      business_persona_context: |
        <the SAME paragraph as orchestrator's above>

  adiyan_reader:
    constants:
      business_persona_context: |
        <the SAME paragraph as orchestrator's above>

  config_agent:
    constants:
      business_persona_context: |
        <the SAME paragraph as orchestrator's above>

  micro_habits:
    constants:
      business_persona_context: |
        <the SAME paragraph as orchestrator's above>
```

Rules for filling this in:
- `business_persona_context` must read as direct instructions to Adiyan, not a description of the business FOR a human reader. Bad: "Vizag Travel Co is warm and helpful." Good: "You are warm and helpful. Never say X. Always do Y."
- Keep it under ~200 words. This gets read on every single customer message - long, rambling instructions cost real money and slow every reply down.
- `strict_grounding: true` if the owner wants to minimize the assistant saying anything not backed by an uploaded document. `false` if they're fine with it using judgment/estimates.
- Every hard rule from question 5 and every boundary from question 7 must actually appear somewhere in `business_persona_context` - don't silently drop one because it didn't fit neatly.
- `scheduler`, `journal`, `adiyan_reader`, `config_agent`, `micro_habits` only ever get `business_persona_context` - never `strict_grounding`, `summon_phrase`, or `card_description`, which don't exist on those agents. See `references/config-vocabulary.md` for the full field list per agent.

## Customer records - automatic, not a spec field

The moment this vertical goes live, Adiyan starts keeping a private record for every one of this business's customers - what they've asked about, expressed interest in, mentioned wanting. This needs **no YAML field and no interview question** - it isn't something the spec configures, it just happens once the vertical exists, the same way `business_persona_context` starts applying the moment the spec is uploaded.

There is exactly one thing the owner can do that the spec itself can't: set a fact on a specific customer's record that only the owner should control - a payment confirmed, a subscription marked active, an order confirmed. Never suggest phrasing that claims Adiyan infers these automatically; it deliberately never does, so a customer can't talk their own record into a false state. The one real command for this, always sent under the vertical's own summon phrase:

```
<summon_phrase> mark <phone number> as <fact> for <field>
```

e.g. `@marinaspice mark 9198765432 as paid for the tiffin plan`, or `@ascentcoach set 9198765432's subscription to active`. It always needs the customer's real phone number, not just their name.

## Handing it back

Give the business owner the complete YAML in a fenced code block, plus one plain-English sentence confirming what it does: "This tells Adiyan to answer your customers as [business name] - warm tone, [key rule], and to say '[fallback line]' when it doesn't know something. Upload this file to your Adiyan WhatsApp number to activate it."

Also tell them, in the same handoff, that Adiyan will start keeping a private record of each customer automatically, and give them one real example of the owner-only command above, using their own business's own wake phrase - this is the one thing about customer records they actually need to know, everything else is automatic.

If they want it as a downloadable file rather than a chat code block, that's fine - the content is what matters, not the container. A `.yaml` file, a `.txt` file, or a PDF with the same YAML text inside all work equally well once it reaches Adiyan - Adiyan reads the text content, not the file format.

# Design question: deterministic trip-budget calculation from a bank statement

## Context

I'm building a WhatsApp-based travel-planning assistant. A customer uploads a
PDF of their bank statement (OCR'd into plain text already - I have that part
solved). The assistant also knows: family size, destination, travel dates,
and real flight/hotel prices from live search APIs.

I need the assistant to tell the customer "here's what you can actually
afford to spend on this trip" - a real number, grounded in their real bank
statement.

## What I have today, and why it's not good enough

Right now, an LLM just reads the raw OCR'd statement text once, as part of a
much bigger prompt, and writes a free-text "budget_signal" string describing
what it noticed (e.g. "statement shows ~$X salary credit and ~$Y average
expenses"). In practice this is:

- **Not deterministic.** The same statement can produce a slightly different
  read on different calls - different numbers, different framing, sometimes
  it misses the salary line entirely.
- **Not a real number.** It's prose, not a structured, usable budget figure
  the rest of the system can do arithmetic with (e.g. "compare this number to
  the real flight+hotel total and tell the customer if they're over or under").
- **Expensive and non-reusable.** The full raw statement text gets re-sent as
  part of the prompt on every single conversational turn (the assistant
  re-infers the whole customer profile from scratch each message), so this
  unstructured, unreliable extraction is also the single biggest token cost
  in the whole flow.

## My two hard constraints

1. **Deterministic.** The same statement, reprocessed, must produce the same
   budget number every time - no re-rolling a different answer because an LLM
   call came back phrased differently today. I need to be able to trust and
   audit this number.
2. **Token-cost-conscious.** I'm actively deciding between a few LLM backends
   partly based on token cost per customer interaction. Whatever this design
   does should not multiply that cost - ideally it should reduce it versus
   today's "re-read the whole statement every turn" approach.

## The actual input I'm working with

- Bank statement PDFs vary wildly in format across banks and countries - no
  guaranteed column layout, no guaranteed labels, sometimes scanned images
  that went through OCR and have minor garbling.
- A typical statement has: a running list of dated transactions (description,
  debit/credit amount, running balance), usually covering 1-12 months.
- There's no guarantee a line is explicitly labeled "salary" or "rent" -
  sometimes it's a company name, sometimes a vague reference code.

## What I actually want designed

1. **A parsing/extraction approach** that turns "messy OCR'd statement text,
   unknown bank format" into structured numbers (e.g. monthly income, monthly
   expenses, or a full categorized transaction list) - reliably, the same way
   every time for the same input. I'm open to: a one-time LLM extraction call
   (just once per uploaded document, with the result cached/reused rather
   than re-run every conversational turn) that outputs a strict schema rather
   than prose; a rules/regex-based parser; or a hybrid. I want the tradeoffs
   explained, not just one answer asserted.
2. **A deterministic trip-budget algorithm** that takes those structured
   numbers (income, expenses, maybe a savings/surplus figure) plus trip
   inputs (family size, number of nights, how many months until travel) and
   produces one real affordability figure or range - the actual math, spelled
   out, not "have an LLM eyeball it and guess a number."
3. Where you'd put the one LLM call (if any) versus where you'd insist on
   plain deterministic code, given both constraints above.

Please give me a concrete design, not a survey of generic best practices -
I want something I could actually hand to an engineer to build.

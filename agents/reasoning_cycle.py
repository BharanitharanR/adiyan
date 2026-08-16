"""
The named reasoning cycle inside LLMAgent: Hermes (triage) -> Prometheus (plan) ->
Pythia (clarify) / Hephaestus (tools, gate only) -> Calliope (draft) -> Momus (skeptic).

Why this exists: a single LLM call given both "answer factual questions plainly" and
"always format coaching responses as Actionable Step + Probing Question" will follow
the mechanical formatting rule over the conditional one - happened for real (asking
"what is the crux of [a book]" got forced into the coaching template). Splitting into
focused stages removes the conflict structurally: whichever prompt Calliope drafts
with only ever contains the instructions relevant to that turn.

Every stage is independently toggleable and independently configured (model/
temperature/timeout/prompt_template - config/database.py's agent_configs table), all
OFF by default. If a stage is disabled at the point it's needed, its behavior falls
back to the safest/cheapest option (see each function's docstring) rather than the
turn failing - Hermes disabled or "quick" skips the cycle entirely, which is exactly
today's original single-call LLMAgent behavior; that's the safety net every disable
combination ultimately reduces to.
"""
import json
import logging
import re
import asyncio
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger('ReasoningCycle')

PLAN_MENU = ['ask_clarifying_question', 'call_tool', 'answer_from_knowledge_base', 'verify_math', 'give_coaching_advice']

# A tool bound but never mentioned is invisible to a small model - it has no reason
# to reach for create_my_job just because it's technically callable. This exact bug
# happened once already for the plain fallback path (agents/llm_agent.py), and is
# now also happening for two of this module's three draft_system_prompt branches
# below (the clarify-blocking branch and the KB-only branch never mention the tool
# exists, even though it's bound via extra_tools regardless of which branch fires).
# Confirmed live: a real user's "Setup a daily journal logger for me. At 9:00pm..."
# got routed into the answer_from_knowledge_base branch (a false-positive KB match),
# and only succeeded in creating the job because the model got lucky/resourceful -
# not because the prompt told it to.
JOB_TOOL_HINT = (
    "You can also set up an actual recurring WhatsApp reminder for this person via "
    "the create_my_job tool - use it whenever they're asking to be tracked, "
    "reminded, or checked in on repeatedly (e.g. \"start a habit tracker\", "
    "\"remind me every night to journal\", \"check in with me weekly\"), not just "
    "asking for one-time advice. If they didn't say how often or what time, ask "
    "before creating it. Use list_my_jobs/cancel_my_job if they ask what's "
    "scheduled or want to stop one."
)


def _parse_json_loose(text: str, default: Any) -> Any:
    """Local models don't always return clean JSON. Try straight, then try pulling the
    first {...}/[...] substring out of surrounding prose, then give up to `default`."""
    text = (text or '').strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r'[\{\[].*[\}\]]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return default


def _extract_plan_steps(parsed: Any) -> List[str]:
    """A local model asked for a flat JSON list of step names reliably wraps it in
    extra structure anyway (seen for real: {"steps": [{"step": "...", "details": "..."}]}
    instead of ["..."]). Rather than fight that with prompt wording alone, unwrap the
    common shapes: a bare list, a dict with a steps/plan/actions list, and list items
    that are either plain strings or single-key-ish dicts naming the step."""
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        items = next((parsed[k] for k in ('steps', 'plan', 'actions') if isinstance(parsed.get(k), list)), None)
        if items is None:
            return []
    else:
        return []

    steps = []
    for item in items:
        if isinstance(item, str):
            steps.append(item)
        elif isinstance(item, dict):
            name = next((item[k] for k in ('step', 'action', 'name', 'type') if isinstance(item.get(k), str)), None)
            if name:
                steps.append(name)
    return steps


class ReasoningCycle:
    """One instance per LLMAgent - shares its ollama_url/mcp_tools, reads each stage's
    own model/temperature/timeout/prompt_template live from control_plane every turn
    (so a dashboard or WhatsApp admin change takes effect on the very next message,
    same as the 7 pipeline agents already do)."""

    def __init__(self, control_plane, ollama_url: str, mcp_tools: List):
        self.control_plane = control_plane
        self.ollama_url = ollama_url
        self.mcp_tools = mcp_tools

    def _cfg(self, agent_id: str):
        return self.control_plane.get_agent_config(agent_id)

    async def _call_stage(self, agent_cfg, system_prompt: str, human_prompt: str, extra_tools: Optional[List] = None) -> str:
        """One focused call for a stage: builds a tool-capable agent on THAT stage's own
        configured model/temperature (never a shared one) - every stage can decide to
        search/fetch if its own reasoning calls for it, not just Calliope. Built fresh
        per call, not cached, for the same cross-event-loop reason agents/llm_agent.py's
        _build_react_agent is - a cached client would work once then fail with "Event
        loop is closed" on the next message's fresh event loop.

        extra_tools are appended for this call only (e.g. Calliope gets a client's
        job-scheduling self-service tools bound in per-message by agents/llm_agent.py -
        contact-scoped, so they can't live in self.mcp_tools, which is shared/static)."""
        from langchain_ollama import ChatOllama
        from langgraph.prebuilt import create_react_agent

        model = ChatOllama(
            model=agent_cfg.model,
            base_url=self.ollama_url,
            temperature=agent_cfg.temperature if agent_cfg.temperature is not None else 0.7,
        )
        tools = self.mcp_tools + (extra_tools or [])
        agent = create_react_agent(model, tools)

        messages = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=human_prompt))

        result = await asyncio.wait_for(
            agent.ainvoke({"messages": messages}),
            timeout=agent_cfg.timeout or 60,
        )
        final = result["messages"][-1]
        if not isinstance(final, AIMessage) or not final.content:
            raise Exception("Stage produced no final answer")
        return final.content.strip()

    async def hermes_triage(self, message_body: str, kb_hit: bool) -> str:
        """quick vs deep. Disabled -> 'quick' (the cycle never engages at all, falling
        back to LLMAgent's original single-call path) - this is the master gate every
        other stage's enabled flag is downstream of.

        kb_hit short-circuits straight to 'deep' rather than asking the model to guess:
        we've already run a real semantic search and found a genuine match, which is a
        far more reliable signal than asking a small model "does this look like it needs
        the knowledge base" from the raw text alone. Concretely this matters - "what is
        the crux of [a book]" reads as a low-complexity question and a small model
        reasonably says 'quick' for it, which used to skip the cycle (and therefore
        Prometheus's correct KB-vs-coaching routing) entirely, right back into the same
        unreliable prompt-only fix this whole cycle exists to avoid."""
        cfg = self._cfg('hermes')
        if not cfg or not cfg.enabled:
            return 'quick'
        if kb_hit:
            logger.info("🔀 Hermes: deep (knowledge base has a relevant match)")
            return 'deep'
        try:
            raw = await self._call_stage(cfg, cfg.prompt_template, message_body)
            decision = 'deep' if 'deep' in raw.lower() else 'quick'
            logger.info(f"🔀 Hermes: {decision} (raw: {raw[:40]!r})")
            return decision
        except Exception as e:
            logger.warning(f"⚠️  Hermes triage failed, defaulting to quick: {e}")
            return 'quick'

    async def prometheus_plan(self, message_body: str, kb_hit: bool) -> List[str]:
        """Fixed menu only (PLAN_MENU) - never freeform. Disabled or failure ->
        ['give_coaching_advice'], matching today's default (always-coaching) behavior."""
        cfg = self._cfg('prometheus')
        default_plan = ['give_coaching_advice']
        if not cfg or not cfg.enabled:
            return default_plan
        try:
            human = f"Message: {message_body}\nKnowledge base has relevant material: {kb_hit}"
            raw = await self._call_stage(cfg, cfg.prompt_template, human)
            parsed = _parse_json_loose(raw, default_plan)
            steps = _extract_plan_steps(parsed)
            plan = [step for step in steps if step in PLAN_MENU]
            if not plan:
                logger.warning(f"⚠️  Prometheus plan had no recognizable steps, defaulting to coaching. Raw: {raw[:150]!r}")
                return default_plan
            logger.info(f"📋 Prometheus plan: {plan} (raw: {raw[:150]!r})")
            return plan
        except Exception as e:
            logger.warning(f"⚠️  Prometheus planning failed, defaulting to coaching: {e}")
            return default_plan

    async def pythia_clarify(self, message_body: str, plan: List[str]) -> Dict[str, Any]:
        """Only runs if planned and enabled. Failure -> not blocking (never stalls the
        user indefinitely because a parse hiccup)."""
        default = {'blocking': False, 'reason': ''}
        cfg = self._cfg('pythia')
        if 'ask_clarifying_question' not in plan or not cfg or not cfg.enabled:
            return default
        try:
            raw = await self._call_stage(cfg, cfg.prompt_template, message_body)
            parsed = _parse_json_loose(raw, default)
            if not isinstance(parsed, dict) or 'blocking' not in parsed:
                return default
            return parsed
        except Exception as e:
            logger.warning(f"⚠️  Pythia clarification check failed, not blocking: {e}")
            return default

    def hephaestus_allows_tools(self, plan: List[str]) -> bool:
        """Not a model call - a signal, recorded in the trace. Every stage's underlying
        call is always tool-capable (every stage gets the full MCP tool set - Momus can
        fact-check mid-critique, Pythia can look something up while judging a gap, not
        just Calliope), so this doesn't strip tools from anything; it records whether
        the plan explicitly called for one, which is what "Hephaestus enabled" means -
        the forge being lit for THIS turn versus tools simply being available always."""
        cfg = self._cfg('hephaestus')
        return 'call_tool' in plan and bool(cfg and cfg.enabled)

    async def calliope_draft(self, message_body: str, system_prompt: str, extra_tools: Optional[List] = None) -> str:
        cfg = self._cfg('calliope')
        return await self._call_stage(cfg, system_prompt, message_body, extra_tools=extra_tools)

    async def momus_review(self, draft: str, source_material: str, plan: List[str]) -> Dict[str, Any]:
        """Disabled or failure -> approved (never blocks the pipeline on a broken
        reviewer - the point is to catch real problems, not add a new failure mode)."""
        default = {'approved': True, 'feedback': ''}
        cfg = self._cfg('momus')
        if not cfg or not cfg.enabled:
            return default
        try:
            human = (
                f"Draft response:\n{draft}\n\n"
                f"Source material it should be grounded in (if any):\n{source_material or '(none retrieved)'}\n\n"
                f"Plan for this turn: {plan}"
            )
            raw = await self._call_stage(cfg, cfg.prompt_template, human)
            parsed = _parse_json_loose(raw, default)
            if not isinstance(parsed, dict) or 'approved' not in parsed:
                return default
            return parsed
        except Exception as e:
            logger.warning(f"⚠️  Momus review failed, approving by default: {e}")
            return default

    async def run(
        self,
        message_body: str,
        coaching_system_prompt: str,
        kb_snippets: List[str],
        extra_tools: Optional[List] = None,
    ) -> Optional[Dict[str, Any]]:
        """Runs the full cycle. Returns None if the cycle doesn't engage (Hermes says
        quick, or Hermes is disabled) - caller should fall through to its own original
        single-call path in that case. Returns a trace dict with 'response' and the
        per-stage decisions (for state.metadata, so /api/history shows why a response
        came out the way it did) when the cycle does produce an answer."""
        kb_hit = bool(kb_snippets)
        triage = await self.hermes_triage(message_body, kb_hit)
        if triage != 'deep':
            return None

        plan = await self.prometheus_plan(message_body, kb_hit)

        clarify = await self.pythia_clarify(message_body, plan)
        allow_tools = self.hephaestus_allows_tools(plan)

        if clarify.get('blocking'):
            draft_system_prompt = (
                "The user's request can't be answered responsibly yet - a real piece of "
                f"information is missing: {clarify.get('reason', 'more context is needed')}. "
                "Write ONE short, warm question asking for exactly that missing piece. "
                "Do not give advice or a plan yet - just ask."
            )
        elif 'answer_from_knowledge_base' in plan and 'give_coaching_advice' not in plan:
            source_text = "\n\n".join(f"- {s}" for s in kb_snippets) if kb_snippets else ''
            draft_system_prompt = (
                "Answer the user's question directly and completely using the material below. "
                "Do not add unrelated advice or force this into any other response format.\n\n"
                f"Knowledge base:\n{source_text}"
            )
        else:
            draft_system_prompt = coaching_system_prompt

        if extra_tools:
            draft_system_prompt = draft_system_prompt + "\n\n" + JOB_TOOL_HINT

        draft = await self.calliope_draft(message_body, draft_system_prompt, extra_tools=extra_tools)

        source_material = "\n\n".join(kb_snippets) if kb_snippets else ''
        review = await self.momus_review(draft, source_material, plan)
        retried = False
        if not review.get('approved', True):
            retried = True
            feedback_prompt = (
                f"{draft_system_prompt}\n\n"
                f"Your previous attempt had a problem: {review.get('feedback', 'unspecified')}. "
                "Write a corrected response addressing that."
            )
            try:
                draft = await self.calliope_draft(message_body, feedback_prompt, extra_tools=extra_tools)
            except Exception as e:
                logger.warning(f"⚠️  Momus-triggered retry failed, sending original draft: {e}")

        return {
            'response': draft,
            'trace': {
                'triage': triage,
                'plan': plan,
                'clarify': clarify,
                'allow_tools': allow_tools,
                'momus_review': review,
                'momus_retried': retried,
            },
        }

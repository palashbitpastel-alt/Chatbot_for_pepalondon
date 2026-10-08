"""Shared plumbing for every agent in the app.

An agent package (``admin_agent``, ``customer_support_agent``, ...) only has to
supply a system prompt and a list of tools; the LLM client, the prompt scaffold,
the executor wiring and the chat-history conversion all live here so the agents
stay thin and behave consistently.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from app.core.config import settings

MAX_ITERATIONS = 6

# (role, content) pairs where role is 'user' or 'assistant'.
ChatHistory = list[tuple[str, str]]

# One streamed step of a turn. 'type' is:
#   token  -> {"text": str}            a piece of the reply as the model writes it
#   reset  -> {}                       drop the tokens shown so far (see below)
#   tool   -> {"name": str, "phase": "start"|"end", "output": str|None}
#                                      a tool call starting/finishing; output on end
#   final  -> {"reply": str}           the complete reply, emitted once at the end
AgentEvent = dict[str, Any]

AgentRunner = Callable[[str, ChatHistory], Awaitable[str]]
AgentStreamer = Callable[[str, ChatHistory], AsyncIterator[AgentEvent]]
# (session_id, user message, reply) -> stores the turn in the agent's long-term memory
AgentRemember = Callable[[str, str, str], Awaitable[None]]
# raw reply -> (reply to show and save, structured actions for the client to render)
AgentFinalise = Callable[[str], tuple[str, list[dict]]]


@dataclass(frozen=True)
class Agent:
    """An agent as the API sees it: a name to route by, plus how to run it."""

    name: str
    label: str
    description: str
    run: AgentRunner
    stream: AgentStreamer
    # Optional: called after a turn is saved so an agent can keep long-term memory.
    remember: AgentRemember | None = None
    # Optional: strips machine-readable extras (suggested follow-ups, links) out
    # of the reply text so the client can render them as buttons.
    finalise: AgentFinalise | None = None


# Room for Gemini's thinking on top of the answer itself, which shares max_tokens.
# 1024 cut a product list off mid-line when the thinking ran long.
_GEMINI_THINKING_TOKENS = 3072


# While DeepSeek is out of credit or refuses its key, every call would wait for
# that refusal before Gemini answered - long enough that the shopper-reading
# call timed out and fell back to word lists ("doesn't like pink" -> Colour:
# Pink). After such a refusal Gemini answers alone for a while; then DeepSeek
# is tried again, so a top-up takes effect by itself.
DEEPSEEK_REST_SECONDS = 10 * 60
# A key DeepSeek does not recognise will not start working by itself - a new
# key means a new deploy, which starts the clock again - so it rests longer.
DEEPSEEK_BAD_KEY_REST_SECONDS = 6 * 60 * 60
_deepseek_resting_until = 0.0


class DeepSeekResting(RuntimeError):
    """Raised without a network call while DeepSeek rests, so the fallback answers."""


def _deepseek_refused(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    return status in (401, 402) or "insufficient balance" in str(exc).lower()


class _DeepSeekChat(ChatOpenAI):
    """ChatOpenAI that notes when DeepSeek refuses for credit or key."""

    async def _agenerate(self, *args: Any, **kwargs: Any):
        # The agent's model chain is built once, so the rest is checked here, on
        # every call: a resting DeepSeek hands over at once instead of waiting
        # for its refusal on each step of each reply.
        if time.monotonic() < _deepseek_resting_until:
            raise DeepSeekResting("DeepSeek is resting")
        try:
            return await super()._agenerate(*args, **kwargs)
        except Exception as exc:
            _note_refusal(exc)
            raise

    async def _astream(self, *args: Any, **kwargs: Any):
        if time.monotonic() < _deepseek_resting_until:
            raise DeepSeekResting("DeepSeek is resting")
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        except Exception as exc:
            _note_refusal(exc)
            raise


def _note_refusal(exc: BaseException) -> None:
    global _deepseek_resting_until
    if _deepseek_refused(exc) and settings.GEMINI_API_KEY:
        bad_key = getattr(exc, "status_code", None) == 401
        _deepseek_resting_until = time.monotonic() + (
            DEEPSEEK_BAD_KEY_REST_SECONDS if bad_key else DEEPSEEK_REST_SECONDS)


def _deepseek(temperature: float, max_tokens: int | None) -> ChatOpenAI:
    return _DeepSeekChat(
        model=settings.DEEPSEEK_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens,
        # A stalled model must not hold the shopper's chat for ten minutes.
        timeout=60,
        max_retries=1,
    )


# Google's documented stand-in for a tool call whose signature was never seen
# (one DeepSeek made, before a fallback mid-turn).
_NO_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}


class _GeminiChat(ChatOpenAI):
    """ChatOpenAI that hands Gemini back its thought signatures.

    Gemini 3 signs every tool call it makes (``extra_content`` on the call) and
    refuses the next request - "Function call is missing a thought_signature" -
    unless the history carries that signature back. LangChain keeps the raw call
    in ``additional_kwargs`` but rebuilds the outgoing tool calls without it, so
    the agent's second step always failed. The signature is put back here.
    """

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        signed = {
            raw.get("id"): raw["extra_content"]
            for message in self._convert_input(input_).to_messages()
            if isinstance(message, AIMessage)
            for raw in message.additional_kwargs.get("tool_calls") or []
            if isinstance(raw, dict) and raw.get("extra_content")
        }
        for message in payload.get("messages") or []:
            for call in message.get("tool_calls") or []:
                call.setdefault("extra_content", signed.get(call.get("id")) or _NO_SIGNATURE)
        return payload


def _gemini_models() -> list[str]:
    return [m.strip() for m in settings.GEMINI_MODEL.split(",") if m.strip()]


def _gemini(model: str, temperature: float, max_tokens: int | None) -> ChatOpenAI:
    extra = {"reasoning_effort": settings.GEMINI_REASONING_EFFORT} if settings.GEMINI_REASONING_EFFORT else None
    return _GeminiChat(
        model=model,
        api_key=settings.GEMINI_API_KEY,
        base_url=settings.GEMINI_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens + _GEMINI_THINKING_TOKENS if max_tokens else None,
        extra_body=extra,
        timeout=60,
        max_retries=1,
    )


def build_llm(temperature: float = 0.2, max_tokens: int | None = None) -> Runnable:
    """The shared chat model: DeepSeek, with Gemini as the backup.

    A DeepSeek call that fails (out of credit, key rejected, down) is retried on
    each Gemini model in GEMINI_MODEL in turn when GEMINI_API_KEY is set, so a
    shopper still gets an answer; the next call tries DeepSeek first again.
    bind_tools() passes through to every model in the chain.
    """
    resting = time.monotonic() < _deepseek_resting_until
    chain = [_deepseek(temperature, max_tokens)] if settings.DEEPSEEK_API_KEY and not resting else []
    if settings.GEMINI_API_KEY:
        chain += [_gemini(m, temperature, max_tokens) for m in _gemini_models()]
    if len(chain) < 2:
        return chain[0] if chain else _deepseek(temperature, max_tokens)
    return chain[0].with_fallbacks(chain[1:])


def build_agent_executor(
    system_prompt: str,
    tools: Sequence[BaseTool],
    temperature: float = 0.2,
    max_iterations: int = MAX_ITERATIONS,
    max_tokens: int | None = None,
) -> AgentExecutor:
    """Build a tool-calling agent executor from a system prompt and a tool set."""
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    )
    llm = build_llm(temperature, max_tokens)
    agent = create_tool_calling_agent(llm, list(tools), prompt)
    return AgentExecutor(agent=agent, tools=list(tools), max_iterations=max_iterations,
                         max_execution_time=120)


def to_messages(history: ChatHistory) -> list[BaseMessage]:
    """Convert stored (role, content) rows into LangChain messages."""
    return [
        HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        for role, content in history
    ]


async def run_executor(executor: AgentExecutor, message: str, history: ChatHistory) -> str:
    """Run one turn against an executor and return the agent's reply text."""
    result = await executor.ainvoke({"input": message, "chat_history": to_messages(history)})
    return plain_dashes(result["output"])


# Models reach for em and en dashes constantly; the store wants plain punctuation.
_DASHES = str.maketrans({"—": "-", "–": "-"})


def plain_dashes(text: str) -> str:
    """Replace em and en dashes with a plain hyphen."""
    return text.translate(_DASHES)


def _chunk_text(chunk: Any) -> str:
    """Text out of a streamed model chunk, whose content may be a str or blocks."""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _tool_output_text(output: Any) -> str | None:
    """A tool's return value as text, whether it came back raw or as a message."""
    if output is None:
        return None
    content = getattr(output, "content", output)
    return content if isinstance(content, str) else None


async def stream_executor(
    executor: AgentExecutor, message: str, history: ChatHistory
) -> AsyncIterator[AgentEvent]:
    """Run one turn, yielding reply tokens and tool activity as they happen.

    A tool-calling agent often thinks out loud ("let me check that...") before it
    reaches for a tool, and that commentary is not part of the answer it settles
    on. So a ``reset`` is emitted whenever a tool starts: the tokens after the
    last ``reset`` are the reply, which keeps what the shopper watched being typed
    identical to what is saved and returned in ``final``.
    """
    tokens: list[str] = []
    final: str | None = None

    async for event in executor.astream_events(
        {"input": message, "chat_history": to_messages(history)}, version="v2"
    ):
        kind = event["event"]
        if kind == "on_chat_model_stream":
            text = plain_dashes(_chunk_text(event["data"].get("chunk")))
            if text:
                tokens.append(text)
                yield {"type": "token", "text": text}
        elif kind == "on_tool_start":
            tokens.clear()
            yield {"type": "reset"}
            # The arguments too, so a wrong answer can be traced to what was asked of the tool.
            yield {"type": "tool", "name": event["name"], "phase": "start",
                   "input": event["data"].get("input")}
        elif kind == "on_tool_end":
            # Carry the tool's result too: a caller may want the structured data
            # behind the answer (product cards, for instance), not just the prose.
            yield {
                "type": "tool",
                "name": event["name"],
                "phase": "end",
                "output": _tool_output_text(event["data"].get("output")),
            }
        elif kind == "on_chain_end" and event.get("name") == "AgentExecutor":
            output = event["data"].get("output")
            if isinstance(output, dict) and isinstance(output.get("output"), str):
                final = output["output"]

    # Hitting the step or time limit returns LangChain's own sentence; a shopper
    # should never read "Agent stopped due to iteration limit".
    if final and final.startswith("Agent stopped due to"):
        final = "Sorry, that took me longer than it should. Could you ask me that again?"
    yield {"type": "final", "reply": plain_dashes(final or "".join(tokens))}

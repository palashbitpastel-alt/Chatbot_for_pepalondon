"""Shared plumbing for every agent in the app.

An agent package (``admin_agent``, ``customer_support_agent``, ...) only has to
supply a system prompt and a list of tools; the LLM client, the prompt scaffold,
the executor wiring and the chat-history conversion all live here so the agents
stay thin and behave consistently.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
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


def build_llm(temperature: float = 0.2, max_tokens: int | None = None) -> ChatOpenAI:
    """The shared DeepSeek (OpenAI-compatible) chat model."""
    return ChatOpenAI(
        model=settings.DEEPSEEK_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens,
    )


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
    return AgentExecutor(agent=agent, tools=list(tools), max_iterations=max_iterations)


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
            yield {"type": "tool", "name": event["name"], "phase": "start"}
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

    yield {"type": "final", "reply": plain_dashes(final or "".join(tokens))}

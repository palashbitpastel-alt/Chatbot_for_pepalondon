"""The admin agent: inventory, marketing, operations and finance for this store.

Every turn is retrieval-augmented: before the model sees the question, the
pgvector index (``services.rag``) is searched for earlier staff conversations
that match it, and those are attached as context. After a turn is answered, the
question and reply are embedded and stored so later conversations - in any
session - can recall them. Store data itself (products, orders, ...) is not
retrieved this way - the agent's tools read it live from Shopify on demand.
"""

import json
import logging
import re
from collections.abc import AsyncIterator
from functools import lru_cache

from langchain.agents import AgentExecutor

from app.agent.admin_agent.prompts import ADMIN_SYSTEM_PROMPT
from app.agent.admin_agent.tools import ADMIN_TOOLS
from app.agent.base import (
    Agent,
    AgentEvent,
    ChatHistory,
    build_agent_executor,
    run_executor,
    stream_executor,
)
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services import rag

logger = logging.getLogger(__name__)

CONTEXT_HEADER = "Retrieved context (from earlier staff conversations; verify totals with tools):"

ACTIONS_RE = re.compile(r"<<actions>>\s*(.*?)\s*<</actions>>", re.DOTALL)
ACTIONS_OPEN = "<<actions>>"
MAX_ACTIONS = 6
INTERNAL_ANCHORS = {
    "#inventory-analysis",
    "#marketing-analysis",
    "#operations-analysis",
    "#finance-analysis",
    "#priority-actions",
    "#health-scoreboard",
}


def store_admin_url() -> str:
    host = settings.SHOPIFY_STORE_URL.removeprefix("https://").removeprefix("http://").strip("/")
    handle = host.split(".")[0] if host else ""
    return f"https://admin.shopify.com/store/{handle}" if handle else "https://admin.shopify.com"


@lru_cache(maxsize=1)
def get_admin_agent_executor() -> AgentExecutor:
    # The prompt is a LangChain template: {admin_url} is the only variable;
    # literal braces in it are doubled.
    prompt = ADMIN_SYSTEM_PROMPT.replace("{admin_url}", store_admin_url())
    return build_agent_executor(prompt, ADMIN_TOOLS)


def _valid_href(href: str) -> bool:
    """Dashboard anchors or https links only; nothing that could run script."""
    return href in INTERNAL_ANCHORS or href.startswith("https://")


def split_actions(reply: str) -> tuple[str, list[dict]]:
    """Separate the trailing ``<<actions>>`` block from the prose.

    Returns the prose (what is shown and saved) and a cleaned list of
    ``{"type": "chip", "label"}`` / ``{"type": "link", "label", "href"}`` dicts.
    A malformed block is simply dropped, never shown to the user.
    """
    match = ACTIONS_RE.search(reply)
    if not match:
        # Model stopped mid-block: drop whatever is after the opener.
        cut = reply.find(ACTIONS_OPEN)
        return (reply[:cut] if cut != -1 else reply).rstrip(), []
    prose = (reply[: match.start()] + reply[match.end() :]).strip()
    actions: list[dict] = []
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return prose, []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        kind = item.get("type")
        if not label:
            continue
        if kind == "chip":
            actions.append({"type": "chip", "label": label})
        elif kind == "link":
            href = str(item.get("href") or "").strip()
            if href and _valid_href(href):
                actions.append({"type": "link", "label": label, "href": href})
        if len(actions) >= MAX_ACTIONS:
            break
    return prose, actions


async def _augment(message: str, history: ChatHistory) -> str:
    """The question plus any retrieved context worth attaching."""
    try:
        async with AsyncSessionLocal() as db:
            context = await rag.build_context(db, message)
    except Exception:
        logger.exception("Retrieval failed; answering without RAG context")
        return message
    if not context:
        return message
    # Turns already in this conversation's history need not be repeated.
    asked = {content.strip() for role, content in history if role == "user"}
    lines = [ln for ln in context.splitlines() if not any(f"Staff asked: {q}" in ln for q in asked)]
    context = "\n".join(lines).strip()
    if not context or context.endswith(":"):
        return message
    return f"{message}\n\n---\n{CONTEXT_HEADER}\n{context}"


async def run_admin_agent(message: str, history: ChatHistory) -> str:
    """Run one admin-agent turn. history is a list of (role, content) with role 'user' or 'assistant'."""
    return await run_executor(get_admin_agent_executor(), await _augment(message, history), history)


async def stream_admin_agent(message: str, history: ChatHistory) -> AsyncIterator[AgentEvent]:
    """Run one admin-agent turn, yielding reply tokens and tool activity as they happen."""
    augmented = await _augment(message, history)
    async for event in stream_executor(get_admin_agent_executor(), augmented, history):
        yield event


async def remember_admin_turn(session_id: str, message: str, reply: str) -> None:
    """Embed and store a finished turn for cross-session recall."""
    try:
        async with AsyncSessionLocal() as db:
            await rag.remember_chat_turn(db, session_id, message, reply)
    except Exception:
        logger.exception("Could not store the admin chat turn in the retrieval index")


ADMIN_AGENT = Agent(
    name="admin",
    label="NestIQ",
    description="Answers staff questions about inventory, marketing, operations and finance, with memory of earlier conversations.",
    run=run_admin_agent,
    stream=stream_admin_agent,
    remember=remember_admin_turn,
    finalise=split_actions,
)

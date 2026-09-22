"""The customer support agent: products, order status and policies, for shoppers."""

from collections.abc import AsyncIterator
from functools import lru_cache

from langchain.agents import AgentExecutor

from app.agent.base import (
    Agent,
    AgentEvent,
    ChatHistory,
    build_agent_executor,
    run_executor,
    stream_executor,
)
from app.agent.customer_support_agent.prompts import CUSTOMER_SUPPORT_SYSTEM_PROMPT
from app.agent.customer_support_agent.tools import CUSTOMER_SUPPORT_TOOLS


# Fitting a whole look to a budget takes a few passes - browse, price, swap the
# piece that broke the budget, price again - so this agent gets more headroom
# than the six steps a single lookup needs.
OUTFIT_MAX_ITERATIONS = 10

# This agent faces the open storefront, so a runaway answer is a runaway bill.
# A full outfit with its bullets fits comfortably in a few hundred tokens.
MAX_REPLY_TOKENS = 500


@lru_cache(maxsize=1)
def get_customer_support_agent_executor() -> AgentExecutor:
    # Slightly warmer than the admin agent: this one is talking to shoppers.
    return build_agent_executor(
        CUSTOMER_SUPPORT_SYSTEM_PROMPT,
        CUSTOMER_SUPPORT_TOOLS,
        temperature=0.3,
        max_iterations=OUTFIT_MAX_ITERATIONS,
        max_tokens=MAX_REPLY_TOKENS,
    )


async def run_customer_support_agent(message: str, history: ChatHistory) -> str:
    """Run one support turn and return the whole reply."""
    return await run_executor(get_customer_support_agent_executor(), message, history)


def stream_customer_support_agent(message: str, history: ChatHistory) -> AsyncIterator[AgentEvent]:
    """Run one support turn, yielding reply tokens and tool activity as they happen."""
    return stream_executor(get_customer_support_agent_executor(), message, history)


CUSTOMER_SUPPORT_AGENT = Agent(
    name="customer_support",
    label="Customer Support",
    description="Answers shopper questions about products, order status and store policies.",
    run=run_customer_support_agent,
    stream=stream_customer_support_agent,
)

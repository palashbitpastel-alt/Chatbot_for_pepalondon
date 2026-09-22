"""The agents the API can route a conversation to.

Register a new agent by adding its ``Agent`` here; endpoints look one up by name
and need no other change.
"""

from app.agent.admin_agent import ADMIN_AGENT
from app.agent.base import Agent
from app.agent.customer_support_agent import CUSTOMER_SUPPORT_AGENT

DEFAULT_AGENT = ADMIN_AGENT.name

AGENTS: dict[str, Agent] = {a.name: a for a in (ADMIN_AGENT, CUSTOMER_SUPPORT_AGENT)}


def get_agent(name: str | None = None) -> Agent:
    """Look up an agent by name. Raises KeyError for an unknown name."""
    return AGENTS[name or DEFAULT_AGENT]

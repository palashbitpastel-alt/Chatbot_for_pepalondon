"""Retrieval over PROJECT_DESCRIPTION.md - the store's own handbook.

The handbook holds things no API can tell us: which storefront paths exist, how
the account pages are laid out, what the collections are called, and what the
policies actually say. It is chunked by section and embedded into the same
``knowledge_chunks`` table (pgvector on Railway) that the admin agent's
retrieval uses, under its own ``handbook`` kind.

Two rules the document itself sets, enforced here rather than left to the model:

* **Audience.** Some sections are for whoever builds the bot, not for a shopper:
  admin URLs, API scopes, the security rules, the to-do list. Those are indexed
  as ``audience: internal`` and the shopper-facing tool never retrieves them.
* **Confidence.** The document marks confirmed facts with a tick, guesses with a
  warning sign, and blanks with an empty box, and says plainly that a wrong
  refund window creates real disputes. Those markers are preserved verbatim in
  the chunk text, and each chunk records whether it still contains any, so the
  agent can see what it must not state as fact.
"""

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeChunk, StoreSetting
from app.services import rag

logger = logging.getLogger(__name__)

HANDBOOK_KIND = "handbook"
HASH_SETTING = "handbook_sha"

# backend/app/services/handbook.py -> backend/PROJECT_DESCRIPTION.md
HANDBOOK_PATH = Path(__file__).resolve().parents[2] / "PROJECT_DESCRIPTION.md"

# Section numbers a shopper must never be answered from: base URLs (they include
# the Shopify admin app-config link), the API/scopes section, the bot's own
# security rules, the open-items list and the changelog.
INTERNAL_SECTIONS = {"1", "2", "7", "8", "9", "10", "11"}

UNVERIFIED_MARK = "⚠"  # warning sign: assumed, must be verified
BLANK_MARK = "\U0001f532"  # empty box: not filled in yet
CONFIRMED_MARK = "✅"  # tick: confirmed

MAX_CHUNK_CHARS = 1800
SECTION_RE = re.compile(r"^(#{2,3})\s+(.*)$")
NUMBER_RE = re.compile(r"^(\d+)")


def _split_sections(text: str) -> list[dict]:
    """Split the document on its ## and ### headings, keeping each heading with its body."""
    sections: list[dict] = []
    heading: str | None = None
    body: list[str] = []

    def flush() -> None:
        if heading is None:
            return
        content = "\n".join(body).strip()
        if content:
            sections.append({"heading": heading, "body": content})

    for line in text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            body = []
        else:
            body.append(line)
    flush()
    return sections


def _section_number(heading: str) -> str | None:
    match = NUMBER_RE.match(heading)
    return match.group(1) if match else None


def _split_long(heading: str, body: str) -> list[str]:
    """Keep chunks retrievable: split an over-long section on blank lines."""
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    parts, current = [], ""
    for block in body.split("\n\n"):
        if current and len(current) + len(block) + 2 > MAX_CHUNK_CHARS:
            parts.append(current.strip())
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current.strip():
        parts.append(current.strip())
    return parts


def build_chunks(text: str) -> list[dict]:
    """Turn the handbook into rows ready for the shared chunk writer."""
    rows: list[dict] = []
    for section in _split_sections(text):
        heading = section["heading"]
        number = _section_number(heading)
        audience = "internal" if number in INTERNAL_SECTIONS else "customer"
        for index, part in enumerate(_split_long(heading, section["body"])):
            rows.append(
                {
                    "kind": HANDBOOK_KIND,
                    "ref_id": f"{number or heading}:{index}"[:64],
                    # The heading rides along in the text so a retrieved chunk
                    # still says which part of the handbook it came from.
                    "content": f"# {heading}\n\n{part}",
                    "meta": {
                        "section": heading,
                        "section_number": number,
                        "audience": audience,
                        "has_unverified": UNVERIFIED_MARK in part,
                        "has_blanks": BLANK_MARK in part,
                        "has_confirmed": CONFIRMED_MARK in part,
                    },
                }
            )
    return rows


def read_handbook() -> str | None:
    if not HANDBOOK_PATH.is_file():
        logger.warning("No handbook at %s; skipping handbook indexing", HANDBOOK_PATH)
        return None
    return HANDBOOK_PATH.read_text(encoding="utf-8")


async def reindex(db: AsyncSession, force: bool = False) -> dict:
    """Re-embed the handbook when it has changed. Returns what happened."""
    text = read_handbook()
    if text is None:
        return {"indexed": 0, "skipped": True, "reason": "missing"}

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stored = (await db.execute(select(StoreSetting).where(StoreSetting.key == HASH_SETTING))).scalar_one_or_none()
    if not force and stored is not None and stored.value == digest:
        return {"indexed": 0, "skipped": True, "reason": "unchanged"}

    rows = build_chunks(text)
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.kind == HANDBOOK_KIND))
    written = await rag._write_chunks(db, rows)

    if stored is None:
        db.add(StoreSetting(key=HASH_SETTING, value=digest))
    else:
        stored.value = digest
    await db.commit()

    customer = sum(1 for r in rows if r["meta"]["audience"] == "customer")
    logger.info("Indexed %s handbook chunks (%s shopper-facing)", written, customer)
    return {"indexed": written, "customer_facing": customer, "skipped": False}


async def search(db: AsyncSession, question: str, limit: int = 4, audience: str = "customer") -> list[dict]:
    """Handbook passages relevant to a question, filtered to one audience.

    ``audience="customer"`` is the only safe setting for the shopper-facing
    agent: it keeps admin URLs, API scopes and the bot's own security rules out
    of a conversation with a customer.
    """
    hits = await rag.search(db, question, kinds=[HANDBOOK_KIND], limit=limit * 3)
    out: list[dict] = []
    for hit in hits:
        meta = hit.meta or {}
        if audience and meta.get("audience") != audience:
            continue
        out.append(
            {
                "section": meta.get("section"),
                "content": hit.content,
                "score": hit.score,
                # Read these before quoting: the handbook marks guesses and gaps.
                "contains_unverified_items": bool(meta.get("has_unverified")),
                "contains_blanks_to_be_filled": bool(meta.get("has_blanks")),
            }
        )
        if len(out) >= limit:
            break
    return out

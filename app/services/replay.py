"""The replay test: does the assistant still answer reviewed moments the owner's way?

Every answer the owner reviewed (see ``services.lessons``) is a test case. A run
copies the conversation up to that moment into a scratch session, sends the
shopper's message through the real chat endpoint - live catalogue, live tools,
the current prompt - and has the model judge the new answer against the owner's
verdict: an approved answer must be handled the same way, a wrong one must not
be repeated and should follow the correction.

``mode="both"`` also replays each case with the reviewed lessons switched off,
so the owner can see what the lessons themselves are worth.

One run at a time; the latest result is kept in ``store_settings``.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db.models import ChatMessage, KnowledgeChunk, StoreSetting
from app.db.session import AsyncSessionLocal
from app.services import lessons

logger = logging.getLogger(__name__)

RESULT_KEY = "replay_test"
SCRATCH_PREFIX = "cs_rp_"
CONCURRENCY = 3
TURN_TIMEOUT = 150
JUDGE_TIMEOUT = 60

_task: asyncio.Task | None = None

_JUDGE = """You check a children's clothing shop assistant against its owner's review.
You get the conversation so far, what the owner said about the assistant's ORIGINAL answer,
and the assistant's NEW answer to the same message (with the products it showed).
Decide whether the NEW answer handles the moment the way the owner wants:
- If the owner APPROVED the original: the new answer must take the same approach (asks the
  same kind of question, or shows the same kind of thing, same intent). Wording, exact
  products and prices may differ - the catalogue changes.
- If the owner marked the original WRONG: the new answer must not make that mistake and should
  do what the owner's correction / note asks. Products and prices may differ.
Return ONLY JSON: {"pass": true|false, "reason": "one short sentence"}."""


def running() -> bool:
    return _task is not None and not _task.done()


async def last_result() -> dict | None:
    async with AsyncSessionLocal() as db:
        row = await db.get(StoreSetting, RESULT_KEY)
        return row.value if row else None


async def _store(value: dict) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(StoreSetting, RESULT_KEY)
        if row:
            row.value = value
        else:
            db.add(StoreSetting(key=RESULT_KEY, value=value))
        await db.commit()


async def _cases() -> list[dict]:
    """Every reviewed answer, with the conversation that led to it."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.kind == lessons.KIND)
                                 .order_by(KnowledgeChunk.id.asc()))).scalars().all()
        cases = []
        for row in rows:
            meta = row.meta or {}
            answer_id = int(row.ref_id)
            answer = await db.get(ChatMessage, answer_id)
            if answer is None:
                continue
            before = (await db.execute(
                select(ChatMessage).where(ChatMessage.session_id == answer.session_id, ChatMessage.id < answer_id)
                .order_by(ChatMessage.id.asc()))).scalars().all()
            if not before or before[-1].role != "user":
                continue
            cases.append({
                "message_id": answer_id,
                "history": [(m.role, m.content) for m in before[:-1]],
                "question": before[-1].content,
                "verdict": meta.get("verdict"),
                "original": meta.get("answer", ""),
                "correction": meta.get("correction", ""),
                "note": meta.get("note", ""),
            })
        return cases


def _parse_sse(raw: str) -> dict:
    done: dict = {}
    for block in raw.split("\n\n"):
        event = re.search(r"^event: (\S+)", block, re.M)
        data = re.search(r"^data: (.*)$", block, re.M)
        if event and data and event.group(1) in ("done", "error"):
            done = {"event": event.group(1), **json.loads(data.group(1))}
    return done


def _shown(done: dict) -> list[str]:
    titles = []
    for key in ("products", "outfit"):
        for item in (done.get(key) or {}).get("items") or []:
            if item.get("title"):
                titles.append(item["title"])
    return titles


async def _ask(case: dict, with_lessons: bool) -> dict:
    """Send the case's message through the real chat endpoint in a scratch session."""
    from app.api.v1.endpoints.support import SupportChatRequest, support_chat

    session_id = SCRATCH_PREFIX + uuid.uuid4().hex[:20]
    async with AsyncSessionLocal() as db:
        db.add_all(ChatMessage(session_id=session_id, role=r, content=c) for r, c in case["history"])
        await db.commit()
    switch = lessons.enabled.set(with_lessons)
    try:
        response = await support_chat(SupportChatRequest(message=case["question"], session_id=session_id))
        chunks = []

        async def read() -> None:
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, str) else chunk.decode())

        await asyncio.wait_for(read(), timeout=TURN_TIMEOUT)
        done = _parse_sse("".join(chunks))
        if done.get("event") != "done":
            return {"reply": "", "shown": [], "error": done.get("message") or "no answer"}
        return {"reply": done.get("reply", ""), "shown": _shown(done)}
    except asyncio.TimeoutError:
        return {"reply": "", "shown": [], "error": "timed out"}
    finally:
        lessons.enabled.reset(switch)
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
            await db.commit()


async def _judge(case: dict, new: dict) -> dict:
    if new.get("error"):
        return {"pass": False, "reason": f"No answer: {new['error']}"}
    from app.agent.base import build_llm
    from app.api.v1.endpoints.support import _without_cards_note

    convo = "\n".join(f"{r}: {_without_cards_note(c)[:600]}" for r, c in case["history"][-8:])
    review = (f"Owner APPROVED this original answer:\n{case['original']}" if case["verdict"] == "good" else
              f"Owner marked this original answer WRONG:\n{case['original']}\n"
              f"Owner's correction: {case['correction'] or '(none)'}")
    if case["note"]:
        review += f"\nOwner's note: {case['note']}"
    human = (f"Conversation so far:\n{convo or '(none)'}\nShopper: {case['question']}\n\n{review}\n\n"
             f"NEW answer:\n{new['reply']}\nProducts shown with it: {', '.join(new['shown']) or 'none'}")
    try:
        answer = await asyncio.wait_for(build_llm(temperature=0, max_tokens=300).ainvoke(
            [("system", _JUDGE), ("human", human)]), timeout=JUDGE_TIMEOUT)
        text = answer.content if isinstance(answer.content, str) else str(answer.content)
        found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return {"pass": bool(found.get("pass")), "reason": str(found.get("reason", ""))[:300]}
    except Exception:  # noqa: BLE001 - an unjudged case is reported, not hidden
        logger.warning("Could not judge replay case %s", case["message_id"], exc_info=True)
        return {"pass": False, "reason": "Could not be judged"}


async def _run_case(case: dict, mode: str, gate: asyncio.Semaphore) -> dict:
    async with gate:
        out = {"message_id": case["message_id"], "question": case["question"], "verdict": case["verdict"]}
        new = await _ask(case, with_lessons=True)
        out["with_lessons"] = {"reply": new["reply"], "shown": new["shown"], **await _judge(case, new)}
        if mode == "both":
            new = await _ask(case, with_lessons=False)
            out["without_lessons"] = {"reply": new["reply"], "shown": new["shown"], **await _judge(case, new)}
        return out


def _score(results: list[dict], key: str) -> dict | None:
    judged = [r[key] for r in results if key in r]
    return {"passed": sum(1 for j in judged if j["pass"]), "total": len(judged)} if judged else None


async def _run(mode: str) -> None:
    started = datetime.now(timezone.utc).isoformat()
    try:
        cases = await _cases()
        await _store({"status": "running", "mode": mode, "started": started, "total": len(cases), "results": []})
        gate = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*(_run_case(c, mode, gate) for c in cases))
        await _store({
            "status": "finished", "mode": mode, "started": started,
            "finished": datetime.now(timezone.utc).isoformat(),
            "with_lessons": _score(results, "with_lessons"),
            "without_lessons": _score(results, "without_lessons"),
            "results": results,
        })
    except Exception as exc:  # noqa: BLE001 - report the failure on the page
        logger.exception("Replay test failed")
        await _store({"status": "failed", "mode": mode, "started": started, "error": str(exc)[:300]})


def start(mode: str = "with") -> bool:
    """Start a run in the background. False if one is already running."""
    global _task
    if running():
        return False
    _task = asyncio.create_task(_run(mode))
    return True

"""Conversation persistence for voice and text.

Voice and text write to the same store, so a caller can start by speaking, reload
the page, and carry on by typing with the thread intact.

The reason this is server-side rather than just kept in the browser: **the
Realtime API is stateless**. Each WebRTC session starts blank. If a call drops —
network blip, closed tab, expired token — reconnecting gives you an agent with no
memory of the last five minutes, which on a phone call is the most obvious
possible failure. So every turn is persisted as it happens, and a resumed session
gets the prior context replayed into its instructions.

Tool turns are stored alongside user and assistant turns. That makes a replayed
transcript diagnosable: you can see *which lookup* produced a wrong answer rather
than only that the answer was wrong.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from kb import db

# How much history to replay into a resumed voice session. Long enough to keep a
# thread coherent, short enough not to crowd out the system prompt or slow the
# first response.
RESUME_TURNS = 12
# Text mode can afford more: no latency budget, and Claude's context is large.
TEXT_HISTORY_TURNS = 30
TITLE_MAX = 70


def create(*, mode: str = "text", conversation_id: str | None = None,
           user_id: str | None = None) -> dict[str, Any]:
    cid = conversation_id or str(uuid.uuid4())
    now = db.now()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations "
            "(id, user_id, mode, turn_count, started_at, updated_at) VALUES (?,?,?,0,?,?)",
            (cid, user_id, mode, now, now),
        )
    return {"id": cid, "mode": mode, "started_at": now}


def owned_by(conversation_id: str, user_id: str | None) -> bool:
    """True if this conversation belongs to this user.

    Rows created before user scoping have user_id NULL; the first user to touch
    one claims it, so legacy threads keep working without leaking to others.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not row:
            return False
        if row["user_id"] is None:
            conn.execute(
                "UPDATE conversations SET user_id = ? WHERE id = ? AND user_id IS NULL",
                (user_id, conversation_id),
            )
            return True
    return row["user_id"] == user_id


def ensure(conversation_id: str | None, *, mode: str = "text",
           user_id: str | None = None) -> str:
    """Return a usable conversation id owned by this user, creating one if needed.

    Accepts a client-supplied id so the browser can keep the same thread across
    reloads via localStorage. If the id exists but belongs to a *different*
    user, a fresh conversation is minted instead — a guessed or stale id can
    never open someone else's thread.
    """
    if not conversation_id:
        return create(mode=mode, user_id=user_id)["id"]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    if row:
        if owned_by(conversation_id, user_id):
            return conversation_id
        return create(mode=mode, user_id=user_id)["id"]
    return create(mode=mode, conversation_id=conversation_id, user_id=user_id)["id"]


def add_turn(conversation_id: str, role: str, content: str | None, *,
             channel: str = "text", tool_name: str | None = None,
             tool_input: Any = None, tool_found: bool | None = None,
             language: str | None = None, user_id: str | None = None) -> int | None:
    """Append one turn. Returns the message id, or None if there was nothing to store."""
    if role not in ("user", "assistant", "tool"):
        return None
    if not (content or "").strip() and not tool_name:
        return None

    cid = ensure(conversation_id, mode=channel, user_id=user_id)
    now = db.now()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_name, "
            "tool_input, tool_found, channel, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, role, content, tool_name,
             json.dumps(tool_input, ensure_ascii=False) if tool_input is not None else None,
             None if tool_found is None else int(tool_found), channel, now),
        )

        # Title from the first user turn — what the history list shows.
        title_row = conn.execute(
            "SELECT title, mode FROM conversations WHERE id = ?", (cid,)
        ).fetchone()
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if role == "user" and content and not (title_row and title_row["title"]):
            title = " ".join(content.split())[:TITLE_MAX]
            updates.append("title = ?")
            params.append(title)
        # A conversation that saw both channels is 'mixed'.
        if title_row and title_row["mode"] not in (channel, "mixed"):
            updates.append("mode = 'mixed'")
        if language:
            updates.append("language = ?")
            params.append(language)
        if role in ("user", "assistant"):
            updates.append("turn_count = turn_count + 1")

        params.append(cid)
        conn.execute(
            f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?", params
        )
        return cur.lastrowid


def add_turns(conversation_id: str, turns: list[dict[str, Any]], *,
              channel: str = "voice", user_id: str | None = None) -> int:
    """Batch append. The voice frontend flushes several turns at once."""
    stored = 0
    for turn in turns:
        result = add_turn(
            conversation_id,
            turn.get("role", "user"),
            turn.get("content"),
            channel=turn.get("channel") or channel,
            tool_name=turn.get("tool_name"),
            tool_input=turn.get("tool_input"),
            tool_found=turn.get("tool_found"),
            language=turn.get("language"),
            user_id=user_id,
        )
        stored += bool(result)
    return stored


def history(conversation_id: str, *, limit: int = TEXT_HISTORY_TURNS,
            include_tools: bool = False) -> list[dict[str, Any]]:
    """Most recent turns, oldest-first (the order a model expects)."""
    roles = ("user", "assistant", "tool") if include_tools else ("user", "assistant")
    placeholders = ", ".join("?" for _ in roles)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT role, content, tool_name, tool_input, tool_found, channel, created_at
              FROM messages
             WHERE conversation_id = ? AND role IN ({placeholders})
             ORDER BY id DESC LIMIT ?
            """,
            (conversation_id, *roles, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def transcript(conversation_id: str) -> dict[str, Any]:
    """Everything needed to rehydrate the UI, tool notes included."""
    with db.connect() as conn:
        meta = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not meta:
            return {"found": False, "id": conversation_id}
        rows = conn.execute(
            "SELECT role, content, tool_name, tool_input, tool_found, channel, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return {
        "found": True,
        "conversation": dict(meta),
        "messages": [dict(r) for r in rows],
    }


def recent(*, user_id: str | None = None, limit: int = 25,
           include_archived: bool = False,
           include_empty: bool = False) -> list[dict[str, Any]]:
    """This user's conversations, newest first. Never lists anyone else's.

    Empty ones are hidden by default. A row is created whenever the page loads or
    someone taps the voice button and hangs up before speaking, so listing them
    would fill the panel with untitled zero-turn entries.
    """
    clauses = ["user_id = ?"]
    params: list[Any] = [user_id]
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if not include_empty:
        clauses.append("turn_count > 0")

    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, mode, language, turn_count, started_at, updated_at
              FROM conversations WHERE {' AND '.join(clauses)}
             ORDER BY updated_at DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_empty(*, older_than_hours: int = 6) -> int:
    """Delete stale zero-turn conversations. Called on startup."""
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE turn_count = 0 "
            "AND updated_at < datetime('now', ?)",
            (f"-{int(older_than_hours)} hours",),
        )
    return cur.rowcount


def delete(conversation_id: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        # messages cascade via the FK
    return cur.rowcount > 0


def archive(conversation_id: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE conversations SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (db.now(), conversation_id),
        )
    return cur.rowcount > 0


# A turn longer than this is summarised down. Fee answers in text mode run to
# several hundred characters of table; replaying them whole would crowd out the
# system prompt and slow the first spoken response.
RESUME_CHARS_PER_TURN = 320

_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MD_DIVIDER = re.compile(r"^\s*\|?[\s:|-]{6,}\|?\s*$", re.MULTILINE)
_MD_NOISE = re.compile(r"[*_#`>]+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _speakable(text: str) -> str:
    """Flatten markdown to something a voice model can absorb.

    A text-mode answer is full of pipe tables, bold markers and links. Replaying
    that verbatim into a *voice* session hands the model layout it can't speak and
    will sometimes read aloud. Tables collapse to comma-separated cells, links to
    their label, emphasis markers vanish.
    """
    if not text:
        return ""
    text = _MD_DIVIDER.sub(" ", text)
    text = _MD_TABLE_ROW.sub(
        lambda m: " " + ", ".join(
            cell.strip() for cell in m.group(0).strip().strip("|").split("|") if cell.strip()
        ) + ".",
        text,
    )
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_NOISE.sub("", text)
    return " ".join(text.split())


def resume_block(conversation_id: str, *, turns: int = RESUME_TURNS) -> str | None:
    """Prior turns formatted for injection into a fresh Realtime session.

    The Realtime API has no server-side memory, so a reconnect needs the thread
    replayed as text. Framed as reference material rather than as instructions,
    and explicitly marked as a continuation, so the agent picks up where it left
    off instead of re-greeting the caller.
    """
    past = history(conversation_id, limit=turns)
    if not past:
        return None

    lines = []
    for turn in past:
        who = "Caller" if turn["role"] == "user" else "You"
        text = _speakable(turn["content"] or "")
        if not text:
            continue
        if len(text) > RESUME_CHARS_PER_TURN:
            text = text[:RESUME_CHARS_PER_TURN].rsplit(" ", 1)[0] + " …(truncated)"
        lines.append(f"{who}: {text}")
    if not lines:
        return None

    return (
        "## Continuing an earlier conversation\n\n"
        "This caller has already been speaking with you. Below is the recent "
        "transcript, oldest first. Treat it as established context: do not greet "
        "them again, do not re-introduce yourself, and do not re-ask anything "
        "they have already told you. If their next question refers back to "
        "something here (\"and at the Lahore campus?\"), resolve it against this "
        "transcript.\n\n"
        + "\n".join(lines)
    )

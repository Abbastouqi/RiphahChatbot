"""FastAPI backend for the Riphah voice agent.

Routes:
  GET  /                    -> the voice UI
  GET  /api/health          -> knowledge-base readiness
  POST /api/realtime/session-> mint a short-lived OpenAI Realtime credential
  POST /api/tools/{name}    -> execute a knowledge-base tool (called by the browser)
  POST /api/chat            -> text-mode agent over the same tools (Claude)

Why the browser calls back for tools: with WebRTC the audio and the data channel
run directly between the browser and OpenAI, so function calls surface in the
browser. The browser has no database access and must not have an API key, so it
posts the call here, this server runs the query, and the browser returns the
result over its data channel. The OpenAI key never leaves this process — the
browser only ever holds a ~10-minute ephemeral credential.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
from agent import conversations, prompts, tools
from kb import db
from kb.vector_store import STORE

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Riphah Voice Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,   # set ALLOWED_ORIGINS in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ access control

# Endpoints reachable without a developer key: the bundled UI, its docs, and
# the health probe (monitoring shouldn't need a secret).
_PUBLIC_PATHS = {"/", "/api/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Developer-key gate. Active only when API_KEYS is set in the environment.

    Integrating clients send `X-API-Key: <key>`. The bundled frontend prompts
    for the key once and stores it in localStorage.
    """
    if (config.API_KEYS
            and request.url.path.startswith("/api/")
            and request.url.path not in _PUBLIC_PATHS
            and request.method != "OPTIONS"          # CORS preflight has no headers
            and request.headers.get("X-API-Key") not in config.API_KEYS):
        return JSONResponse({"detail": "missing or invalid X-API-Key"}, status_code=401)
    return await call_next(request)


def user_of(x_user_id: str | None) -> str:
    """Resolve the per-end-user identity every conversation is scoped to.

    Clients send `X-User-Id`, an opaque stable id they mint per end user (the
    bundled frontend generates a UUID in localStorage). Requests without one
    share a single 'anonymous' bucket rather than failing — but real
    integrations should always send it.
    """
    uid = (x_user_id or "").strip()[:64]
    return uid or "anonymous"


@app.on_event("startup")
def _warm() -> None:
    """Load the embedding matrix now so the first question isn't slow."""
    db.migrate()
    try:
        STORE.load()
        print(f"[startup] vector store ready: {STORE.size} chunks")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] vector store unavailable: {exc}")
    pruned = conversations.prune_empty()
    if pruned:
        print(f"[startup] pruned {pruned} empty conversations")


# ------------------------------------------------------------------------ health

@app.get("/api/health")
def health() -> dict[str, Any]:
    counts = db.counts()
    ready = counts["chunks_embedded"] > 0 and counts["fees"] > 0
    return {
        "ready": ready,
        "knowledge_base": counts,
        "vector_store_size": STORE.size,
        "models": {
            "realtime": config.REALTIME_MODEL,
            "embeddings": f"{config.EMBED_MODEL} @ {config.EMBED_DIMENSIONS}d",
            "text_agent": {
                "openai": f"openai:{config.OPENAI_TEXT_MODEL}",
                "ollama": f"ollama:{config.OLLAMA_TEXT_MODEL}",
            }.get(config.TEXT_PROVIDER, config.CLAUDE_MODEL),
        },
        # Length-checked, because .env.example ships literal "sk-..." placeholders
        # and reporting those as present sends you debugging the wrong thing.
        "keys_present": {
            "openai": len(os.getenv("OPENAI_API_KEY", "")) > 25,
            "anthropic": len(os.getenv("ANTHROPIC_API_KEY", "")) > 25,
        },
        # Whether the configured TEXT_PROVIDER can actually serve /api/chat —
        # the frontend gates the send button on this, not on any single key.
        "text_chat_ready": {
            "openai": len(os.getenv("OPENAI_API_KEY", "")) > 25,
            "anthropic": len(os.getenv("ANTHROPIC_API_KEY", "")) > 25,
            "ollama": True,
        }.get(config.TEXT_PROVIDER, False),
        "hint": None if ready else "Run: python -m kb.build",
    }


# --------------------------------------------------------------- realtime session

class SessionRequest(BaseModel):
    voice: str | None = None
    conversation_id: str | None = Field(
        default=None,
        description="Resume this conversation. Prior turns are replayed into the "
                    "session's instructions, because the Realtime API keeps no "
                    "state of its own across connections.",
    )
    language_hint: str | None = Field(
        default=None,
        description="Optional starting language, e.g. 'ur'. The agent follows the "
                    "caller regardless.",
    )


@app.post("/api/realtime/session")
async def realtime_session(payload: SessionRequest | None = None,
                           x_user_id: str | None = Header(None)) -> dict[str, Any]:
    """Mint an ephemeral Realtime credential the browser can use directly.

    The session is configured here — instructions, tools, transcription — so the
    browser cannot weaken the guardrails by editing its own session.update.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY is not configured on the server")

    payload = payload or SessionRequest()
    conversation_id = conversations.ensure(payload.conversation_id, mode="voice",
                                           user_id=user_of(x_user_id))

    extras: list[str] = []
    if payload.language_hint:
        extras.append(f"The caller is expected to start in language code "
                      f"'{payload.language_hint}'. Follow whatever they actually speak.")
    # Replay prior turns so a reconnect resumes rather than restarts.
    resumed = conversations.resume_block(conversation_id)
    if resumed:
        extras.append(resumed)
    extra = "\n\n".join(extras) or None

    session: dict[str, Any] = {
        "type": "realtime",
        "model": config.REALTIME_MODEL,
        "instructions": prompts.system_prompt(extra=extra),
        "tools": tools.openai_tools(),
        "tool_choice": "auto",
        "audio": {
            "input": {
                # Transcription gives us the caller's words for the on-screen
                # transcript and the query log. Omitting `language` is deliberate:
                # pinning it would break mid-call language switching.
                "transcription": {"model": "whisper-1"},
                "turn_detection": {"type": "semantic_vad"},
            },
            "output": {"voice": payload.voice or config.REALTIME_VOICE},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Binds the identifier to the token, so the browser never sends it.
                "OpenAI-Safety-Identifier": "riphah-voice-agent",
            },
            json={"session": session},
        )

    if response.status_code >= 400:
        # Surface OpenAI's message verbatim — it names the offending field, which
        # is what you need when a session field is renamed upstream.
        raise HTTPException(response.status_code,
                            f"OpenAI rejected the session: {response.text[:600]}")

    data = response.json()
    return {
        "client_secret": data.get("value") or data.get("client_secret", {}).get("value"),
        "expires_at": data.get("expires_at"),
        "model": config.REALTIME_MODEL,
        # The browser POSTs its SDP offer here to establish the peer connection.
        "sdp_url": "https://api.openai.com/v1/realtime/calls",
        "conversation_id": conversation_id,
        "resumed": bool(resumed),
    }


# ------------------------------------------------------------------- tool bridge

class ToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/tools/{tool_name}")
def run_tool(tool_name: str, payload: ToolRequest | None = None) -> JSONResponse:
    if tool_name not in tools.DISPATCH:
        raise HTTPException(404, f"unknown tool '{tool_name}'")
    args = (payload.arguments if payload else {}) or {}
    return JSONResponse(tools.execute(tool_name, args))


@app.get("/api/tools")
def list_tools() -> dict[str, Any]:
    return {"tools": [
        {"name": s["name"], "description": s["description"][:160] + "..."}
        for s in tools.TOOL_SPECS
    ]}


# --------------------------------------------------------------------- text chat

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    # Kept for callers that manage their own history (the eval harness). When a
    # conversation_id is supplied, stored history wins — the client shouldn't
    # have to mirror state the server already holds.
    history: list[ChatMessage] = Field(default_factory=list)


@app.post("/api/chat")
def chat(payload: ChatRequest,
         x_user_id: str | None = Header(None)) -> dict[str, Any]:
    """Text-mode agent. Same tools, same grounding rules, Claude instead of voice.

    This exists so retrieval quality can be judged without a microphone — it is
    what eval/run_eval.py drives.
    """
    from agent.text_agent import answer

    text_on_openai = config.TEXT_PROVIDER == "openai"
    if config.TEXT_PROVIDER != "ollama":  # local models need no API key
        required_key = "OPENAI_API_KEY" if text_on_openai else "ANTHROPIC_API_KEY"
        if not os.getenv(required_key):
            raise HTTPException(500, f"{required_key} is not configured on the server")

    # ensure() re-mints the id if it belongs to a different user, so a stale or
    # guessed conversation_id can never read into someone else's history.
    conversation_id = conversations.ensure(payload.conversation_id, mode="text",
                                           user_id=user_of(x_user_id))
    prior = (
        conversations.history(conversation_id)
        if payload.conversation_id
        else [m.model_dump() for m in payload.history]
    )

    conversations.add_turn(conversation_id, "user", payload.message, channel="text",
                           user_id=user_of(x_user_id))
    try:
        result = answer(payload.message, history=prior)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        # A spend cap or rate limit is an operational condition, not a bug. Say so
        # in words the user can act on. Which key ran out decides both the advice
        # and whether voice (always OpenAI) is affected too.
        exhausted = ("usage limit" in message.lower()
                     or "credit balance" in message.lower()
                     or "insufficient_quota" in message)
        if exhausted and text_on_openai:
            raise HTTPException(
                503,
                "Text chat is unavailable: the OpenAI API key is out of quota. "
                "Voice runs on the same key, so it is down too. Add credits at "
                "platform.openai.com/settings/organization/billing.",
            ) from exc
        if exhausted:
            raise HTTPException(
                503,
                "Text chat is unavailable: the Anthropic API key has reached its "
                "usage limit. Raise the cap at console.anthropic.com, or set "
                "TEXT_PROVIDER=openai in .env to run text chat on OpenAI instead.",
            ) from exc
        if "rate_limit" in message.lower() or "429" in message:
            raise HTTPException(429, "The model is rate-limited; retry shortly.") from exc
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

    for step in result.get("trace", []):
        conversations.add_turn(
            conversation_id, "tool", None, channel="text",
            tool_name=step.get("tool"), tool_input=step.get("input"),
            tool_found=step.get("found"), user_id=user_of(x_user_id),
        )
    conversations.add_turn(conversation_id, "assistant", result.get("answer"),
                           channel="text", user_id=user_of(x_user_id))

    result["conversation_id"] = conversation_id
    return result


@app.post("/api/chat/stream")
def chat_stream(payload: ChatRequest,
                x_user_id: str | None = Header(None)) -> StreamingResponse:
    """Streaming text chat: Server-Sent Events, one JSON object per event.

        data: {"type":"delta","text":"..."}         -- append to the answer
        data: {"type":"tool","name":...,"found":..} -- a lookup ran
        data: {"type":"done","answer":...,"conversation_id":...}
        data: {"type":"error","detail":"..."}

    Same request body and headers as /api/chat; use this when the UI should
    render the reply as it is generated instead of after it is complete.
    """
    from agent.text_agent import stream_answer

    if config.TEXT_PROVIDER != "ollama":
        required_key = ("OPENAI_API_KEY" if config.TEXT_PROVIDER == "openai"
                        else "ANTHROPIC_API_KEY")
        if not os.getenv(required_key):
            raise HTTPException(500, f"{required_key} is not configured on the server")

    uid = user_of(x_user_id)
    conversation_id = conversations.ensure(payload.conversation_id, mode="text",
                                           user_id=uid)
    prior = (
        conversations.history(conversation_id)
        if payload.conversation_id
        else [m.model_dump() for m in payload.history]
    )
    conversations.add_turn(conversation_id, "user", payload.message,
                           channel="text", user_id=uid)

    def events():
        import json as _json

        def sse(obj: dict[str, Any]) -> str:
            return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"

        try:
            for kind, data in stream_answer(payload.message, history=prior):
                if kind == "delta":
                    yield sse({"type": "delta", "text": data})
                elif kind == "tool":
                    conversations.add_turn(
                        conversation_id, "tool", None, channel="text",
                        tool_name=data["tool"], tool_input=data["input"],
                        tool_found=data["found"], user_id=uid)
                    yield sse({"type": "tool", "name": data["tool"],
                               "found": data["found"]})
                else:  # done
                    conversations.add_turn(conversation_id, "assistant",
                                           data.get("answer"), channel="text",
                                           user_id=uid)
                    yield sse({"type": "done", "answer": data.get("answer", ""),
                               "conversation_id": conversation_id,
                               "rounds": data.get("rounds", 0)})
        except Exception as exc:  # noqa: BLE001 - stream errors go to the client as data
            yield sse({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      # tell nginx/proxies not to buffer the stream
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------- conversation history

class TurnIn(BaseModel):
    role: str
    content: str | None = None
    channel: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_found: bool | None = None
    language: str | None = None


class TurnsRequest(BaseModel):
    turns: list[TurnIn] = Field(default_factory=list)


@app.post("/api/conversations")
def new_conversation(mode: str = "text",
                     x_user_id: str | None = Header(None)) -> dict[str, Any]:
    return conversations.create(mode=mode, user_id=user_of(x_user_id))


@app.get("/api/conversations")
def list_conversations(limit: int = 25,
                       x_user_id: str | None = Header(None)) -> dict[str, Any]:
    return {"conversations": conversations.recent(
        user_id=user_of(x_user_id), limit=max(1, min(limit, 100)))}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str,
                     x_user_id: str | None = Header(None)) -> dict[str, Any]:
    # Someone else's id gets the same 404 as a nonexistent one — a probe can't
    # even learn that a conversation exists, let alone read it.
    if not conversations.owned_by(conversation_id, user_of(x_user_id)):
        raise HTTPException(404, "no such conversation")
    data = conversations.transcript(conversation_id)
    if not data.get("found"):
        raise HTTPException(404, "no such conversation")
    return data


@app.post("/api/conversations/{conversation_id}/turns")
def append_turns(conversation_id: str, payload: TurnsRequest,
                 x_user_id: str | None = Header(None)) -> dict[str, Any]:
    """Record voice turns.

    The voice transcript arrives in the browser (WebRTC is browser↔OpenAI), so the
    browser is what reports each turn here as it happens — rather than at the end
    of the call, which would lose everything if the tab closed mid-conversation.
    """
    if not conversations.owned_by(conversation_id, user_of(x_user_id)):
        raise HTTPException(404, "no such conversation")
    stored = conversations.add_turns(
        conversation_id, [t.model_dump() for t in payload.turns], channel="voice",
        user_id=user_of(x_user_id),
    )
    return {"conversation_id": conversation_id, "stored": stored}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str,
                        x_user_id: str | None = Header(None)) -> dict[str, Any]:
    if not conversations.owned_by(conversation_id, user_of(x_user_id)):
        raise HTTPException(404, "no such conversation")
    if not conversations.delete(conversation_id):
        raise HTTPException(404, "no such conversation")
    return {"deleted": conversation_id}


# --------------------------------------------------------------------- frontend

@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "agent.server:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )


if __name__ == "__main__":
    main()

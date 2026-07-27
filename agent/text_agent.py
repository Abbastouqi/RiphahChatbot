"""Text-mode agent: Claude + the same tools, for evaluation and debugging.

Voice is hard to iterate on — you can't diff an audio answer, and you can't run a
regression suite through a microphone. This module answers the same questions
through the same tools in text, so retrieval quality and grounding behaviour can
be measured before voice is in the loop.

It also doubles as the fallback surface if a browser can't get microphone access.
"""
from __future__ import annotations

import os
from typing import Any

import config
from agent import prompts, tools

MAX_TOOL_ROUNDS = 6      # generous: a fee comparison across campuses uses 3-4


def _client():
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set — add it to .env")
    return anthropic.Anthropic()


def _openai_client():
    from openai import OpenAI

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set — add it to .env")
    return OpenAI()


def _ollama_client():
    # Ollama speaks the OpenAI wire format; the key is ignored but required
    # by the client constructor.
    from openai import OpenAI

    return OpenAI(base_url=config.OLLAMA_BASE_URL, api_key="ollama")


def answer(question: str, *, history: list[dict[str, Any]] | None = None,
           max_rounds: int = MAX_TOOL_ROUNDS) -> dict[str, Any]:
    """Run the tool loop until the model stops calling tools.

    Returns the answer plus the full tool trace, which is what makes this useful
    for debugging: you can see whether a bad answer came from bad retrieval or
    from bad reasoning over good retrieval.

    TEXT_PROVIDER in .env picks the model: "anthropic" (Claude, the default),
    "openai", or "ollama" (local, free) — same tools, same prompt, same return
    shape either way.
    """
    if config.TEXT_PROVIDER == "openai":
        return _answer_openai(question, history=history, max_rounds=max_rounds)
    if config.TEXT_PROVIDER == "ollama":
        return _answer_openai(question, history=history, max_rounds=max_rounds,
                              client=_ollama_client(), model=config.OLLAMA_TEXT_MODEL)
    return _answer_anthropic(question, history=history, max_rounds=max_rounds)


def _answer_openai(question: str, *, history: list[dict[str, Any]] | None = None,
                   max_rounds: int = MAX_TOOL_ROUNDS,
                   client=None, model: str | None = None) -> dict[str, Any]:
    import json

    client = client or _openai_client()
    model = model or config.OPENAI_TEXT_MODEL
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.text_system_prompt()},
    ]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    trace: list[dict[str, Any]] = []
    tool_definitions = tools.openai_chat_tools()

    for _ in range(max_rounds):
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=4000,
            tools=tool_definitions,
            messages=messages,
        )
        message = response.choices[0].message
        tool_calls = message.tool_calls or []

        if not tool_calls:
            details = getattr(response.usage, "prompt_tokens_details", None)
            return {
                "answer": (message.content or "").strip(),
                "trace": trace,
                "rounds": len(trace),
                "usage": {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                    "cache_read": getattr(details, "cached_tokens", 0) or 0,
                },
            }

        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [call.model_dump() for call in tool_calls],
        })
        for call in tool_calls:
            result = tools.execute(call.function.name, call.function.arguments)
            trace.append({
                "tool": call.function.name, "input": call.function.arguments,
                "found": result.get("found", None),
                "result_preview": json.dumps(result, ensure_ascii=False)[:400],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)[:20000],
            })

    return {
        "answer": "I wasn't able to complete that lookup. Please contact the Riphah "
                  "admissions office at riphah.edu.pk/contact.",
        "trace": trace,
        "exhausted": True,
    }


def _answer_anthropic(question: str, *, history: list[dict[str, Any]] | None = None,
                      max_rounds: int = MAX_TOOL_ROUNDS) -> dict[str, Any]:
    import json

    client = _client()
    messages: list[dict[str, Any]] = []
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    trace: list[dict[str, Any]] = []
    tool_definitions = tools.anthropic_tools()

    for _ in range(max_rounds):
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4000,
            system=[{
                "type": "text",
                "text": prompts.text_system_prompt(),
                # The prompt and tool list are byte-identical every call, so cache
                # the prefix and pay for it once.
                "cache_control": {"type": "ephemeral"},
            }],
            tools=tool_definitions,
            messages=messages,
        )

        if response.stop_reason == "refusal":
            return {"answer": "I can't help with that request.",
                    "refused": True, "trace": trace}

        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text")
            return {
                "answer": text.strip(),
                "trace": trace,
                "rounds": len(trace),
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
                },
            }

        results = []
        for call in tool_uses:
            result = tools.execute(call.name, dict(call.input))
            trace.append({"tool": call.name, "input": dict(call.input),
                          "found": result.get("found", None),
                          "result_preview": json.dumps(result, ensure_ascii=False)[:400]})
            results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)[:20000],
            })
        # All results for a parallel batch go back in one user message.
        messages.append({"role": "user", "content": results})

    return {
        "answer": "I wasn't able to complete that lookup. Please contact the Riphah "
                  "admissions office at riphah.edu.pk/contact.",
        "trace": trace,
        "exhausted": True,
    }


def main() -> None:
    """Interactive REPL:  python -m agent.text_agent"""
    import sys

    print("Riphah text agent. Ctrl-D to exit.\n")
    history: list[dict[str, Any]] = []
    while True:
        try:
            question = input("you  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        try:
            result = answer(question, history=history)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {type(exc).__name__}: {exc}\n", file=sys.stderr)
            continue

        for step in result.get("trace", []):
            print(f"       [{step['tool']}({step['input']}) -> found={step['found']}]")
        print(f"agent> {result['answer']}\n")
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": result["answer"]})


if __name__ == "__main__":
    main()

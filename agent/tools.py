"""Tool definitions and dispatch.

One schema list, rendered into two wire formats:
  * OpenAI Realtime — flat {type: "function", name, description, parameters}
  * Anthropic Messages — {name, description, input_schema}

Descriptions are written prescriptively ("Call this when...") rather than
descriptively, because that measurably raises should-call rate: the model decides
from the description whether a question needs a lookup at all.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from kb import retrieve

# --------------------------------------------------------------- specifications

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_riphah_knowledge_base",
        "description": (
            "Search Riphah's website content for anything not covered by the more "
            "specific tools: departments and faculties, facilities, hostels, "
            "scholarships, societies and student life, research centres, "
            "accreditation, leadership, the application process, transport, "
            "libraries, or university history. Call this for any open-ended "
            "question about Riphah. ALWAYS pass the query in English even when the "
            "user is speaking Urdu, Pashto, Punjabi or Arabic — the knowledge base "
            "is English. Returns ranked passages with their source URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query in English. Translate the user's "
                                   "question if they asked in another language.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many passages to return (1-10). Default 6.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_fee_structure",
        "description": (
            "Get the exact first-semester fee structure for a specific program. "
            "Call this for ANY question about cost, fees, tuition, per-credit-hour "
            "rates, or how much a degree costs — never answer a fee question from "
            "memory. Returns amounts already formatted with their currency (PKR for "
            "Pakistani nationals, USD for international students), a per-item "
            "breakdown, the notes about hostel and tax exclusions, and the date the "
            "figures were last verified. Fees differ by campus, so pass a campus if "
            "the user named one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "program": {
                    "type": "string",
                    "description": "Program name, abbreviation, or code in English — "
                                   "'MBBS', 'BS Computer Science', 'BSSE', 'Pharm-D'.",
                },
                "campus": {
                    "type": "string",
                    "description": "Optional campus filter: 'I-14', 'Al-Mizan', "
                                   "'Gulberg Green', 'Raiwind', 'Malakand', or a "
                                   "city name. Omit to get every campus.",
                },
                "applies_to": {
                    "type": "string",
                    "description": "Optional: 'Pakistani Nationals' or "
                                   "'International Students'.",
                },
            },
            "required": ["program"],
        },
    },
    {
        "name": "get_program_info",
        "description": (
            "Get details of a specific program: eligibility and admission criteria, "
            "merit/selection criteria, duration, credit hours, which faculty and "
            "department runs it, and every campus that offers it with intake, seat "
            "count, timings and whether admission is currently open. Call this when "
            "someone asks whether they qualify, how long a program takes, what it "
            "covers, or where they can study it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "program": {
                    "type": "string",
                    "description": "Program name or abbreviation in English.",
                },
            },
            "required": ["program"],
        },
    },
    {
        "name": "list_programs",
        "description": (
            "Browse Riphah's catalog of 184 programs, filtered by level, faculty, or "
            "campus. Call this when someone asks what programs are offered, what "
            "they can study, or which degrees a faculty or campus has — i.e. when "
            "they have no specific program in mind yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "description": "Study level: 'undergraduate', 'graduate', "
                                   "'doctoral', 'certificate', or 'associate'.",
                },
                "faculty": {
                    "type": "string",
                    "description": "Faculty name or code: 'Computing', 'FC', "
                                   "'Health & Medical Sciences', 'Pharmaceutical'.",
                },
                "campus": {
                    "type": "string",
                    "description": "Campus or city name to restrict to.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_campus_programs",
        "description": (
            "List everything a particular campus or city offers. Call this for "
            "'what can I study in Lahore', 'which programs are at the Malakand "
            "campus', or when a user has picked a location and wants their options."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "campus": {
                    "type": "string",
                    "description": "Campus name or city: 'Lahore', 'I-14', "
                                   "'Malakand', 'Gulberg Green'.",
                },
                "level": {
                    "type": "string",
                    "description": "Optional level filter, e.g. 'Undergraduate'.",
                },
            },
            "required": ["campus"],
        },
    },
    {
        "name": "get_admission_dates",
        "description": (
            "Get admission deadlines, campaign windows, and academic calendar dates. "
            "Call this for any question about when to apply, application deadlines, "
            "when the semester starts, or whether admissions are still open. Dates "
            "are returned exactly as published — never reformat or infer a year."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intake": {
                    "type": "string",
                    "description": "Optional intake filter: 'Fall', 'Spring', "
                                   "'Fall 2026'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_contact_info",
        "description": (
            "Get campus addresses, phone numbers, emails, and the online application "
            "link. Call this whenever the user should speak to a human — including "
            "every time you cannot answer their question, and whenever they ask "
            "about their own application status, a fee waiver, or an exception."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "campus": {
                    "type": "string",
                    "description": "Optional campus or city to get the contact for.",
                },
            },
            "required": [],
        },
    },
]


# ------------------------------------------------------------------- dispatchers

def _tool_search(query: str, top_k: int = 6) -> dict[str, Any]:
    hits = retrieve.search(query, top_k=max(1, min(int(top_k or 6), 10)))
    retrieve.log_query(query, tool="search_riphah_knowledge_base", hit=bool(hits),
                       top_similarity=hits[0].get("similarity") if hits else None)
    if not hits:
        return {
            "found": False,
            "message": "Nothing in the knowledge base matches that. Tell the user "
                       "you don't have the information and offer the admissions "
                       "office contact.",
        }
    return {
        "found": True,
        "count": len(hits),
        "passages": [
            {
                "title": h["title"],
                "heading": h["heading"],
                "text": h["text"][:1500],
                "source_url": h["url"],
                "last_verified": (h.get("fetched_at") or "")[:10],
            }
            for h in hits
        ],
    }


DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "search_riphah_knowledge_base": _tool_search,
    "get_fee_structure": lambda program, campus=None, applies_to=None: (
        retrieve.fee_structure(program, campus=campus, applies_to=applies_to)
    ),
    "get_program_info": lambda program: retrieve.program_info(program),
    "list_programs": lambda level=None, faculty=None, campus=None: (
        retrieve.list_programs(level=level, faculty=faculty, campus=campus)
    ),
    "get_campus_programs": lambda campus, level=None: (
        retrieve.campus_offerings(campus, level=level)
    ),
    "get_admission_dates": lambda intake=None: retrieve.admission_dates(intake=intake),
    "get_contact_info": lambda campus=None: retrieve.contact_info(campus=campus),
}


def execute(name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    """Run a tool by name. Never raises — a tool error is returned as data so the
    model can recover and tell the user something useful."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {"error": "arguments were not valid JSON", "raw": arguments[:200]}

    handler = DISPATCH.get(name)
    if handler is None:
        return {"error": f"unknown tool '{name}'", "available": list(DISPATCH)}

    try:
        return handler(**(arguments or {}))
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the conversation
        return {"error": f"{type(exc).__name__}: {exc}"}


# ----------------------------------------------------------------- wire formats

def openai_tools() -> list[dict[str, Any]]:
    """Realtime API shape: type/name/description/parameters, flat."""
    return [
        {
            "type": "function",
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        }
        for spec in TOOL_SPECS
    ]


def openai_chat_tools() -> list[dict[str, Any]]:
    """Chat Completions shape: the function object nested under a "function" key
    (unlike Realtime, which flattens it)."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in TOOL_SPECS
    ]


def anthropic_tools() -> list[dict[str, Any]]:
    """Messages API shape: input_schema instead of parameters."""
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "input_schema": spec["parameters"],
        }
        for spec in TOOL_SPECS
    ]

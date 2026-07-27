# Riphah Chatbot — API Integration Guide

For developers integrating the Riphah assistant into a website or app
(examples in PHP/Laravel; any HTTP client works the same way).

**Base URL:** `http://YOUR-SERVER:8090` — ask the Riphah bot team for the
current host. Interactive API explorer: `http://YOUR-SERVER:8090/docs`

---

## Required headers on every request

| Header | Value | Purpose |
|---|---|---|
| `X-API-Key` | Issued to you by the bot team | Authenticates your app (401 without it, when enabled) |
| `X-User-Id` | A stable, opaque id **per end user** (UUID recommended) | Gives each user their own private chat history. Never reuse one id for all users. |
| `Content-Type` | `application/json` | On POST requests |

In Laravel, generate the user id once per user and persist it (e.g. in the
`users` table or session):

```php
$user->chatbot_uid ??= (string) Str::uuid();   // save it
```

---

## 1. Text chat — `POST /api/chat`

The main endpoint. Send a question, get a grounded answer.

**Request:**
```json
{
  "message": "What is the MBBS fee for Pakistani students?",
  "conversation_id": "optional — omit on first message"
}
```

**Response:**
```json
{
  "answer": "The first-semester MBBS fee at Al-Mizan Campus is PKR 2,450,000 ...",
  "conversation_id": "71ac08e9-d8ec-4052-bc1f-4ec5c0537475",
  "trace": [ {"tool": "get_fee_structure", "input": {...}, "found": true} ],
  "usage": {"input_tokens": 3197, "output_tokens": 191}
}
```

- **Save `conversation_id`** and send it back on the user's next message —
  that's what makes follow-ups work ("and at the Lahore campus?").
- `answer` is Markdown (tables, bold, links) — render it with any Markdown
  library, or strip it for plain text.
- Off-topic questions get: *"You can ask me questions related to Riphah
  International University."*
- Responses take 2–10 seconds (the model may do database lookups). Set your
  HTTP timeout to at least 60 s.

**Laravel example:**

```php
use Illuminate\Support\Facades\Http;

public function ask(Request $request)
{
    $resp = Http::withHeaders([
            'X-API-Key'  => config('services.riphahbot.key'),
            'X-User-Id'  => $request->user()->chatbot_uid,
        ])
        ->timeout(60)
        ->post(config('services.riphahbot.url') . '/api/chat', [
            'message'         => $request->input('message'),
            'conversation_id' => $request->input('conversation_id'), // null on first msg
        ]);

    return $resp->json();   // {answer, conversation_id, trace, usage}
}
```

`config/services.php`:
```php
'riphahbot' => [
    'url' => env('RIPHAHBOT_URL', 'http://YOUR-SERVER:8090'),
    'key' => env('RIPHAHBOT_KEY'),
],
```

---

## 2. Direct data lookups — `POST /api/tools/{name}` (no AI, instant, free)

If you just need Riphah data for a page (a fee table, a program list), call
the tool directly — no model involved, responses in ~100 ms.

```
POST /api/tools/get_fee_structure     {"arguments": {"program": "MBBS", "campus": "Al-Mizan"}}
POST /api/tools/get_program_info      {"arguments": {"program": "BS Computer Science"}}
POST /api/tools/list_programs         {"arguments": {"level": "undergraduate", "faculty": "Computing"}}
POST /api/tools/get_campus_programs   {"arguments": {"campus": "Lahore"}}
POST /api/tools/get_admission_dates   {"arguments": {}}
POST /api/tools/get_contact_info      {"arguments": {"campus": "G-7"}}
POST /api/tools/search_riphah_knowledge_base  {"arguments": {"query": "hostel facilities"}}
```

`GET /api/tools` lists them all with descriptions. Every fee amount comes
pre-formatted with its currency (`"PKR 2,450,000"` / `"USD 17,000"`).

---

## 3. Conversation history

```
GET    /api/conversations                 → this user's conversation list
GET    /api/conversations/{id}            → full transcript (owner only)
DELETE /api/conversations/{id}            → delete (owner only)
```

History is strictly scoped to the `X-User-Id` you send — a user can never
see another user's conversations; someone else's conversation id returns 404.

---

## 4. Voice mode — `POST /api/realtime/session`

Voice runs in the **end user's browser** (WebRTC to OpenAI), not through your
Laravel backend. The flow:

1. Browser JS calls `POST /api/realtime/session` with
   `{"conversation_id": "..."}` (and the two headers).
2. Response contains a short-lived token: `{client_secret, model, sdp_url, conversation_id}`.
3. The browser opens a WebRTC connection to `sdp_url` with that token, streams
   microphone audio, and receives spoken replies.
4. Tool calls surface in the browser and are relayed to `POST /api/tools/{name}`.

This is non-trivial to implement from scratch — **copy the reference
implementation** in [`frontend/index.html`](frontend/index.html) (functions
`startCall`, `handleServerEvent`, `handleFunctionCall`). The page must be
served over **HTTPS** (browsers block microphone access on HTTP).

---

## 5. Health — `GET /api/health` (public, no headers needed)

Returns `{"ready": true, ...}` plus knowledge-base counts. Use it for uptime
monitoring.

---

## Error handling

| Status | Meaning | What to do |
|---|---|---|
| 401 | Missing/invalid `X-API-Key` | Check the key |
| 404 | Conversation doesn't exist or isn't this user's | Start a new conversation |
| 429 | Model rate-limited | Retry after a few seconds |
| 503 | Upstream AI quota exhausted | Show "assistant temporarily unavailable"; alert the bot team |

Error responses are JSON: `{"detail": "human-readable explanation"}`.

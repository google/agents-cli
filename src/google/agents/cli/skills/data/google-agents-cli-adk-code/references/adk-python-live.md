# ADK Live and Voice Agents

Real-time voice and video agents built on ADK's **Gemini Live API Toolkit**
(`Runner.run_live` + `LiveRequestQueue`). Use this for low-latency spoken
conversation and interruption ("barge-in") — not for turn-based
request/response agents (use a normal `Agent` with `run_async` for those).

**Official docs:** [ADK Live Docs](https://adk.dev/live/index.md) ·
[Configuration](https://adk.dev/live/configuration/index.md) ·
[Tools](https://adk.dev/live/tools/index.md) ·
[Get started](https://adk.dev/live/get-started/index.md)

---

## 1. When to use Live

| Use `run_live` (Live) | Use `run_async` (normal) |
|-----------------------|--------------------------|
| Voice/phone assistants, live captioning | Chatbots, tools, batch/RAG |
| User can interrupt mid-response | One turn completes before the next |
| Continuous audio/video input | Discrete text/multimodal messages |
| Latency-critical spoken UX | Latency-tolerant |

> **Live agents can't be served over A2A, and can't be published to Gemini.**
> Both are request/response with no Live transport. This has
> consequences for a scaffolded project beyond the model line — see
> "Converting a scaffolded project to Live" next.

## 2. Converting a scaffolded project to Live

There is no Live scaffold template. Scaffold `adk_python` normally, then make these
edits **together** — they are all consequences of one decision, and stopping
after the first leaves a project whose `uv run pytest` fails.

| # | File | Edit |
|---|------|------|
| 1 | `app/agent.py` | Point `MODEL` at a Live model and, on Gemini platform, add `client_kwargs={"location": …}` to the `Gemini(...)` the scaffold already emits (see "Models"). No `.env` or Terraform change. |
| 2 | `app/fast_api_app.py` | Delete the `attach_a2a_routes` call and its imports; delete `app/app_utils/a2a.py` and the `a2a-sdk` dependency. `/run_live` comes from `get_fast_api_app` and is untouched. |
| 3 | `tests/integration/test_agent.py` | Rewrite onto `run_live` (below). |
| 4 | `tests/integration/test_server_e2e.py` | Repoint the readiness probe, drop the A2A and SSE tests, add a `/run_live` test (below). |
| 5 | — | Drop `agents-cli publish gemini` from the plan; the Gemini app has no Live transport to register against. |

Steps 3 and 4 are not optional cleanup. Both scaffolded integration tests drive
the **non-live** path, and a Live model doesn't degrade to text — it rejects it,
so `runner.run()` and `/run_sse` fail outright. Rewrite the tests; don't delete
them.

### `tests/integration/test_agent.py`

Same shape as the scaffolded test, but driven by `run_live` and asserting on the
transcript rather than on text parts:

```python
import asyncio

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent

APP_NAME = "test"
USER_ID = "test_user"


def test_agent_live_stream() -> None:
    """The agent answers over run_live and returns audio plus a transcript."""

    async def _turn() -> tuple[str, int]:
        session_service = InMemorySessionService()
        # The session must exist before run_live, or it raises "Session not found".
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID
        )
        runner = Runner(
            agent=root_agent, session_service=session_service, app_name=APP_NAME
        )
        queue = LiveRequestQueue()
        queue.send_content(
            types.Content(role="user", parts=[types.Part(text="Why is the sky blue?")])
        )

        transcript, audio_chunks = "", 0
        try:
            async for event in runner.run_live(
                user_id=USER_ID,
                session_id=session.id,
                live_request_queue=queue,
                # Transcription is on by default — see "RunConfig".
                run_config=RunConfig(response_modalities=["AUDIO"]),
            ):
                # Consume the finished aggregate; partials would duplicate text.
                if (
                    event.output_transcription
                    and event.output_transcription.finished
                    and event.output_transcription.text
                ):
                    transcript += event.output_transcription.text
                if event.content and event.content.parts:
                    audio_chunks += sum(
                        1 for part in event.content.parts if part.inline_data
                    )
                if event.turn_complete:
                    break
        finally:
            queue.close()
        return transcript, audio_chunks

    transcript, audio_chunks = asyncio.run(_turn())
    assert transcript.strip(), "Expected a non-empty output transcript"
    assert audio_chunks > 0, "Expected the agent to return audio"
```

### `tests/integration/test_server_e2e.py`

Three edits, and the first is the one that's easy to miss:

- **Repoint the readiness probe.** `wait_for_server()` polls `AGENT_CARD_URL`,
  which step 2 deleted — use `f"{BASE_URL}/list-apps"` instead. Miss this and
  the probe raises `NameError`, which the surrounding `except RequestException`
  does **not** catch, so every test in the file errors before the server is even
  contacted. (`/list-apps` is a sound readiness signal: uvicorn serves no
  request until the lifespan startup completes.)
- **Delete `test_adk_run_sse`, `test_a2a_chat_stream`, and `test_agent_card`**,
  and add the `/run_live` test below. Those three are the whole file, so the
  new test replaces them rather than joining them.
- **On the `agent_runtime` target only**, also delete
  `test_reasoning_engine_stream` — `async_stream_query` is the non-live path
  too.

```python
import asyncio
import json
import uuid

import requests
from websockets.asyncio.client import connect

APP_NAME = "app"  # the basename of your agent_directory
BASE_URL = "http://127.0.0.1:8000"
HEADERS = {"Content-Type": "application/json"}


def test_adk_run_live(server_fixture) -> None:
    """Test the native ADK Live route (/run_live) end to end."""
    user_id = f"user_{uuid.uuid4()}"
    # /run_live closes with 1002 if the session doesn't already exist.
    session_response = requests.post(
        f"{BASE_URL}/apps/{APP_NAME}/users/{user_id}/sessions",
        headers=HEADERS,
        json={},
        timeout=60,
    )
    assert session_response.status_code == 200
    session_id = session_response.json()["id"]

    ws_url = (
        f"ws://127.0.0.1:8000/run_live?app_name={APP_NAME}&user_id={user_id}"
        f"&session_id={session_id}&modalities=AUDIO"
    )

    async def _turn() -> list[dict]:
        events: list[dict] = []
        async with connect(ws_url) as websocket:
            await websocket.send(
                json.dumps(
                    {"content": {"role": "user", "parts": [{"text": "Hi!"}]}}
                )
            )
            while True:
                raw = await asyncio.wait_for(websocket.recv(), timeout=60)
                event = json.loads(raw)
                events.append(event)
                # A tool round-trip emits an extra turnComplete before the
                # answer; a tool-using agent must keep reading past the first.
                if event.get("turnComplete"):
                    break
            await websocket.send(json.dumps({"close": True}))
        return events

    events = asyncio.run(_turn())
    assert events, "No events received from /run_live"
    assert any(
        (transcription := event.get("outputTranscription"))
        and transcription.get("finished")
        and transcription.get("text")
        for event in events
    ), "Expected a finished output transcription"
```

Frame details: the local server sends serialized events as **text** frames (see
"Events"), so keys are camelCase and absent means unset — `.get()` is the right
accessor, not truthiness on a guaranteed key. Only Agent Runtime in Gemini platform also delivers
event JSON as binary frames — see "Deployed URL shapes". `websockets` is
already available via `google-genai`.

## 3. Models

Every supported Live model is **native audio** — audio in, audio out, end to
end, with no intermediate text-to-speech stage.

| Model | Gemini platform | AI Studio | Use for |
|---|---|---|---|
| `gemini-3.8-live` | Supported | Supported | **Default.** Low-latency dialogue, interleaved reasoning, no reasoning-induced pauses. |
| `gemini-3.8-live-extended-thinking` | Supported | Supported | Complex, multi-step problem solving mid-conversation. Runs background reasoning and async tool calls while it keeps streaming audio, trading latency for depth. |
| `gemini-live-2.5-flash-native-audio` | Supported | — | Legacy. Pre-3.8 behavior. |

### What 3.8 changed

| Area | 3.8 behavior |
|---|---|
| Affective dialog | **Removed from the API.** `enable_affective_dialog` now errors. Delete it. |
| Proactive audio | **Permanently on.** `proactive_audio=False` errors. |
| Function calling | Async (`NON_BLOCKING`) is the default. `gemini-3.8-live` still accepts `BLOCKING`; `-extended-thinking` rejects it with a hard error. |
| Function scheduling | `SILENT` / `WHEN_IDLE` / `INTERRUPTED` work on `gemini-3.8-live`, and are unsupported on `-extended-thinking`. |
| Thinking | `gemini-3.8-live` takes no `thinking_level`; omit it. `-extended-thinking` takes `thinking_config` with `low`, `medium`, or `high` (not `MINIMAL`). |

> **`-extended-thinking` breaks the `turnComplete` assumption.** It keeps
> reasoning and calling tools after `turnComplete: true`, so that frame no
> longer means idle. Read `interaction_status`: `IN_PROGRESS` means more output
> is coming, `IDLE` ends the turn. A client that stops at `turnComplete`
> truncates the answer. `--mode adk_live` already handles this.

Being native audio has consequences the rest of this page depends on:

- **AUDIO-only output.** `response_modalities=["TEXT"]` is rejected — send
  `["AUDIO"]` and read the transcripts (see "Events").
- **Language is inferred**, not set — see "Voice and language".
- **Proactive audio is always on** in 3.8 (see "RunConfig").

Keep the model **and its region** env-overridable — Live model IDs change
often, and the two are coupled, since changing the model can force a different
region:

```python
import os
from google.adk.agents import Agent
from google.adk.models import Gemini

MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
# Gemini platform only — pins the model's region (see "Region" below). The Gemini API
# rejects `location` outright, so leave LIVE_LOCATION empty on AI Studio.
LIVE_LOCATION = os.getenv("LIVE_LOCATION", "us-central1")

root_agent = Agent(
    name="live_assistant",
    model=Gemini(
        model=MODEL,
        client_kwargs={"location": LIVE_LOCATION} if LIVE_LOCATION else None,
    ),
    instruction="You are a helpful voice assistant. Keep replies concise.",
)
```

Confirm the current ID before shipping against
`https://ai.google.dev/gemini-api/docs/live` (AI Studio) or
`https://cloud.google.com/vertex-ai/generative-ai/docs/live-api` (Gemini platform). Load
`.env` **before** importing the agent module, or `os.getenv` runs first and
silently uses the fallback.

**Region.** On Gemini platform, Live models are served from a **regional** endpoint such
as `us-central1`, **not** the `global` endpoint — so a valid model ID still
fails under the wrong location. Confirm yours in the model × region matrix at
[Google model endpoint locations](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations#google-models).

**Pin the region on the model, not in the environment.** `client_kwargs` is a
passthrough to the `genai.Client` constructor, and an explicit `location` there
beats `GOOGLE_CLOUD_LOCATION` — as in the example above. Leave it out and the
model falls back to `GOOGLE_CLOUD_LOCATION`, so an exported `global` (a common
default) silently sends Live to an endpoint that doesn't serve it. Pinning is
also the smaller change: `GOOGLE_CLOUD_LOCATION` is shared with sessions,
telemetry, and eval, and `agents-cli deploy` sets it to `global` on Agent
Runtime — so an env-only fix works locally and then breaks once deployed.

Two things to know about `client_kwargs`:

- **It is Gemini platform only.** On AI Studio, a `location` raises `ValueError: Gemini
  API does not support project/location.` The Live client is built lazily, so
  this surfaces on connect and looks like a transport failure. Keep the
  `if LIVE_LOCATION else None` guard if the same code runs on both backends.
- **Pass only `location` / `project`.** Its keys are merged *over* the ones ADK
  builds, so passing `http_options` replaces the object carrying Live's
  `api_version` and tracking headers.

> Don't reach for the `api_client` subclass override shown in ADK's `Gemini`
> docstring — Live reads a different client (`_live_api_client`), so that
> pattern silently does nothing here. `client_kwargs` is applied to both.

## 4. The agent

A Live agent is a normal `Agent` — the difference is how you *run* it, plus the
voice it speaks with and how you write for the ear rather than the eye.

```python
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import google_search

root_agent = Agent(
    name="live_assistant",
    model=Gemini(
        model="gemini-3.8-live",
        # Gemini platform only — see "Models"; omit on AI Studio.
        client_kwargs={"location": "us-central1"},
    ),
    instruction="You are a helpful voice assistant. Keep replies concise.",
    tools=[google_search],
)
```

### Writing the instruction for speech

The instruction is where most voice-agent quality comes from, and a chat
agent's instruction ported unchanged is the usual cause of a bad first demo.
Every reply is **spoken**, so:

- **Ban visual formatting.** Markdown, bullets, tables, emoji, and URLs get
  read aloud literally. Say so explicitly — models default to formatting.
- **Cap turn length.** Ask for a sentence or two, then a question. Long
  monologues make the user talk over the agent to get a word in.
- **Speak numbers, not glyphs.** Dates, currency, phone numbers, and IDs need
  spoken forms ("March third", "four one five…"), and long codes want
  grouping and pauses.
- **Confirm before acting on what it misheard.** Speech recognition mangles
  names, addresses, and account numbers; tell the agent to read critical
  values back rather than proceed on its best guess.
- **Cover the pauses.** Tell it to say what it's doing before a slow tool
  ("let me look that up"). Silence reads as a dropped call — see "Tools and
  latency" for the non-blocking patterns behind this.

Language choice is instruction-driven too — see "Voice and language".

### Voice and language (`speech_config`)

Set the voice **per agent** by passing a configured `Gemini` instance — the
usual choice, because it lets each agent in a multi-agent system have its own
voice:

```python
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

support_llm = Gemini(
    model="gemini-3.8-live",
    client_kwargs={"location": "us-central1"},  # Gemini platform only — see "Models"
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
        ),
        language_code="en-US",
    ),
)

support_agent = Agent(name="support", model=support_llm, instruction="...")
```

Or set one voice for the whole session via `RunConfig(speech_config=...)`.

Precedence runs agent-level (`Gemini(speech_config=…)`) → `RunConfig` → the
Live API default, so a per-agent voice always wins. Set a session default in
`RunConfig` and override it for the agents that need their own voice.

`language_code` (e.g. `"en-US"`, `"ja-JP"`) is a hint, not a switch: native
audio models may ignore it and infer the language from the conversation, so
say what language to speak in the agent's instruction when it matters. An
unsupported voice fails at **connection time**, so test your voice on the
target platform early.

## 5. Driving a turn: `run_live` + `LiveRequestQueue`

`run_live()` is an async generator; `LiveRequestQueue` is the upstream channel.
They run concurrently — you send input while receiving output.

```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.genai import types

session_service = InMemorySessionService()
runner = Runner(app_name="app", agent=root_agent, session_service=session_service)

async def one_turn(user_id: str, session_id: str, text: str):
    # Create the session first, or run_live raises "Session not found".
    queue = LiveRequestQueue()
    run_config = RunConfig(
        # One modality per session, fixed at start.
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    # Upstream: send the user's turn.
    queue.send_content(types.Content(parts=[types.Part(text=text)]))

    # Downstream: consume events until the turn completes.
    async for event in runner.run_live(
        user_id=user_id, session_id=session_id,
        live_request_queue=queue, run_config=run_config,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(event.author, part.text)
        if event.turn_complete:
            break
    queue.close()  # always close — never reuse a queue across sessions
```

Response modality constrains **output only**: you can always send text, audio,
or video input regardless of what the model replies with.

### Sending input
- **Text:** `queue.send_content(types.Content(parts=[types.Part(text=...)]))`
- **Audio (realtime):** `queue.send_realtime(types.Blob(mime_type="audio/pcm;rate=16000", data=pcm_bytes))`
- **Video frame:** `queue.send_realtime(types.Blob(mime_type="image/jpeg", data=frame_bytes))`
- **Close:** `queue.close()` (ends the session)

### Audio format and chunking

Input is **16-bit PCM, 16 kHz, mono**; output is **24 kHz**. ADK does no format
conversion — wrong rates produce garbled audio or a `1007` close. Stream
continuously in **50–100 ms** chunks rather than waiting for replies;
`LiveRequestQueue` forwards each chunk without batching.

## 6. RunConfig

| Field | Purpose |
|-------|---------|
| `response_modalities=["AUDIO"]` | One per session, fixed at start. Defaults to `["AUDIO"]`. Every supported Live model is native audio, so `["TEXT"]` is rejected. |
| `speech_config` | Voice + language — see "Voice and language". Overridden by agent-level config. |
| `input_audio_transcription` / `output_audio_transcription` | `types.AudioTranscriptionConfig()` — text transcripts of speech. **Default-on** via `default_factory`, and ADK's built-in `/run_live` doesn't override them, so transcripts flow with no config. Set to `None` to disable. |
| `realtime_input_config` | Turn-taking / VAD (below). |
| `proactivity` / `enable_affective_dialog` | Legacy models only; both error on 3.8 (below). |
| `session_resumption` / `context_window_compression` | See "Session lifetime and limits". |
| `save_live_blob` | Saves the user's and the model's audio as artifacts, for debugging or compliance. Only ADK Python 2.14 or higher saves the image frames the user sends, such as camera or screen-share frames. |

Full options: [Configuration](https://adk.dev/live/configuration/index.md).

### Turn-taking and VAD

Voice activity detection is **enabled by default**: just stream audio
continuously and the API decides when the user started and stopped talking,
including handling interruptions.

Disable it for push-to-talk, noisy/cross-talk environments, or when your client
does its own detection:

```python
run_config = RunConfig(
    response_modalities=["AUDIO"],
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=True
        )
    ),
)
```

With VAD off you **must** delimit each turn yourself:
`queue.send_activity_start()` before the first audio chunk,
`queue.send_activity_end()` after the last. The model only processes audio
between those signals, so mistimed signals cause dropped or ignored speech.

### Proactivity and affective dialog

On 3.8 neither is a knob: proactive audio is permanently on, and
`enable_affective_dialog` errors. Pass neither. Proactive audio is
**probabilistic**, so where predictability matters more than warmth, constrain
it in the instruction.

Legacy `gemini-live-2.5-flash-native-audio` takes both as opt-in config:

```python
run_config = RunConfig(
    response_modalities=["AUDIO"],
    proactivity=types.ProactivityConfig(proactive_audio=True),
    enable_affective_dialog=True,
)
```

### Multi-agent and workflow Live

Live is not limited to one agent. Four shapes work:

| Shape | How a stage ends | Use for |
|-------|------------------|---------|
| Single `Agent` | — | Most voice agents |
| Coordinator + `sub_agents` | `transfer_to_agent` | Route by intent to a specialist |
| `SequentialAgent` pipeline | `task_completed` | Fixed stages: research → write → review |
| Graph `Workflow` | stage completes its task | Explicit topology, typed handoffs |

`transfer_to_agent` hands the conversation to a specialist and the stream
continues in the **same** `run_live()` loop. `task_completed` ends one stage of
a `SequentialAgent`; it does *not* exit your loop — only the last agent
completing does. Use **one** `LiveRequestQueue` for the whole conversation;
never create one per agent.

A graph `Workflow` runs as a voice conversation with no structural change — the
only Live-specific part is the model on each stage. `mode='task'` is what makes
a stage run its own turn-taking loop and complete before handing off, which is
what you want for a staged voice call:

```python
from google.adk.agents import Agent
from google.adk.workflow import START, Workflow

greeter = Agent(model=LIVE_MODEL, name="greeter", mode="task",
                instruction="Greet the caller and confirm their name.")
verifier = Agent(model=LIVE_MODEL, name="verifier", mode="task",
                 instruction="Verify their date of birth.")

root_agent = Workflow(name="intake", edges=[(START, greeter), (greeter, verifier)])
```

Typed handoffs between stages (`output_schema`) and the rest of the graph API
are in `references/adk-python-workflows.md`. Working example:
[live_workflow sample](https://github.com/google/adk-python/tree/main/contributing/samples/live/live_workflow)
— which also evaluates the workflow in-process with ADK's audio user simulator.
To evaluate a Workflow over the wire instead, serve it and run
`agents-cli eval generate --mode adk_live`: the server drives `run_live`, so graph roots
need no special handling on the client.

Whenever an agent has `sub_agents`, ADK **forces audio transcription on** even
if you set it to `None` — agent transfer needs text context to pass along. Plan
on receiving transcription events. Give each agent its own voice (see "Voice
and language") so transitions are audible to the user.

> Built-in grounding tools (`google_search`, `google_maps_grounding`) are
> model-internal and don't mix with other tools on the same agent — a
> constraint that bites conversational voice roots especially hard. Put them on
> a grounding sub-agent instead; see `references/adk-python.md`, "Tools".

## 7. Events (`run_live` output)

Same `Event` model as `run_async`, plus Live flags. **Live emits control,
transcription, and terminator frames as their own content-less events** — a
consumer must not assume every frame carries `content`. Usage metadata and
voice-activity frames are content-less too; skip them.

- `event.turn_complete` — standalone, content-less frame; the model finished
  this turn (enable user input).
- `event.interrupted` — user barged in; may be standalone (no content) or
  attached to a final text response. Flush any buffered audio you haven't
  played yet, or the user hears stale speech over their own.
- `event.input_transcription` / `event.output_transcription` — speech text,
  each on its own content-less frame. Streamed as partial chunks
  (`finished=False`, `partial=True`) then a final aggregate (`finished=True`,
  `partial=False`); ADK also flushes the aggregate on turn_complete/interrupted.
  **Consume the finished aggregate; skip partials** to avoid duplicating text.
  Null-check twice: the object may exist with empty `.text`.
- `event.content.parts[].inline_data` — raw audio bytes (AUDIO modality).
- `event.author` — agent name for model output; `"user"` for input transcripts.
- `event.error_code` / `event.error_message` — failures (e.g. `SAFETY`, `MAX_TOKENS`).

Serialize for the wire with `event.model_dump_json(exclude_none=True, by_alias=True)`.
Full event handling (interruption, tool calls, error decision table):
[Events](https://adk.dev/live/events/index.md).

## 8. Session lifetime and limits

**ADK `Session` vs Live session.** The ADK `Session` (from `SessionService`) is
durable conversation storage that survives restarts. The Live session is
ephemeral: created when `run_live()` starts, destroyed when the queue closes.
Each new `run_live()` re-seeds a fresh Live session from the ADK `Session`
history — which is why conversations survive across calls.

**Default duration limits**, absent compression:

| | Gemini Live API | Agent Runtime |
|---|---|---|
| Audio-only session | 15 min | 10 min |
| Audio + video session | 2 min | 10 min |
| Connection | ~10 min (ADK reconnects transparently) | — |

**Session resumption** keeps a session alive across the connection timeout.
ADK caches the resumption handle and reconnects on its own — you write no
reconnection code. Enable it by default in production:

```python
run_config = RunConfig(
    response_modalities=["AUDIO"],
    session_resumption=types.SessionResumptionConfig(),
)
```

**Context window compression removes the session duration limit entirely** and
keeps long conversations inside the model's context window. Enable it when
calls can outlast the table above:

```python
run_config = RunConfig(
    response_modalities=["AUDIO"],
    session_resumption=types.SessionResumptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        trigger_tokens=100_000,  # ~78% of a 128k context
        sliding_window=types.SlidingWindow(target_tokens=80_000),
    ),
)
```

Trade-off: older turns are summarized rather than kept verbatim, so skip it
when precise recall of early conversation matters.

## 9. Serving and testing a Live agent

Live agents are reached over a **WebSocket** at `/run_live`, not `/run_sse` —
and there is **no server code to write**. `get_fast_api_app` serves that route
on every target, and the scaffold's `fast_api_app.py` already wires it up.
Work up this ladder and stop at the first rung that does the job.

**1. Talk to it in the playground.** `agents-cli playground` starts `adk web`,
whose dev UI has mic and camera controls wired to `/run_live`. This is the
first thing to do after pointing a project at a Live model — a real voice
conversation, no client code, no flags. The UI drives session defaults (AUDIO,
VAD on) and sends no session flags, so what you're validating here is the
agent itself: instruction, voice, tools, and barge-in.

**2. Script a turn.** `agents-cli run "<prompt>" --mode adk_live` runs one turn
and prints the transcript; `agents-cli eval run --mode adk_live` plays a
dataset's user turns over one persistent socket and grades the transcripts
(see `/google-agents-cli-eval`). Both connect with `modalities=AUDIO` and use a
**text-in, audio-out** turn model — one text `content` frame per user turn.
There is no flag to point them at a custom WebSocket route; see
`agents-cli run --help` and `agents-cli eval generate --help`.

**3. Write a *client*, not a server.** A browser UI, phone bridge, or device
client connects to the same built-in endpoint:
`ws://host/run_live?app_name=<app>&user_id=<uid>&session_id=<sid>&modalities=AUDIO`
— create the session over HTTP first. Upstream frames are `LiveRequest` JSON
(snake_case): `{"content": {...}}` for a discrete turn, `{"blob": {...}}` for
realtime mic audio, `{"close": true}` to end; with VAD off, bracket a streamed
turn as `{"activity_start": {}}` → one `{"blob": {...}}` per chunk →
`{"activity_end": {}}`. `LiveRequest` has no `realtime_input` field and does
not forbid extras, so that key is dropped silently and the audio never lands.
Downstream frames are serialized ADK `Event`s (binary on some targets, so
decode-as-JSON before treating bytes as audio).

**4. Only then, edit `fast_api_app.py`.** The file already exists — you are
adding a handler, not creating the server. Reach for this only when you need
something the rungs above can't express: a `RunConfig` field with no query
param (`context_window_compression`, disabling transcription,
`tool_thread_pool_config`), custom auth on the WebSocket handshake, or your own
binary wire protocol. Bridge your `@app.websocket("/ws")` route to `run_live` +
`LiveRequestQueue` (see
[Build a custom server](https://adk.dev/live/custom-server/index.md)); `agents-cli` boots
`fast_api_app.py` in preference to `adk api_server` for local serving.

> Voice selection is **not** a reason to write a handler — set it per agent
> with `Gemini(speech_config=…)`, as in "Voice and language".

**Where each decision lives.** What you build into the agent — model and its
region, voice, instruction, tools, sub-agents — ships with the deployment and
behaves the same for every caller. Per-session behavior does not: `RunConfig`
is an argument to `run_live`, and `App` has nowhere to hold one, so the client
opening the socket chooses it. On the built-in endpoint that means query
params (`modalities`, `explicit_vad_signal` for push-to-talk, and the rest of
section 6's fields that it exposes); rung 4 is for the fields it doesn't.
Design accordingly: anything that must hold for every caller belongs in agent
code, not in a URL.

### Deployed URL shapes (every target serves the same `/run_live`)

`get_fast_api_app` serves `/run_live` on a local server, Cloud Run, GKE, and
Agent Runtime alike — only the base URL differs. `agents-cli run`/`eval`
auto-detect these; the shapes are here for hand-rolled clients.

- **Local / Cloud Run / GKE:** `wss://<host>/run_live?app_name=…&user_id=…&session_id=…`
  (sessions over `https://<host>/apps/{APP}/users/{USER}/sessions`).
- **Agent Runtime** proxies the container behind an `/api` passthrough with a
  separate WebSocket ingress:
  - **WebSocket:** `wss://{LOC}-aiplatform.googleapis.com/reasoningEngines/ws/internal/{RESOURCE}/api/run_live?app_name=…&user_id=…&session_id=…`
  - **Session create (HTTP):** `POST https://{LOC}-aiplatform.googleapis.com/reasoningEngines/v1/{RESOURCE}/api/apps/{APP}/users/{USER}/sessions`
  - Auth is a standard `Authorization: Bearer <ADC token>` header on the handshake.
  - Only the WebSocket uses the separate `ws/internal` ingress; HTTP goes
    through the same `reasoningEngines/v1/{RESOURCE}/api` passthrough as every
    other container route (see `/google-agents-cli-deploy`,
    `references/agent-runtime.md`).
  - The managed `AdkApp` + `bidi_stream_query` path is a different mechanism and
    is **not** used for ADK Live agents.

## 10. Tools and latency

Live raises the stakes on tool latency: in a spoken conversation a pause over a
second reads as a dropped call, where the same pause in chat is invisible.
**Prefer non-blocking tools for anything that isn't reliably fast** — but only
when a tool can actually outrun the conversation; tools that return quickly
need no special handling.

| Expected tool latency | Pattern |
|----------------------|---------|
| Under ~500 ms | Plain `async def` tool — the pause is conversationally invisible |
| Seconds, bounded | `NON_BLOCKING` + `response_scheduling` |
| Open-ended / continuous | Streaming tool (`AsyncGenerator`) |
| Sync/blocking library | `RunConfig.tool_thread_pool_config` |

**Non-blocking tools.** ADK runs the tool in a background task and returns a
"pending" ack immediately so the model keeps talking, then feeds the real
result back into the live queue:

```python
from google.adk.tools import FunctionTool
from google.genai import types

my_tool = FunctionTool(my_async_fn)  # my_async_fn is `async def`
my_tool.response_scheduling = types.FunctionResponseScheduling.WHEN_IDLE
#   WHEN_IDLE  → speak the result when the caller is idle (typical)
#   SILENT     → apply the result without speaking (e.g. a state update)
#   INTERRUPT  → speak the result as soon as it's ready
```

`-extended-thinking` runs every tool non-blocking and rejects
`response_scheduling`; omit that line there.

Inside such a tool you can drive a multi-step **graph `Workflow`**
(`google.adk.workflow`) via `await tool_context.run_node(workflow,
node_input=...)`. Always `await` it directly — never wrap in
`asyncio.create_task`. See `references/adk-python-workflows.md`.

**Streaming tools.** An `async def` tool that `yield`s streams intermediate
results back into the conversation (e.g. monitor a price or a video feed). A
tool parameter named `input_stream: LiveRequestQueue` is a reserved hook ADK
fills with inbound realtime frames. You must also supply a
`stop_streaming(function_name: str)` tool so the model can end the stream. See
[Tools](https://adk.dev/live/tools/index.md).

**Blocking I/O.** For unavoidably blocking tools,
`RunConfig.tool_thread_pool_config` offloads them to a thread pool so the event
loop — and therefore barge-in — stays responsive.

## 11. Vision input (camera and screen share)

Images and video are both sent as individual **JPEG frames** on the same
socket — `send_realtime` in-process, a `blob` frame over the wire. There is no
video codec involved.

- **Frame rate:** 1 FPS recommended maximum.
- **Resolution:** 768×768 recommended.

Good for "look at this" moments: a user pointing a camera at a product, sharing
a screen for troubleshooting, or a device sending periodic stills. Note that
audio + video sessions have a much tighter default duration limit than
audio-only on Gemini Live API (see "Session lifetime and limits").

## 12. Deploy and evaluate

Deploy with `agents-cli deploy`, adding `--timeout 3600` on Cloud Run so a
session outlives the 300s default (`/google-agents-cli-deploy`). Evaluate with
`agents-cli eval run --mode adk_live` (`/google-agents-cli-eval`).

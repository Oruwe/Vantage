# employer-voice

Voice frontend for the Vantage manager agent. Connects a local microphone
and speaker to a running `shadow-replica` and lets a manager hold a spoken
conversation with the tool-calling agent that fronts the dashboard.

## Install

```
uv sync --all-packages --group dev
# For microphone + playback (optional — text mode works without it):
uv pip install "employer-voice[audio]"
```

## Setup

```
employer-voice init --manager-token vsm_<your-token> \
                    --replica-url http://127.0.0.1:7475
```

Put your OpenAI key in the env var the config points at
(`OPENAI_API_KEY` by default). Whisper is used for STT and OpenAI's `tts-1`
with the `alloy` voice is used for playback — change either in the config.

## Usage

```
# Text turn (no audio hardware required)
employer-voice ask "what's blocking Alice today?"

# Text turn with live tool-call stream
employer-voice ask "which repos saw collisions this week?" --stream

# Voice loop
employer-voice talk

# One turn then exit, skip playback
employer-voice talk --turns 1 --silent

# List and reset conversations
employer-voice conversations
employer-voice reset
```

The last `conversation_id` is persisted in the config so each new `ask` or
`talk` continues the previous chat. `reset` forgets it.

## Architecture

```
mic → sounddevice → wav → OpenAI Whisper → text
                                             │
                                             ▼
                            shadow-replica /ui/api/chat/stream  ← SSE
                                             │
                                   assistant text
                                             │
                              OpenAI TTS → mp3 → sounddevice → speaker
```

The whole audio stack (`sounddevice`, `soundfile`, `numpy`) is a soft import
behind the `[audio]` extra, so the package stays usable in CI and on
headless servers for text-only operation.

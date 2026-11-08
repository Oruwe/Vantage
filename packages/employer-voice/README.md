# employer-voice

Text chat frontend for the Vantage manager agent. A tiny CLI that connects
to a running `shadow-replica` and lets a manager hold a conversation with
the tool-calling agent that fronts the dashboard.

## Install

```
uv sync --all-packages --group dev
```

## Setup

```
employer-voice init --manager-token vsm_<your-token> \
                    --replica-url http://127.0.0.1:7475
```

## Usage

```
# Text turn
employer-voice ask "what's blocking Alice today?"

# Text turn with live tool-call stream
employer-voice ask "which repos saw collisions this week?" --stream

# List and reset conversations
employer-voice conversations
employer-voice reset
```

The last `conversation_id` is persisted in the config so each new `ask`
continues the previous chat. `reset` forgets it.

## Architecture

```
employer-voice ask ──▶ shadow-replica /ui/api/chat           (JSON)
employer-voice ask --stream ──▶ shadow-replica /ui/api/chat/stream (SSE)
```

Depends only on `httpx` + `typer` + `pydantic`.

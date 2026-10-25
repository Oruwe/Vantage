from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class A2AIntent(str, Enum):
    """Structured intents for Agent-to-Agent dialogue.

    Replaces the old implicit single-call RPC model with explicit, inspectable
    message purposes. Every A2AEnvelope carries one.
    """
    INTENT_DISCOVERY = "intent_discovery"           # Manager → broadcast: "who is touching X?"
    CONTEXT_NEGOTIATION = "context_negotiation"     # Follow-up: "give me more detail on Y"
    POLICY_CHALLENGE = "policy_challenge"           # Dev → Manager: "this is restricted"
    POLICY_COUNTER_OFFER = "policy_counter_offer"   # Dev → Manager: "I can give you X instead"
    SYNTHESIS_ACK = "synthesis_ack"                 # Manager → Dev: "accepted downgrade"
    PROBE_REQUEST = "probe_request"
    PROBE_RESPONSE = "probe_response"
    ARTIFACT_PUSH = "artifact_push"                 # Approved artifact crossing the wire
    REDACTION_REPORT = "redaction_report"           # Scrubber announces what it stripped
    SESSION_OPEN = "session_open"
    SESSION_CLOSE = "session_close"
    DISCOVERY_REQUEST = "discovery_request"         # Fan-out of a free-form question
    DISCOVERY_RESPONSE = "discovery_response"       # One ghost's answer within a discovery session
    INTENT_BEACON = "intent_beacon"                 # Ghost: "I am currently touching these files in this repo"
    COLLISION_ALERT = "collision_alert"             # Replica: "two ghosts overlapping"
    FRICTION_REPORT = "friction_report"             # Ghost: "I am struggling / not struggling"
    BRIEF_SYNTHESIZED = "brief_synthesized"         # Replica: "daily brief ready"
    REACTIVE_FOLLOWUP = "reactive_followup"         # Replica: "GitHub event triggered a self-probe; here's what the dev's session was doing"
    SEMANTIC_COLLISION_ALERT = "semantic_collision_alert"  # Replica: "two devs working on semantically similar things" (complement to file-overlap COLLISION_ALERT)
    # Manager-agent (M13) — surface the agent's reasoning path on the wiretap
    AGENT_TOOL_CALL = "agent_tool_call"             # Manager-agent → tool: "I'm calling X with args Y"
    AGENT_TOOL_RESULT = "agent_tool_result"         # Tool → Manager-agent: "here's the result (preview)"
    AGENT_RESPONSE = "agent_response"               # Manager-agent → Manager-UI: final assistant message


class A2AAgentRef(BaseModel):
    """A compact identity for an agent participating in a conversation."""
    kind: str                     # "ghost-daemon" | "shadow-replica" | "manager-ui" | "subscriber-agent"
    developer_id: str = ""        # "" when not developer-scoped
    subscriber_id: str = ""       # "" when not subscriber-scoped
    label: str = ""               # human-readable, e.g. "Alice's ghost"


class A2AEnvelope(BaseModel):
    """One structured message in an A2A dialogue.

    Serialized to JSON and dropped on the WiretapBus so the UI can render the
    conversation live. Every agent-to-agent interaction gets wrapped in one of
    these — probe requests, probe responses, pushes, policy challenges, etc.
    """
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    parent_turn_id: str = ""                              # "" for conversation openers
    session_id: str = ""                                  # groups turns into a dialogue
    intent: A2AIntent
    from_agent: A2AAgentRef
    to_agent: A2AAgentRef
    created_at: datetime = Field(default_factory=datetime.utcnow)
    latency_ms: int | None = None                         # fill on response envelopes
    status: str = "ok"                                    # "ok" | "denied" | "timeout" | "error"
    summary: str = ""                                     # one-line human preview
    payload: dict[str, Any] = Field(default_factory=dict)
    redaction_count: int = 0                              # how many secrets scrubber stripped


class A2ASessionMeta(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    topic: str = ""
    initiator: A2AAgentRef
    participants: list[A2AAgentRef] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None

"""
execution_tracing.py

Generic execution tracing for an AI / AIOps platform.

Goals
-----
1. Trace ANY execution, not only one agent.
2. Support agents, tools, APIs, databases, RAG, cache, workflows and direct-tool paths.
3. Preserve parent/child span hierarchy.
4. Track latency, token usage and token savings.
5. Keep domain-specific information in metadata rather than hard-coding Backup/Oracle/FinOps logic.
6. Redact secrets before persistence.
7. Produce a JSON structure directly consumable by AgentTracesPage.tsx.

This module is deliberately framework-agnostic. Claude/another coding model should adapt
the persistence layer, ORM models, API routes and project conventions to the existing codebase
instead of replacing them.
"""

from __future__ import annotations

import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET_KEYWORDS = {
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "secret",
    "private_key",
    "client_secret",
    "bearer",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


def new_span_id() -> str:
    return f"sp_{uuid.uuid4().hex[:16]}"


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower())


def is_sensitive_key(key: str) -> bool:
    k = _normalized_key(key)
    return any(secret in k for secret in SECRET_KEYWORDS)


def redact(value: Any) -> Any:
    """Recursively redact secrets before storing trace metadata."""
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if is_sensitive_key(str(key)) else redact(val)
            for key, val in value.items()
        }

    if isinstance(value, list):
        return [redact(v) for v in value]

    if isinstance(value, tuple):
        return [redact(v) for v in value]

    return value


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------

@dataclass
class TokenBreakdown:
    system_prompt: int = 0
    user_prompt: int = 0
    conversation_history: int = 0
    tool_definitions: int = 0
    retrieved_context: int = 0
    model_output: int = 0

    cache_saved: int = 0
    history_saved: int = 0
    tool_filtering_saved: int = 0
    context_filtering_saved: int = 0
    direct_tool_saved: int = 0

    @property
    def input_tokens(self) -> int:
        return (
            self.system_prompt
            + self.user_prompt
            + self.conversation_history
            + self.tool_definitions
            + self.retrieved_context
        )

    @property
    def output_tokens(self) -> int:
        return self.model_output

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_saved(self) -> int:
        return (
            self.cache_saved
            + self.history_saved
            + self.tool_filtering_saved
            + self.context_filtering_saved
            + self.direct_tool_saved
        )

    def to_dict(self) -> Dict[str, int]:
        return {
            "systemPrompt": self.system_prompt,
            "userPrompt": self.user_prompt,
            "conversationHistory": self.conversation_history,
            "toolDefinitions": self.tool_definitions,
            "retrievedContext": self.retrieved_context,
            "modelOutput": self.model_output,
            "cacheSaved": self.cache_saved,
            "historySaved": self.history_saved,
            "toolFilteringSaved": self.tool_filtering_saved,
            "contextFilteringSaved": self.context_filtering_saved,
            "directToolSaved": self.direct_tool_saved,
        }


# ---------------------------------------------------------------------------
# Span
# ---------------------------------------------------------------------------

@dataclass
class TraceSpan:
    id: str
    name: str
    span_type: str

    parent_span_id: Optional[str] = None
    service_name: Optional[str] = None
    agent_name: Optional[str] = None
    tool_name: Optional[str] = None

    status: str = "running"          # running | success | error
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: Optional[str] = None
    duration_ms: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    tokens: int = 0

    metadata: Dict[str, Any] = field(default_factory=dict)
    children: List["TraceSpan"] = field(default_factory=list)

    _perf_start: float = field(default=0.0, repr=False, compare=False)

    def start(self) -> None:
        self._perf_start = time.perf_counter()

    def finish(
        self,
        *,
        status: str = "success",
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.completed_at = utc_now_iso()
        self.duration_ms = max(
            0, round((time.perf_counter() - self._perf_start) * 1000)
        )
        self.status = status

        if input_tokens is not None:
            self.input_tokens = max(0, int(input_tokens))

        if output_tokens is not None:
            self.output_tokens = max(0, int(output_tokens))

        self.tokens = (
            max(0, int(tokens))
            if tokens is not None
            else self.input_tokens + self.output_tokens
        )

        if metadata:
            self.metadata.update(redact(metadata))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.span_type,
            "parentSpanId": self.parent_span_id,
            "serviceName": self.service_name,
            "agent": self.agent_name,
            "toolName": self.tool_name,
            "status": self.status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "durationMs": self.duration_ms,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "tokens": self.tokens,
            "metadata": redact(self.metadata),
            "children": [child.to_dict() for child in self.children],
        }


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

@dataclass
class ExecutionTrace:
    id: str
    name: str
    user_query: str

    request_id: Optional[str] = None
    root_agent: Optional[str] = None
    selected_agent: Optional[str] = None

    status: str = "running"
    execution_path: str = "agent"   # agent | direct_tool | cache | workflow
    model: Optional[str] = None

    intent: Optional[str] = None
    routing_confidence: Optional[float] = None
    cache_status: str = "bypass"    # hit | miss | bypass

    started_at: str = field(default_factory=utc_now_iso)
    completed_at: Optional[str] = None
    duration_ms: int = 0

    tool_calls: int = 0
    llm_calls: int = 0

    token_breakdown: TokenBreakdown = field(default_factory=TokenBreakdown)
    spans: List[TraceSpan] = field(default_factory=list)

    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    _perf_start: float = field(default=0.0, repr=False, compare=False)

    def start(self) -> None:
        self._perf_start = time.perf_counter()

    def finish(
        self,
        *,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> None:
        self.completed_at = utc_now_iso()
        self.duration_ms = max(
            0, round((time.perf_counter() - self._perf_start) * 1000)
        )
        self.status = status
        self.error_message = error_message

    def to_dict(self) -> Dict[str, Any]:
        actual = self.token_breakdown.total_tokens
        saved = self.token_breakdown.tokens_saved
        potential = actual + saved
        optimization_rate = round(saved / potential * 100, 2) if potential else 0.0

        return {
            "id": self.id,
            "name": self.name,
            "requestId": self.request_id,
            "userQuery": self.user_query,
            "rootAgent": self.root_agent,
            "agentName": self.selected_agent or self.root_agent or "N/A",
            "status": self.status,
            "executionPath": self.execution_path,
            "model": self.model,
            "intent": self.intent,
            "routingConfidence": self.routing_confidence,
            "cacheStatus": self.cache_status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "durationMs": self.duration_ms,
            "inputTokens": self.token_breakdown.input_tokens,
            "outputTokens": self.token_breakdown.output_tokens,
            "totalTokens": actual,
            "tokensSaved": saved,
            "potentialTokens": potential,
            "optimizationRate": optimization_rate,
            "toolCalls": self.tool_calls,
            "llmCalls": self.llm_calls,
            "tokenBreakdown": self.token_breakdown.to_dict(),
            "spans": [span.to_dict() for span in self.spans],
            "errorMessage": self.error_message,
            "metadata": redact(self.metadata),
            "recommendations": build_optimization_recommendations(self),
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class TraceManager:
    """
    One TraceManager per inbound request / workflow execution.

    IMPORTANT:
    Domain logic should NOT be placed inside TraceManager.
    Backup, Oracle, PostgreSQL, FinOps, Snow, etc. should create spans and
    attach their own safe metadata.
    """

    def __init__(
        self,
        *,
        name: str,
        user_query: str = "",
        request_id: Optional[str] = None,
        root_agent: Optional[str] = None,
        model: Optional[str] = None,
        execution_path: str = "agent",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.trace = ExecutionTrace(
            id=new_trace_id(),
            name=name,
            request_id=request_id,
            user_query=user_query,
            root_agent=root_agent,
            selected_agent=root_agent,
            model=model,
            execution_path=execution_path,
            metadata=redact(metadata or {}),
        )
        self.trace.start()
        self._stack: List[TraceSpan] = []

    @contextmanager
    def span(
        self,
        name: str,
        span_type: str,
        *,
        service_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        tool_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Iterator[TraceSpan]:
        parent = self._stack[-1] if self._stack else None

        span = TraceSpan(
            id=new_span_id(),
            name=name,
            span_type=span_type,
            parent_span_id=parent.id if parent else None,
            service_name=service_name,
            agent_name=agent_name,
            tool_name=tool_name,
            metadata=redact(metadata or {}),
        )
        span.start()

        if parent:
            parent.children.append(span)
        else:
            self.trace.spans.append(span)

        self._stack.append(span)

        try:
            yield span
            if span.status == "running":
                span.finish(status="success")
        except Exception as exc:
            span.finish(
                status="error",
                metadata={"error": str(exc)},
            )
            raise
        finally:
            self._stack.pop()

    def set_routing(
        self,
        *,
        intent: str,
        selected_agent: str,
        confidence: Optional[float] = None,
    ) -> None:
        self.trace.intent = intent
        self.trace.selected_agent = selected_agent
        self.trace.routing_confidence = confidence

    def set_cache(self, status: str, saved_tokens: int = 0) -> None:
        self.trace.cache_status = status
        self.trace.token_breakdown.cache_saved = max(0, int(saved_tokens))

    def record_prompt_tokens(
        self,
        *,
        system_prompt: int = 0,
        user_prompt: int = 0,
        conversation_history: int = 0,
        tool_definitions: int = 0,
        retrieved_context: int = 0,
    ) -> None:
        tb = self.trace.token_breakdown
        tb.system_prompt = max(0, int(system_prompt))
        tb.user_prompt = max(0, int(user_prompt))
        tb.conversation_history = max(0, int(conversation_history))
        tb.tool_definitions = max(0, int(tool_definitions))
        tb.retrieved_context = max(0, int(retrieved_context))

    def record_model_output(self, output_tokens: int) -> None:
        self.trace.token_breakdown.model_output = max(0, int(output_tokens))

    def record_savings(
        self,
        *,
        cache_saved: Optional[int] = None,
        history_saved: int = 0,
        tool_filtering_saved: int = 0,
        context_filtering_saved: int = 0,
        direct_tool_saved: int = 0,
    ) -> None:
        tb = self.trace.token_breakdown

        if cache_saved is not None:
            tb.cache_saved = max(0, int(cache_saved))

        tb.history_saved = max(0, int(history_saved))
        tb.tool_filtering_saved = max(0, int(tool_filtering_saved))
        tb.context_filtering_saved = max(0, int(context_filtering_saved))
        tb.direct_tool_saved = max(0, int(direct_tool_saved))

    def increment_tool_calls(self, count: int = 1) -> None:
        self.trace.tool_calls += max(0, int(count))

    def increment_llm_calls(self, count: int = 1) -> None:
        self.trace.llm_calls += max(0, int(count))

    def finish(
        self,
        *,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.trace.finish(status=status, error_message=error_message)
        return self.trace.to_dict()


# ---------------------------------------------------------------------------
# Optimization recommendations
# ---------------------------------------------------------------------------

def build_optimization_recommendations(trace: ExecutionTrace) -> List[str]:
    tb = trace.token_breakdown
    recommendations: List[str] = []

    if tb.tool_definitions > 400:
        recommendations.append(
            "High tool-definition usage: dynamically load only tools relevant to the selected intent/agent."
        )

    if tb.conversation_history > 500:
        recommendations.append(
            "Large conversation history: trim old turns or send a compact conversation summary."
        )

    if tb.retrieved_context > 500:
        recommendations.append(
            "Large retrieved context: improve retrieval filtering or reduce RAG top-k."
        )

    if (
        trace.execution_path == "agent"
        and trace.tool_calls == 1
        and trace.llm_calls > 0
        and trace.token_breakdown.total_tokens > 1000
        and trace.intent
    ):
        recommendations.append(
            "Possible Direct Tool Path candidate: deterministic intent + one tool call may not require an LLM."
        )

    if trace.execution_path == "direct_tool":
        recommendations.append(
            f"Direct Tool Path used successfully; {tb.direct_tool_saved:,} estimated tokens avoided."
        )

    if trace.cache_status == "hit":
        recommendations.append(
            f"Cache hit avoided approximately {tb.cache_saved:,} tokens."
        )

    if not recommendations:
        recommendations.append("No major token optimization issue detected.")

    return recommendations


# ---------------------------------------------------------------------------
# Generic integration examples
# ---------------------------------------------------------------------------

def demo_agent_trace() -> Dict[str, Any]:
    """
    Example only. Replace sleeps/token values with real values from your project.
    """
    tm = TraceManager(
        name="user_request",
        user_query="Show latest backup status for server ABC01",
        request_id="req_demo_001",
        root_agent="concierge",
        model="configured-model",
        execution_path="agent",
    )

    try:
        with tm.span("run_agent", "agent", agent_name="concierge") as root:
            with tm.span("route_request", "router") as route:
                time.sleep(0.01)
                tm.set_routing(
                    intent="backup_status",
                    selected_agent="backup",
                    confidence=0.96,
                )
                route.finish(
                    status="success",
                    tokens=65,
                    metadata={
                        "intent": "backup_status",
                        "selectedAgent": "backup",
                        "confidence": 0.96,
                    },
                )

            with tm.span("run_agent", "agent", agent_name="backup") as backup:
                with tm.span("prompt_build", "prompt") as prompt:
                    tm.record_prompt_tokens(
                        system_prompt=180,
                        user_prompt=28,
                        conversation_history=210,
                        tool_definitions=145,
                        retrieved_context=77,
                    )
                    tm.record_savings(
                        history_saved=180,
                        tool_filtering_saved=420,
                        context_filtering_saved=234,
                    )
                    prompt.finish(
                        status="success",
                        tokens=640,
                    )

                with tm.span(
                    "tool_call",
                    "tool",
                    tool_name="get_backup_status",
                ) as tool:
                    tm.increment_tool_calls()

                    # Replace with your registered tool execution.
                    result = {
                        "server": "ABC01",
                        "status": "SUCCESS",
                    }

                    tool.finish(
                        status="success",
                        tokens=0,
                        metadata={
                            "tool": "get_backup_status",
                            "input": {"server": "ABC01"},
                            "result": result,
                        },
                    )

                with tm.span("llm_response", "model") as model:
                    tm.increment_llm_calls()
                    tm.record_model_output(126)
                    model.finish(
                        status="success",
                        input_tokens=255,
                        output_tokens=126,
                        tokens=381,
                    )

                backup.finish(status="success", tokens=1021)

            with tm.span("format_response", "response") as response:
                response.finish(status="success", tokens=220)

            root.finish(status="success", tokens=1306)

        return tm.finish()

    except Exception as exc:
        return tm.finish(status="error", error_message=str(exc))


def demo_direct_tool_trace() -> Dict[str, Any]:
    """
    A simple deterministic request can bypass the LLM completely.
    """
    tm = TraceManager(
        name="user_request",
        user_query="Backup status ABC01",
        root_agent="backup",
        execution_path="direct_tool",
    )

    try:
        tm.set_routing(
            intent="backup_status",
            selected_agent="backup",
            confidence=0.99,
        )

        with tm.span(
            "direct_tool_execution",
            "tool",
            tool_name="get_backup_status",
        ) as tool:
            tm.increment_tool_calls()

            tool.finish(
                status="success",
                tokens=0,
                metadata={
                    "tool": "get_backup_status",
                    "input": {"server": "ABC01"},
                    "llmBypassed": True,
                },
            )

        tm.record_savings(direct_tool_saved=950)
        return tm.finish()

    except Exception as exc:
        return tm.finish(status="error", error_message=str(exc))

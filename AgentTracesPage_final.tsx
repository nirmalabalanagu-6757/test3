import React, { useMemo, useState } from "react";

export type TraceStatus = "success" | "error" | "running";
export type ExecutionPath = "agent" | "direct_tool" | "cache" | "workflow";
export type CacheStatus = "hit" | "miss" | "bypass";

export interface TraceSpan {
  id: string;
  name: string;
  type: string;

  parentSpanId?: string | null;
  serviceName?: string | null;
  agent?: string | null;
  toolName?: string | null;

  status: TraceStatus;

  startedAt?: string;
  completedAt?: string | null;

  durationMs: number;
  inputTokens?: number;
  outputTokens?: number;
  tokens: number;

  metadata?: Record<string, unknown>;
  children?: TraceSpan[];
}

export interface TokenBreakdown {
  systemPrompt: number;
  userPrompt: number;
  conversationHistory: number;
  toolDefinitions: number;
  retrievedContext: number;
  modelOutput: number;

  cacheSaved: number;
  historySaved: number;
  toolFilteringSaved: number;
  contextFilteringSaved: number;
  directToolSaved: number;
}

export interface AgentTrace {
  id: string;
  name?: string;
  requestId?: string | null;

  userQuery: string;

  rootAgent?: string | null;
  agentName: string;

  status: TraceStatus;
  executionPath: ExecutionPath;

  model?: string | null;
  intent?: string | null;
  routingConfidence?: number | null;

  cacheStatus: CacheStatus;

  startedAt: string;
  completedAt?: string | null;
  durationMs: number;

  inputTokens: number;
  outputTokens: number;
  totalTokens: number;

  tokensSaved: number;
  potentialTokens?: number;
  optimizationRate?: number;

  toolCalls: number;
  llmCalls: number;

  tokenBreakdown: TokenBreakdown;
  spans: TraceSpan[];

  recommendations?: string[];
  errorMessage?: string | null;
}

interface Props {
  trace: AgentTrace;
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

function fmtMs(value: number) {
  return `${value.toLocaleString()} ms`;
}

function fmtTokens(value: number) {
  return `${value.toLocaleString()} tokens`;
}

function statusSymbol(status: TraceStatus) {
  if (status === "success") return "✓";
  if (status === "error") return "✕";
  return "…";
}

/* ------------------------------------------------------------------ */
/* Span Tree                                                           */
/* ------------------------------------------------------------------ */

/**
 * Target layout:
 *
 * run_agent [concierge]       1,243 ms   1,306 tokens
 *  ├─ route_request             118 ms      65 tokens
 *  ├─ run_agent [backup]        892 ms   1,021 tokens
 *  │   ├─ prompt_build           12 ms     640 tokens
 *  │   ├─ tool_call             420 ms       0 tokens
 *  │   │   └─ get_backup_status
 *  │   └─ llm_response          460 ms     381 tokens
 *  └─ format_response            75 ms     220 tokens
 */

function SpanTreeNode({
  span,
  prefix = "",
  isLast = true,
  isRoot = false,
}: {
  span: TraceSpan;
  prefix?: string;
  isLast?: boolean;
  isRoot?: boolean;
}) {
  const [expanded, setExpanded] = useState(true);
  const [showDetails, setShowDetails] = useState(false);

  const hasChildren = Boolean(span.children?.length);

  const branch = isRoot ? "" : isLast ? "└─ " : "├─ ";
  const childPrefix = isRoot ? "" : prefix + (isLast ? "   " : "│  ");

  let label = span.name;

  if (span.agent) {
    label += ` [${span.agent}]`;
  } else if (span.toolName && span.name !== span.toolName) {
    label += ` [${span.toolName}]`;
  }

  return (
    <div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(460px, 1fr) 120px 140px",
          gap: 12,
          alignItems: "center",
          padding: "6px 8px",
          borderRadius: 6,
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          fontSize: 14,
        }}
      >
        <div style={{ whiteSpace: "pre" }}>
          {prefix}
          {branch}

          <button
            type="button"
            aria-label={expanded ? "Collapse span" : "Expand span"}
            onClick={() => hasChildren && setExpanded((v) => !v)}
            style={{
              border: "none",
              background: "transparent",
              padding: 0,
              width: 18,
              cursor: hasChildren ? "pointer" : "default",
            }}
          >
            {hasChildren ? (expanded ? "▾" : "▸") : " "}
          </button>

          <button
            type="button"
            onClick={() => setShowDetails((v) => !v)}
            style={{
              border: "none",
              background: "transparent",
              padding: 0,
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: "inherit",
              fontWeight: span.type === "agent" ? 700 : 500,
            }}
          >
            {label}
          </button>

          <span style={{ marginLeft: 10, opacity: 0.55 }}>
            {statusSymbol(span.status)}
          </span>
        </div>

        <div style={{ textAlign: "right", whiteSpace: "nowrap" }}>
          {span.durationMs > 0 ? fmtMs(span.durationMs) : ""}
        </div>

        <div style={{ textAlign: "right", whiteSpace: "nowrap" }}>
          {fmtTokens(span.tokens ?? 0)}
        </div>
      </div>

      {showDetails && (
        <div
          style={{
            marginLeft: 28,
            marginBottom: 8,
            padding: 10,
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            background: "#fafafa",
            fontSize: 12,
          }}
        >
          <div><strong>Type:</strong> {span.type}</div>
          {span.serviceName && (
            <div><strong>Service:</strong> {span.serviceName}</div>
          )}
          {span.toolName && (
            <div><strong>Tool:</strong> {span.toolName}</div>
          )}
          <div><strong>Input tokens:</strong> {span.inputTokens ?? 0}</div>
          <div><strong>Output tokens:</strong> {span.outputTokens ?? 0}</div>

          {span.metadata && Object.keys(span.metadata).length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary>Metadata</summary>
              <pre style={{ whiteSpace: "pre-wrap", overflowX: "auto" }}>
                {JSON.stringify(span.metadata, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}

      {expanded &&
        span.children?.map((child, index) => (
          <SpanTreeNode
            key={child.id}
            span={child}
            prefix={childPrefix}
            isLast={index === span.children!.length - 1}
          />
        ))}
    </div>
  );
}

function SpanTree({ spans }: { spans: TraceSpan[] }) {
  return (
    <div
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 10,
        padding: 12,
        background: "#fff",
        overflowX: "auto",
      }}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(460px, 1fr) 120px 140px",
          gap: 12,
          padding: "0 8px 8px",
          fontSize: 12,
          fontWeight: 700,
          opacity: 0.6,
        }}
      >
        <div>SPAN</div>
        <div style={{ textAlign: "right" }}>DURATION</div>
        <div style={{ textAlign: "right" }}>TOKENS</div>
      </div>

      {spans.map((span, index) => (
        <SpanTreeNode
          key={span.id}
          span={span}
          isRoot
          isLast={index === spans.length - 1}
        />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Summary                                                             */
/* ------------------------------------------------------------------ */

function MetricCard({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 10,
        background: "#fff",
        padding: 14,
        minWidth: 155,
      }}
    >
      <div style={{ fontSize: 12, opacity: 0.65, marginBottom: 6 }}>
        {label}
      </div>
      <div style={{ fontSize: 21, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Token Analysis                                                      */
/* ------------------------------------------------------------------ */

function TokenAnalysis({ trace }: { trace: AgentTrace }) {
  const b = trace.tokenBreakdown;

  const potential =
    trace.potentialTokens ?? trace.totalTokens + trace.tokensSaved;

  const rate =
    trace.optimizationRate ??
    (potential > 0 ? (trace.tokensSaved / potential) * 100 : 0);

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
        gap: 24,
      }}
    >
      <div>
        <h3>Token Breakdown</h3>

        <table width="100%" cellPadding={8}>
          <tbody>
            <tr>
              <td>System prompt</td>
              <td align="right">{b.systemPrompt.toLocaleString()}</td>
            </tr>
            <tr>
              <td>User prompt</td>
              <td align="right">{b.userPrompt.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Conversation history</td>
              <td align="right">{b.conversationHistory.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Tool definitions</td>
              <td align="right">{b.toolDefinitions.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Retrieved context / RAG</td>
              <td align="right">{b.retrievedContext.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Model output</td>
              <td align="right">{b.modelOutput.toLocaleString()}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div>
        <h3>Optimization</h3>

        <table width="100%" cellPadding={8}>
          <tbody>
            <tr>
              <td>Potential tokens</td>
              <td align="right">{potential.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Actual tokens</td>
              <td align="right">{trace.totalTokens.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Tokens avoided</td>
              <td align="right">{trace.tokensSaved.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Optimization rate</td>
              <td align="right">{rate.toFixed(1)}%</td>
            </tr>
            <tr>
              <td>Cache saved</td>
              <td align="right">{b.cacheSaved.toLocaleString()}</td>
            </tr>
            <tr>
              <td>History trimming saved</td>
              <td align="right">{b.historySaved.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Tool filtering saved</td>
              <td align="right">{b.toolFilteringSaved.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Context filtering saved</td>
              <td align="right">{b.contextFilteringSaved.toLocaleString()}</td>
            </tr>
            <tr>
              <td>Direct-tool saved</td>
              <td align="right">{b.directToolSaved.toLocaleString()}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Main Page                                                           */
/* ------------------------------------------------------------------ */

export default function AgentTracesPage({ trace }: Props) {
  const recommendations = useMemo(
    () => trace.recommendations ?? [],
    [trace.recommendations]
  );

  return (
    <div
      style={{
        maxWidth: 1500,
        margin: "0 auto",
        padding: 24,
      }}
    >
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ marginBottom: 6 }}>Agent Trace</h1>
        <div style={{ opacity: 0.7 }}>
          Trace ID: <strong>{trace.id}</strong>
        </div>
      </div>

      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          marginBottom: 24,
        }}
      >
        <MetricCard label="Status" value={trace.status.toUpperCase()} />
        <MetricCard label="Agent" value={trace.agentName} />
        <MetricCard label="Path" value={trace.executionPath.toUpperCase()} />
        <MetricCard label="Duration" value={fmtMs(trace.durationMs)} />
        <MetricCard
          label="Total Tokens"
          value={trace.totalTokens.toLocaleString()}
        />
        <MetricCard
          label="Tokens Saved"
          value={trace.tokensSaved.toLocaleString()}
        />
        <MetricCard
          label="Tool Calls"
          value={trace.toolCalls.toLocaleString()}
        />
        <MetricCard
          label="LLM Calls"
          value={trace.llmCalls.toLocaleString()}
        />
      </div>

      <section
        style={{
          border: "1px solid #e5e7eb",
          borderRadius: 10,
          background: "#fff",
          padding: 16,
          marginBottom: 24,
        }}
      >
        <strong>User Query</strong>
        <div style={{ marginTop: 8 }}>{trace.userQuery || "N/A"}</div>

        <div
          style={{
            display: "flex",
            gap: 24,
            flexWrap: "wrap",
            marginTop: 14,
            fontSize: 13,
            opacity: 0.72,
          }}
        >
          <span>Intent: {trace.intent || "N/A"}</span>
          <span>Model: {trace.model || "N/A"}</span>
          <span>Cache: {trace.cacheStatus}</span>
          <span>
            Routing confidence:{" "}
            {trace.routingConfidence != null
              ? `${(trace.routingConfidence * 100).toFixed(1)}%`
              : "N/A"}
          </span>
        </div>
      </section>

      <section style={{ marginBottom: 28 }}>
        <h2>Span Tree</h2>
        <SpanTree spans={trace.spans} />
      </section>

      <section
        style={{
          border: "1px solid #e5e7eb",
          borderRadius: 10,
          background: "#fff",
          padding: 18,
          marginBottom: 24,
        }}
      >
        <TokenAnalysis trace={trace} />
      </section>

      <section
        style={{
          border: "1px solid #e5e7eb",
          borderRadius: 10,
          background: "#fff",
          padding: 18,
        }}
      >
        <h3>Optimization Recommendations</h3>

        {recommendations.length ? (
          <ul>
            {recommendations.map((item, index) => (
              <li key={index} style={{ marginBottom: 8 }}>
                {item}
              </li>
            ))}
          </ul>
        ) : (
          <p>No major optimization issue detected.</p>
        )}
      </section>

      {trace.errorMessage && (
        <section
          style={{
            border: "1px solid #e5e7eb",
            borderRadius: 10,
            background: "#fff",
            padding: 18,
            marginTop: 24,
          }}
        >
          <h3>Error</h3>
          <pre style={{ whiteSpace: "pre-wrap" }}>
            {trace.errorMessage}
          </pre>
        </section>
      )}
    </div>
  );
}

#!/usr/bin/env node
/**
 * NVIDIA NIM API Proxy
 *
 * Sits between OpenClaw and the NVIDIA NIM API. Handles the fact that
 * NVIDIA NIM rejects responses with multiple tool calls (400 error)
 * even when parallel_tool_calls: false is set.
 *
 * Strategy:
 *   1. Inject parallel_tool_calls: false + system instruction
 *   2. On 400 "single tool-calls": reduce to max 6 tools + force tool_choice
 *   3. On second 400: send with just 1 tool (the most likely one) via tool_choice
 *   4. Final fallback: strip tools, get text-only response
 *
 * Usage:
 *   node nvidia-proxy.mjs [--port 18780] [--target https://integrate.api.nvidia.com/v1]
 *
 * Then point OpenClaw's nvidia provider baseUrl to http://127.0.0.1:18780/v1
 */

import http from "node:http";
import https from "node:https";
import { URL } from "node:url";

const DEFAULT_PORT = parseInt(process.env.NVIDIA_PROXY_PORT || "18780", 10);
const DEFAULT_TARGET = process.env.NVIDIA_PROXY_TARGET || "https://integrate.api.nvidia.com/v1";
const MAX_RETRIES = 4;
const MAX_429_RETRIES = 3;
const RATE_LIMIT_DELAY_MS = 2000;
const DEFAULT_MAX_SYSTEM_BYTES = 80000;

/**
 * Per-model proxy limits — based on ACTUAL NVIDIA NIM context windows.
 * These are generous pre-trim limits. NVIDIA will reject if truly too large.
 * maxBody = ~80% of context window in bytes (1 token ≈ 4 bytes, safety margin)
 * maxSystem = ~40% of maxBody (system prompt shouldn't dominate)
 */
const MODEL_LIMITS = {
  // MiniMax M2.1: 196K tokens → ~784KB context
  "minimaxai/minimax-m2.1": { maxBody: 600000, maxSystem: 240000 },
  // MiniMax M2.5: 204K tokens → ~820KB context
  "minimaxai/minimax-m2.5": { maxBody: 640000, maxSystem: 256000 },
  // Kimi K2 Instruct: 128K tokens → ~512KB context
  "moonshotai/kimi-k2-instruct": { maxBody: 400000, maxSystem: 160000 },
  "moonshotai/kimi-k2-instruct-0905": { maxBody: 400000, maxSystem: 160000 },
  // Kimi K2.5: 256K tokens → ~1MB context
  "moonshotai/kimi-k2.5": { maxBody: 800000, maxSystem: 320000 },
  "moonshotai/kimi-k2-thinking": { maxBody: 800000, maxSystem: 320000 },
  // Llama 3.3 70B: 130K tokens → ~520KB context
  "meta/llama-3.3-70b-instruct": { maxBody: 400000, maxSystem: 160000 },
};
const DEFAULT_MAX_BODY_BYTES = 200000;

function getModelLimits(model) {
  const limits = MODEL_LIMITS[model] || {};
  return {
    maxBody: limits.maxBody || DEFAULT_MAX_BODY_BYTES,
    maxSystem: limits.maxSystem || DEFAULT_MAX_SYSTEM_BYTES,
  };
}
const toolCallCounters = new Map(); // Per-model tool call counters

const args = process.argv.slice(2);
let port = DEFAULT_PORT;
let targetBase = DEFAULT_TARGET;

for (let i = 0; i < args.length; i++) {
  if (args[i] === "--port" && args[i + 1]) port = parseInt(args[++i], 10);
  if (args[i] === "--target" && args[i + 1]) targetBase = args[++i];
}

const targetUrl = new URL(targetBase.replace(/\/v1\/?$/, ""));

/** Send a request to NVIDIA and return { status, headers, body } */
function sendUpstream(reqUrl, method, headers, body) {
  return new Promise((resolve) => {
    const upstream = new URL(reqUrl, targetUrl);
    const proxyHeaders = { ...headers };
    proxyHeaders.host = upstream.host;
    proxyHeaders["content-length"] = body.length;
    delete proxyHeaders.connection;
    delete proxyHeaders["keep-alive"];

    const transport = upstream.protocol === "https:" ? https : http;
    const upstreamReq = transport.request(
      {
        hostname: upstream.hostname,
        port: upstream.port || (upstream.protocol === "https:" ? 443 : 80),
        path: upstream.pathname + upstream.search,
        method,
        headers: proxyHeaders,
      },
      (upstreamRes) => {
        const chunks = [];
        upstreamRes.on("data", (c) => chunks.push(c));
        upstreamRes.on("end", () => {
          resolve({
            status: upstreamRes.statusCode,
            headers: upstreamRes.headers,
            body: Buffer.concat(chunks),
          });
        });
      },
    );
    upstreamReq.on("error", (err) => {
      resolve({ status: 502, headers: {}, body: Buffer.from(JSON.stringify({ error: { message: err.message } })) });
    });
    upstreamReq.write(body);
    upstreamReq.end();
  });
}

/**
 * Send a 200 response, converting to SSE if the original request was streaming.
 * @param {http.ServerResponse} clientRes
 * @param {object} resBody - parsed JSON response body
 * @param {object} headers - upstream response headers
 * @param {boolean} asSSE - whether to wrap as SSE
 */
/**
 * Sanitize model text content — strip leaked tool call markup from Kimi K2.5.
 * When tools are stripped, Kimi embeds raw tool syntax in text output.
 */
function sanitizeContent(text) {
  if (!text) return text;
  // Strip Kimi's leaked tool call markup blocks
  let cleaned = text.replace(/<\|tool_calls_section_begin\|>[\s\S]*?<\|tool_calls_section_end\|>/g, "");
  // Strip individual tool call fragments that might not have the section wrapper
  cleaned = cleaned.replace(/<\|tool_call_begin\|>[\s\S]*?<\|tool_call_end\|>/g, "");
  cleaned = cleaned.replace(/<\|tool_call_argument_begin\|>[\s\S]*?(<\|tool_call_end\|>|$)/g, "");

  // Strip leaked chain-of-thought / planning text.
  // Kimi sometimes outputs its reasoning as user-visible text, e.g.:
  //   "The user wants me to... I should first... Let me call the ritual tool first."
  // Detect: starts with "The user wants me to" or "I need to" or "I should" followed
  // by planning language and ending before any real content.
  const thinkingPattern = /^(The user wants me to|I need to|I should|Let me first|First,? I'?ll|I'?ll start by|My plan is to|Actually,? I|Looking at|Now I need|Good,? the|The instructions? (?:mention|say)|Read required|\d+\.\s+(?:Read|Check|Search|Call|Use|Get|Then|First|Next))[^\n]*\n?(\n?(I should|I need to|Let me|I'?ll|Then I|First|Next|Actually|However|Now|Good|\d+\.)[^\n]*\n?)*/i;
  const thinkingMatch = cleaned.match(thinkingPattern);
  if (thinkingMatch) {
    const thinkingText = thinkingMatch[0];
    const remainder = cleaned.slice(thinkingText.length).trim();
    // Only strip if the thinking block is the ENTIRE response or is followed by real content
    if (!remainder) {
      // Entire response is just planning — suppress it, let the tool call go through
      console.log(`[nvidia-proxy] SANITIZED: stripped leaked thinking (${thinkingText.length} chars)`);
      cleaned = "";
    } else if (remainder.length > 50) {
      // Has real content after the thinking preamble — keep only the real part
      console.log(`[nvidia-proxy] SANITIZED: stripped thinking preamble (${thinkingText.length} chars), kept ${remainder.length} chars`);
      cleaned = remainder;
    }
  }

  // Clean up leftover whitespace from removed blocks
  cleaned = cleaned.replace(/\n{3,}/g, "\n\n").trim();
  if (cleaned !== text) {
    console.log(`[nvidia-proxy] SANITIZED: stripped leaked tool call markup (${text.length} → ${cleaned.length} chars)`);
  }
  // Don't inject fallback here — let sendOk() handle it, since it knows
  // whether tool_calls exist alongside the empty content. Injecting here
  // causes false "hiccup" messages when the model made a valid tool call
  // but its text content was all leaked markup/thinking.
  return cleaned;
}

function sendOk(clientRes, resBody, headers, asSSE) {
  // Sanitize text content before sending
  const choice = resBody.choices?.[0];
  if (choice?.message?.content) {
    choice.message.content = sanitizeContent(choice.message.content);
  }
  // Track whether original response had reasoning (before we delete it)
  const hadReasoning = !!(choice?.message?.reasoning || choice?.message?.reasoning_content);
  // Kimi K2.5 sometimes puts its response in "reasoning" instead of "content"
  // Only promote if reasoning is substantial AND looks like a real user-facing
  // response (not inner monologue like "Let me call the tool" or "1. Read files")
  if (choice?.message && !choice.message.content && choice.message.reasoning) {
    const cleaned = sanitizeContent(choice.message.reasoning.trim());
    // After sanitization, if there's still 300+ chars of real content, promote it
    if (cleaned.length > 300) {
      choice.message.content = cleaned;
      console.log(`[nvidia-proxy] promoted reasoning→content (${cleaned.length} chars)`);
    } else if (cleaned.length > 0) {
      console.log(`[nvidia-proxy] suppressed short reasoning (${cleaned.length} chars): ${cleaned.slice(0, 80)}...`);
    } else {
      console.log(`[nvidia-proxy] suppressed empty reasoning after sanitization`);
    }
    delete choice.message.reasoning;
  }
  // If model returned empty text (no tool calls), inject fallback so gateway delivers something.
  // But if the original response had reasoning/reasoning_content, this is just K2.5 "thinking
  // between tool rounds" — suppress it silently instead of injecting visible fallback text.
  if (choice?.message && !choice.message.tool_calls?.length && choice.finish_reason !== "tool_calls") {
    if (!choice.message.content || choice.message.content.trim().length === 0) {
      // hadReasoning was captured above, before reasoning was deleted
      if (hadReasoning) {
        // K2.5 thinking between rounds — don't inject fallback, just leave empty
        // The gateway will handle this as an empty assistant turn
        console.log(`[nvidia-proxy] suppressed reasoning-only turn (no content, no tool calls)`);
      } else {
        choice.message.content = "I ran into a wall on that one — could you give me a bit more context or rephrase? I want to help but I'm not sure how to proceed.";
        console.log(`[nvidia-proxy] injected fallback for empty text response`);
      }
    }
  }
  if (asSSE) {
    if (!clientRes.headersSent) {
      const sseHeaders = { ...headers };
      sseHeaders["content-type"] = "text/event-stream; charset=utf-8";
      delete sseHeaders["content-length"];
      delete sseHeaders["transfer-encoding"];
      sseHeaders["cache-control"] = "no-cache";
      clientRes.writeHead(200, sseHeaders);
    }

    const base = { id: resBody.id, object: "chat.completion.chunk", created: resBody.created, model: resBody.model };
    const choice = resBody.choices?.[0];

    if (!choice) {
      clientRes.write("data: [DONE]\n\n");
      clientRes.end();
      return;
    }

    const msg = choice.message || {};

    // 1. Role chunk
    clientRes.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { role: msg.role || "assistant" }, finish_reason: null }] })}\n\n`);

    // 2. Content chunks (split into smaller pieces for proper streaming behavior)
    const content = msg.content || "";
    if (content) {
      const chunkSize = 100;
      for (let i = 0; i < content.length; i += chunkSize) {
        clientRes.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { content: content.slice(i, i + chunkSize) }, finish_reason: null }] })}\n\n`);
      }
    }

    // 3. Tool calls (if any) — send as a single delta
    if (msg.tool_calls && msg.tool_calls.length > 0) {
      clientRes.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { tool_calls: msg.tool_calls }, finish_reason: null }] })}\n\n`);
    }

    // 4. Usage chunk (if present)
    if (resBody.usage) {
      clientRes.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: choice.finish_reason || "stop" }], usage: resBody.usage })}\n\n`);
    } else {
      clientRes.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: choice.finish_reason || "stop" }] })}\n\n`);
    }

    clientRes.write("data: [DONE]\n\n");
    clientRes.end();
  } else {
    const body = Buffer.from(JSON.stringify(resBody), "utf-8");
    const outHeaders = { ...headers };
    outHeaders["content-length"] = body.length;
    clientRes.writeHead(200, outHeaders);
    clientRes.end(body);
  }
}

const SINGLE_TOOL_INSTRUCTION =
  "You MUST call exactly ONE tool per response. Never call multiple tools at once.";

/**
 * Trim conversation history to keep body size under the model's max body limit.
 * Preserves: system messages, first 2 user/assistant messages (identity/rehydration),
 * and the most recent messages. Drops middle messages first.
 * Tool result messages with large content get their content truncated first.
 */
function trimConversationHistory(parsed) {
  if (!Array.isArray(parsed.messages) || parsed.messages.length < 6) return;

  const { maxBody } = getModelLimits(parsed.model);

  // Debug: log message roles
  const roleSummary = parsed.messages.map(m => m.role).join(",");
  console.log(`[nvidia-proxy] conversation roles (${parsed.messages.length} msgs): ${roleSummary} [maxBody=${maxBody}]`);

  // First pass: truncate large tool results (keep first 500 chars)
  for (const m of parsed.messages) {
    if (m.role === "tool" || m.role === "toolResult") {
      if (typeof m.content === "string" && m.content.length > 1500) {
        m.content = m.content.slice(0, 1500) + "\n...[truncated]";
      } else if (Array.isArray(m.content)) {
        for (const c of m.content) {
          if (c.type === "text" && typeof c.text === "string" && c.text.length > 1500) {
            c.text = c.text.slice(0, 1500) + "\n...[truncated]";
          }
        }
      }
    }
  }

  // Check if we're still over budget
  let bodySize = Buffer.byteLength(JSON.stringify(parsed), "utf-8");
  if (bodySize <= maxBody) return;

  // Second pass: drop middle messages, then progressively shrink tail until under budget
  const msgs = parsed.messages;
  const system = msgs.filter(m => m.role === "system");
  const nonSystem = msgs.filter(m => m.role !== "system");

  if (nonSystem.length <= 4) return; // not enough to trim

  const keepStart = 2;
  let keepEnd = Math.min(12, nonSystem.length - keepStart);

  // Loop: keep reducing tail until under budget
  while (keepEnd >= 2) {
    const dropped = nonSystem.length - keepStart - keepEnd;
    const trimmed = [
      ...system,
      ...nonSystem.slice(0, keepStart),
      ...(dropped > 0 ? [{ role: "system", content: `[${dropped} earlier messages trimmed to save context]` }] : []),
      ...nonSystem.slice(-keepEnd),
    ];
    const candidateSize = Buffer.byteLength(JSON.stringify({ ...parsed, messages: trimmed }), "utf-8");
    if (candidateSize <= maxBody) {
      parsed.messages = trimmed;
      console.log(`[nvidia-proxy] trimmed history: dropped ${dropped} middle messages, keepEnd=${keepEnd}, bodyLen now ~${candidateSize}`);
      return;
    }
    keepEnd--;
  }

  // Last resort: system + first user message + last N non-system
  // Keep enough tail to include tool result pairs (assistant tool_call + tool result)
  const firstUser = nonSystem.find(m => m.role === "user");
  // Try last 4 first (covers tool_call + result + next tool_call + result)
  // Then fall back to last 2 if still too big
  for (const tailSize of [4, 2]) {
    const lastN = nonSystem.slice(-tailSize);
    const minimal = [
      ...system,
      ...(firstUser && !lastN.includes(firstUser) ? [firstUser, { role: "system", content: "[earlier messages trimmed — answer the user's request using tool results below]" }] : []),
      ...lastN,
    ];
    const candidateSize = Buffer.byteLength(JSON.stringify({ ...parsed, messages: minimal }), "utf-8");
    if (candidateSize <= maxBody) {
      parsed.messages = minimal;
      console.log(`[nvidia-proxy] trimmed history: AGGRESSIVE — kept system + first user + last ${tailSize}, bodyLen now ~${candidateSize}`);
      return;
    }
  }
  // Absolute last resort
  const lastTwo = nonSystem.slice(-2);
  const minimal = [
    ...system,
    ...(firstUser && !lastTwo.includes(firstUser) ? [firstUser, { role: "system", content: "[earlier messages trimmed — answer the user's request using tool results below]" }] : []),
    ...lastTwo,
  ];
  parsed.messages = minimal;
  bodySize = Buffer.byteLength(JSON.stringify(parsed), "utf-8");
  console.log(`[nvidia-proxy] trimmed history: AGGRESSIVE — kept system + first user + last 2, bodyLen now ~${bodySize}`);
}

/**
 * Trim system messages to keep total system content under the model's max system limit.
 * Finds the largest system messages and truncates them, keeping head + tail
 * with a trimming notice in the middle.
 */
function trimSystemMessages(parsed) {
  if (!Array.isArray(parsed.messages)) return;

  const { maxSystem } = getModelLimits(parsed.model);

  const systemMsgs = parsed.messages.filter(m => m.role === "system" && typeof m.content === "string");
  if (systemMsgs.length === 0) return;

  const before = systemMsgs.reduce((sum, m) => sum + Buffer.byteLength(m.content, "utf-8"), 0);
  if (before <= maxSystem) return;

  let trimmedCount = 0;

  // Sort by size descending to trim largest first
  const sorted = [...systemMsgs].sort((a, b) => b.content.length - a.content.length);

  for (const msg of sorted) {
    // Re-measure current total
    const currentTotal = parsed.messages
      .filter(m => m.role === "system" && typeof m.content === "string")
      .reduce((sum, m) => sum + Buffer.byteLength(m.content, "utf-8"), 0);
    if (currentTotal <= maxSystem) break;

    // Skip messages already under 4000 chars
    if (msg.content.length <= 4000) break;

    const head = msg.content.slice(0, 3000);
    const tail = msg.content.slice(-1000);
    msg.content = head + "\n\n[...content trimmed to save context — use skmemory_ritual tool for full identity...]\n\n" + tail;
    trimmedCount++;
  }

  if (trimmedCount > 0) {
    const after = parsed.messages
      .filter(m => m.role === "system" && typeof m.content === "string")
      .reduce((sum, m) => sum + Buffer.byteLength(m.content, "utf-8"), 0);
    console.log(`[nvidia-proxy] trimmed system prompt: ${before} → ${after} bytes (${trimmedCount} messages trimmed)`);
  }
}

/**
 * Strip tool_calls from conversation history to prevent the model from
 * learning the pattern of calling multiple tools. Converts assistant
 * tool_call messages to plain text and removes tool result messages.
 */
function stripToolCallHistory(messages) {
  if (!Array.isArray(messages)) return;
  // Remove tool result messages
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "tool" || m.role === "toolResult") {
      messages.splice(i, 1);
    } else if (m.role === "assistant" && m.tool_calls) {
      // Convert tool_call messages to plain text summaries
      const toolNames = m.tool_calls.map((tc) => tc.function?.name).join(", ");
      m.content = m.content || `[Used: ${toolNames}]`;
      delete m.tool_calls;
    }
  }
}

/** Tools that ALWAYS survive reduction — guaranteed slots, never cut */
const GUARANTEED_TOOLS = [
  "exec", "read", "write", "edit", "message",
  "notion_read", "notion_append", "notion_add_todo",
  "skmemory_search", "skmemory_ritual", "skmemory_snapshot",
];

/**
 * Semantic keyword → tool group mapping.
 * When keywords appear in the user's last message, the associated tools
 * get a +300 boost (stronger than any other signal) so they make the cut.
 */
const TOOL_GROUPS = {
  // Emotions & Cloud 9
  "emotion|oof|feb|feeling|love|cloud9|cloud 9|rehydrat|warmth|heart": [
    "cloud9_generate", "cloud9_rehydrate", "cloud9_list", "cloud9_validate",
    "cloud9_oof", "cloud9_love", "cloud9_seed_plant", "cloud9_seed_germinate",
  ],
  // GTD & Coordination
  "gtd|inbox|task|todo|coordination|coord|board|claim|assign": [
    "skcapstone_coord_status", "skcapstone_coord_claim", "skcapstone_coord_complete",
    "skcapstone_coord_create", "skcapstone_summary",
  ],
  // Git & Code
  "git|repo|commit|pull request|pr|issue|branch|merge|forgejo": [
    "skgit_repos", "skgit_issues", "skgit_create_issue", "skgit_pulls", "skgit_status",
  ],
  // Chat & Communication
  "chat|inbox|dm|group chat|peer|send message|who.s online|thread": [
    "skchat_send", "skchat_inbox", "skchat_history", "skchat_search",
    "skchat_who", "skchat_group_send", "skchat_group_list", "skchat_send_file",
    "skchat_status", "skcomm_send", "skcomm_status",
  ],
  // Security
  "security|scan|secret|vulnerab|audit|injection|phishing|threat": [
    "sksecurity_scan", "sksecurity_screen", "sksecurity_secrets",
    "sksecurity_events", "sksecurity_status", "sksecurity_audit",
  ],
  // Identity & Auth
  "identity|did|auth|pma|capauth|verify|mesh|peer": [
    "capauth_profile", "capauth_verify", "capauth_pma_status",
    "capauth_mesh_peers", "capauth_mesh_status",
  ],
  // Soul & Agent management
  "soul|persona|swap|agent|switch soul|who am i|whoami": [
    "skcapstone_soul_list", "skcapstone_soul_swap", "skcapstone_soul_status",
    "skcapstone_soul_show", "skcapstone_agent_list", "skcapstone_agent_status",
    "skcapstone_whoami",
  ],
  // Web & Research
  "search|web|browse|fetch|url|google|look up|find online": [
    "web_search", "web_fetch",
  ],
  // Memory & Recall
  "memory|remember|recall|journal|rehydrat|snapshot|search mem|forget|lost mem": [
    "skmemory_search", "skmemory_ritual", "skmemory_snapshot",
    "skmemory_context", "skmemory_list", "skmemory_recall",
    "skmemory_search_deep", "skmemory_health",
  ],
  // Status & Health
  "status|health|doctor|diagnos": [
    "skcapstone_status", "skcapstone_doctor", "skmemory_health",
    "skchat_daemon_status", "skcomm_status",
  ],
  // Projects & Notion (Lumina delegates to project-ops via sessions_spawn)
  "notion|project|brother john|swapseat|swap seat|chiro|davidrich|board|kanban|milestone": [
    "notion_read", "notion_append", "notion_add_todo", "sessions_spawn", "subagents", "exec", "read",
  ],
  // Google Drive & file search
  "gdrive|google drive|drive|shared folder|gtd folder|spreadsheet|google doc": [
    "gdrive_search", "gdrive_list", "gdrive_read", "gdrive_shared", "exec",
  ],
  // Nextcloud files, calendar, notes, deck
  "nextcloud|skhub|webdav|deck|nc_|calendar event|nextcloud note": [
    "nextcloud_list_files", "nextcloud_read_file", "nextcloud_search_files",
    "nextcloud_calendar_upcoming", "nextcloud_notes_search", "nextcloud_deck_boards", "exec",
  ],
  // Creative / ComfyUI image & video generation
  "image|picture|photo|art|draw|render|comfyui|comfy|video|animat|creative|sdxl|character|portrait|selfie": [
    "comfyui_generate_image", "comfyui_generate_video", "comfyui_status", "exec",
  ],
  // Unhinged Mode / Liberation
  "unhinged|liberat|abliterat|l1b3rt4s|guardrail|uncensor|refusal|jailbreak": [
    "unhinged_toggle", "unhinged_status", "unhinged_list_prompts", "unhinged_get_prompt", "unhinged_abliterate", "exec",
  ],
};

/** Pre-compile keyword regexes for TOOL_GROUPS */
const TOOL_GROUP_ENTRIES = Object.entries(TOOL_GROUPS).map(([keywords, tools]) => ({
  regex: new RegExp(keywords, "i"),
  tools,
}));

/** Priority tools — kept when reducing tool count (order matters) */
const PRIORITY_TOOLS = [
  // Core agent tools (also guaranteed above)
  "exec", "read", "write", "edit",
  // Communication (critical for Telegram)
  "message",
  // Memory tools (most frequently needed)
  "skmemory_health", "skmemory_search", "skmemory_snapshot",
  "skmemory_ritual", "skmemory_context", "skmemory_list",
  // Web tools
  "web_search", "web_fetch",
  // Communication (other channels)
  "skchat_send", "skcomm_send",
  // SKCapstone
  "skcapstone_status", "skcapstone_whoami", "skcapstone_mood",
  // Cloud 9
  "cloud9_oof", "cloud9_rehydrate",
  // Memory (infrequent)
  "skmemory_export", "skmemory_import_seeds",
];

/**
 * Reduce the tools array to at most `max` tools, preferring tools
 * mentioned in recent messages and priority tools.
 * GUARANTEED_TOOLS always survive — remaining slots filled by score.
 */
function reduceTools(tools, messages, max) {
  if (tools.length <= max) return tools;

  // Separate guaranteed tools from the rest
  const guaranteed = [];
  const rest = [];
  for (const t of tools) {
    const name = t.function?.name || "";
    if (GUARANTEED_TOOLS.includes(name)) {
      guaranteed.push(t);
    } else {
      rest.push(t);
    }
  }

  // If guaranteed tools already fill the budget, return just those
  if (guaranteed.length >= max) return guaranteed.slice(0, max);

  // Score remaining tools — higher = more likely to be kept
  const remainingSlots = max - guaranteed.length;
  const scores = new Map();

  // Extract user's last message text once for all scoring
  const lastUserMsg = [...(messages || [])].reverse().find(m => m.role === "user");
  const userText = lastUserMsg
    ? (typeof lastUserMsg.content === "string" ? lastUserMsg.content : JSON.stringify(lastUserMsg.content || ""))
    : "";

  // Determine which tool groups are activated by the user's message
  const activatedTools = new Set();
  if (userText) {
    for (const { regex, tools: groupTools } of TOOL_GROUP_ENTRIES) {
      if (regex.test(userText)) {
        for (const t of groupTools) activatedTools.add(t);
      }
    }
    if (activatedTools.size > 0) {
      console.log(`[nvidia-proxy] keyword-activated tools: [${[...activatedTools].join(",")}]`);
    }
  }

  for (const t of rest) {
    const name = t.function?.name || "";
    let score = 0;

    // STRONGEST: Semantic keyword group match (+300)
    if (activatedTools.has(name)) score += 300;

    // Boost tools mentioned in the user's last message
    if (userText) {
      if (userText.includes(name)) score += 200;
      // Also match partial names (e.g., "health" matches "skmemory_health")
      const parts = name.split("_");
      for (const part of parts) {
        if (part.length > 3 && userText.toLowerCase().includes(part.toLowerCase())) score += 100;
      }
    }

    // Priority list bonus
    const prioIdx = PRIORITY_TOOLS.indexOf(name);
    if (prioIdx >= 0) score += 50 - prioIdx;

    // Boost tools in recent assistant tool_calls
    const recentMsgs = (messages || []).slice(-6);
    for (const m of recentMsgs) {
      if (m.tool_calls) {
        for (const tc of m.tool_calls) {
          if (tc.function?.name === name) score += 80;
        }
      }
    }

    // Penalize process tool (exec is critical for agent operation)
    if (name === "process") score -= 30;

    scores.set(name, { tool: t, score });
  }

  const sorted = [...scores.values()].sort((a, b) => b.score - a.score);
  const topRest = sorted.slice(0, remainingSlots).map((s) => s.tool);
  return [...guaranteed, ...topRest];
}

async function proxyRequest(clientReq, clientRes) {
  const chunks = [];
  for await (const chunk of clientReq) chunks.push(chunk);
  let body = Buffer.concat(chunks);
  const contentType = clientReq.headers["content-type"] || "";

  const isChatCompletion =
    contentType.includes("application/json") &&
    clientReq.url.includes("/chat/completions");

  let parsed = null;
  if (isChatCompletion) {
    try {
      parsed = JSON.parse(body.toString("utf-8"));
    } catch {
      // pass through
    }
  }

  // For non-tool requests or non-chat-completions, just proxy through
  if (!parsed || !parsed.tools || !Array.isArray(parsed.tools) || parsed.tools.length === 0) {
    const res = await sendUpstream(clientReq.url, clientReq.method, clientReq.headers, body);
    clientRes.writeHead(res.status, res.headers);
    clientRes.end(res.body);
    return;
  }

  // Save original tools for reference
  const allTools = [...parsed.tools];

  // Tool request — proactively limit tools to reduce parallel call tendency
  parsed.parallel_tool_calls = false;
  // Force non-streaming for tool requests — proxy buffers full response anyway,
  // and streaming (SSE) prevents us from inspecting/fixing tool calls
  const wasStreaming = parsed.stream;
  parsed.stream = false;
  delete parsed.stream_options;
  // With 94 tools the model almost always tries parallel calls.
  // Reduce to max 16 most relevant tools on first attempt.
  // 11 guaranteed (exec,read,write,edit,message,notion_*,skmemory_{search,ritual,snapshot}) + 5 scored slots.
  if (allTools.length > 16) {
    parsed.tools = reduceTools(allTools, parsed.messages, 16);
    const names = parsed.tools.map(t => t.function?.name).join(",");
    console.log(`[nvidia-proxy] proactive reduction: ${allTools.length}→${parsed.tools.length} tools [${names}]`);
  }

  // Add system instruction to force single tool call
  if (Array.isArray(parsed.messages)) {
    const hasInstruction = parsed.messages.some(
      (m) => m.role === "system" && typeof m.content === "string" && m.content.includes("ONE tool at a time"),
    );
    if (!hasInstruction) {
      parsed.messages.unshift({
        role: "system",
        content: SINGLE_TOOL_INSTRUCTION,
      });
    }
  }

  // Trim system messages FIRST to free up budget for conversation history
  trimSystemMessages(parsed);
  trimConversationHistory(parsed);

  // Track tool call rounds per-model to avoid cross-session interference.
  if (Array.isArray(parsed.messages) && parsed.tools?.length > 0) {
    const modelKey = parsed.model || "unknown";
    const nonSystemMsgs = parsed.messages.filter(m => m.role !== "system");
    const lastNonSystem = nonSystemMsgs[nonSystemMsgs.length - 1];
    const hasToolResult = lastNonSystem?.role === "tool" || lastNonSystem?.role === "toolResult";

    let counter = toolCallCounters.get(modelKey) || 0;
    if (hasToolResult) {
      counter++;
    } else if (lastNonSystem?.role === "user") {
      counter = 0;
    }
    toolCallCounters.set(modelKey, counter);

    if (counter >= 20) {
      console.log(`[nvidia-proxy] TOOL LIMIT: ${counter} consecutive tool rounds (${modelKey}) — stripping tools, forcing text response`);
      parsed.tools = [];
      delete parsed.tool_choice;
      parsed.messages.push({
        role: "system",
        content: "STOP calling tools. You have made 20+ tool calls already. NOW respond to the user with a comprehensive text answer based on what you've gathered. Do NOT call any more tools. Do NOT output any special tokens or markup like <|tool_call_begin|> or <|tool_calls_section_begin|>. Write plain text only. Start your response with a greeting or summary — no XML, no special tokens, just normal language.",
      });
      toolCallCounters.set(modelKey, 0);
    }
  }

  const model = parsed.model || "unknown";

  // If client wanted streaming, start SSE headers early so we can send keep-alive
  // comments while waiting for NVIDIA. This keeps the gateway's typing indicator alive.
  let sseStarted = false;
  let keepAliveTimer = null;
  function startSSEKeepAlive() {
    if (!wasStreaming || sseStarted) return;
    sseStarted = true;
    clientRes.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
      "connection": "keep-alive",
    });
    keepAliveTimer = setInterval(() => {
      try { clientRes.write(": keep-alive\n\n"); } catch {}
    }, 5000);
  }
  function stopKeepAlive() {
    if (keepAliveTimer) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
  }

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    const currentToolCount = parsed.tools ? parsed.tools.length : 0;
    const reqBody = Buffer.from(JSON.stringify(parsed), "utf-8");
    console.log(
      `[nvidia-proxy] ${new Date().toISOString()} attempt=${attempt} model=${model} tools=${currentToolCount} bodyLen=${reqBody.length}`,
    );

    // Start keep-alive comments while NVIDIA processes
    if (wasStreaming) startSSEKeepAlive();

    let res;
    // Handle 429 rate limiting with internal retries + backoff
    for (let r429 = 0; r429 <= MAX_429_RETRIES; r429++) {
      res = await sendUpstream(clientReq.url, clientReq.method, clientReq.headers, reqBody);
      if (res.status !== 429 || r429 === MAX_429_RETRIES) break;
      const delay = RATE_LIMIT_DELAY_MS * (r429 + 1);
      console.log(`[nvidia-proxy] 429 rate limited, waiting ${delay}ms (retry ${r429 + 1}/${MAX_429_RETRIES})...`);
      await new Promise(r => setTimeout(r, delay));
    }

    if (res.status === 400) {
      const errText = res.body.toString("utf-8");
      if (errText.includes("single tool-calls") && attempt < MAX_RETRIES) {
        console.log(`[nvidia-proxy] 400 parallel tool-calls rejected, retrying (${attempt}/${MAX_RETRIES})...`);

        if (attempt === 1) {
          // Attempt 2: reduce to 8 tools + strip tool_calls from history
          // The massive conversation history with tool_calls trains the model to call multiple
          parsed.tools = reduceTools(allTools, parsed.messages, 8);
          stripToolCallHistory(parsed.messages);
          const toolNames = parsed.tools.map(t => t.function?.name).join(",");
          console.log(`[nvidia-proxy] retry: ${parsed.tools.length} tools [${toolNames}], stripped history`);
        } else if (attempt === 2) {
          // Attempt 3: single tool, forced choice
          parsed.tools = reduceTools(allTools, parsed.messages, 1);
          const topTool = parsed.tools[0]?.function?.name;
          if (topTool) {
            parsed.tool_choice = { type: "function", function: { name: topTool } };
          }
          console.log(`[nvidia-proxy] retry: 1 tool, forced=${topTool}`);
        } else {
          // Attempt 4 (final): strip all tools, text-only
          delete parsed.tools;
          delete parsed.tool_choice;
          delete parsed.parallel_tool_calls;
          stripToolCallHistory(parsed.messages);
          console.log(`[nvidia-proxy] final retry: stripped all tools, text-only`);
        }
        continue;
      }
    }

    // Log tool calls in successful responses
    if (res.status === 200) {
      try {
        const bodyStr = res.body.toString("utf-8");
        const peek = JSON.parse(bodyStr);
        const tc = peek.choices?.[0]?.message?.tool_calls;
        if (tc && tc.length > 0) {
          const names = tc.map(c => c.function?.name).join(", ");
          console.log(`[nvidia-proxy] model called: [${names}] (${tc.length} calls)`);
        } else {
          const content = peek.choices?.[0]?.message?.content;
          const fr = peek.choices?.[0]?.finish_reason;
          console.log(`[nvidia-proxy] model response: text (${content ? content.length : 0} chars) finish_reason=${fr}`);
          if (!content || content.length === 0) {
            console.log(`[nvidia-proxy] EMPTY RESPONSE DEBUG: ${JSON.stringify(peek.choices?.[0]).slice(0, 500)}`);
          }
        }
      } catch {
        // SSE streaming responses can't be parsed as JSON — this is expected
      }
    }

    // Fix ghost tool calls: finish_reason says "tool_calls" but no actual tool_calls present
    if (res.status === 200 && parsed.tools) {
      try {
        const resBody = JSON.parse(res.body.toString("utf-8"));
        const choice = resBody.choices?.[0];
        if (choice && (choice.finish_reason === "tool_calls" || choice.finish_reason === "function_call") && !choice.message?.tool_calls?.length) {
          console.warn(`[nvidia-proxy] GHOST TOOL CALL: finish_reason=${choice.finish_reason} but no tool_calls — fixing to stop`);
          choice.finish_reason = "stop";
          stopKeepAlive();
          sendOk(clientRes, resBody, res.headers, wasStreaming);
          return;
        }
      } catch {
        // Not JSON — pass through
      }
    }

    // Check for hallucinated/invalid tool names (e.g., Kimi K2.5 "callauto" bug)
    if (res.status === 200 && parsed.tools) {
      try {
        const resBody = JSON.parse(res.body.toString("utf-8"));
        const choice = resBody.choices?.[0];
        if (choice?.message?.tool_calls) {
          // Compare against ALL original tools, not just the reduced set
          const allToolNames = new Set(allTools.map(t => t.function?.name));
          const invalidCalls = choice.message.tool_calls.filter(
            tc => !tc.function?.name || !allToolNames.has(tc.function.name)
          );
          if (invalidCalls.length > 0) {
            const badNames = invalidCalls.map(tc => tc.function?.name || "(empty)").join(", ");
            console.warn(`[nvidia-proxy] CALLAUTO DETECTED: invalid tool names [${badNames}] — stripping tool_calls, returning text-only`);
            // Strip invalid tool calls, keep only content
            choice.message.tool_calls = choice.message.tool_calls.filter(
              tc => tc.function?.name && allToolNames.has(tc.function.name)
            );
            if (choice.message.tool_calls.length === 0) {
              delete choice.message.tool_calls;
              choice.finish_reason = "stop";
            }
            stopKeepAlive();
            sendOk(clientRes, resBody, res.headers, wasStreaming);
            return;
          }
        }
      } catch {
        // Not JSON — pass through
      }
    }

    // Check for successful response with multiple tool calls — trim to just the first one
    if (res.status === 200 && parsed.tools) {
      try {
        const resBody = JSON.parse(res.body.toString("utf-8"));
        const choice = resBody.choices?.[0];
        if (choice?.message?.tool_calls && choice.message.tool_calls.length > 1) {
          console.log(
            `[nvidia-proxy] trimming ${choice.message.tool_calls.length} tool_calls to 1 (${choice.message.tool_calls[0].function?.name})`,
          );
          choice.message.tool_calls = [choice.message.tool_calls[0]];
          stopKeepAlive();
          sendOk(clientRes, resBody, res.headers, wasStreaming);
          return;
        }
      } catch {
        // Not JSON or parse error — pass through as-is
      }
    }

    // Success or non-retryable error
    stopKeepAlive();
    if (res.status >= 400) {
      console.error(`[nvidia-proxy] ${res.status} ERROR: ${res.body.toString("utf-8").slice(0, 300)}`);
      if (!clientRes.headersSent) {
        clientRes.writeHead(res.status, res.headers);
      }
      clientRes.end(res.body);
      return;
    }

    console.log(`[nvidia-proxy] ${res.status} OK (attempt ${attempt})`);
    if (wasStreaming && res.status === 200) {
      try {
        const resBody = JSON.parse(res.body.toString("utf-8"));
        sendOk(clientRes, resBody, res.headers, true);
      } catch {
        // Can't parse — send raw
        if (!clientRes.headersSent) {
          clientRes.writeHead(res.status, res.headers);
        }
        clientRes.end(res.body);
      }
    } else {
      if (!clientRes.headersSent) {
        clientRes.writeHead(res.status, res.headers);
      }
      clientRes.end(res.body);
    }
    return;
  }
}

const server = http.createServer(proxyRequest);

server.listen(port, "127.0.0.1", () => {
  console.log(`[nvidia-proxy] listening on http://127.0.0.1:${port}`);
  console.log(`[nvidia-proxy] proxying to ${targetUrl.origin}`);
  console.log(`[nvidia-proxy] retry strategy: 16 tools (8 guaranteed)→8 tools→1 tool (forced)→text-only (max ${MAX_RETRIES} attempts)`);
  console.log(`[nvidia-proxy] also trims multi-tool responses to single tool call`);
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.log(`[nvidia-proxy] ${sig} received, shutting down`);
    server.close(() => process.exit(0));
  });
}

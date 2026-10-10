import { realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const MAX_ATTEMPTS = 4;
const MIN_ATTEMPT_WINDOW_MS = 30_000;
const DELAYS_MS = [10_000, 20_000, 30_000];
// One upstream z.ai 429 puts the whole zai backend into a 30 second cooldown,
// so 10/20/30s retries land inside back-to-back cooldowns. Builds on chiap04
// spent all four attempts and died (477886d2, 7ad93016, 2026-10-10 06:28Z).
// 429 waits at least one full cooldown and may try longer, still inside the
// lane's request budget.
const MAX_ATTEMPTS_429 = 7;
const DELAYS_429_MS = [30_000, 60_000, 90_000, 120_000, 120_000, 120_000];
const GLM_CONTEXT_WINDOW = 128_000;

// Honor a gateway Retry-After (seconds) on 429, capped so one hint cannot
// consume the whole request budget.
function retryAfterMs(response) {
  const seconds = Number(response.headers?.get?.("retry-after"));
  return Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds * 1000, 60_000) : 0;
}

function isGlmModel(model) {
  return typeof model?.id === "string"
    && (/^glm-/i.test(model.id) || /^sk-glm-[sml]$/i.test(model.id));
}
export function requestPolicyForLane(lane) {
  return lane === "glm"
    ? { budgetMs: 570_000, idleTimeoutMs: 600_000 }
    : { budgetMs: 360_000, idleTimeoutMs: 390_000 };
}

export async function configurePiHttpIdleTimeout({
  entrypoint = process.argv[1],
  timeoutMs = 390_000,
  importer = (specifier) => import(specifier),
} = {}) {
  if (typeof entrypoint !== "string" || !entrypoint) {
    throw new Error("Fleet gateway retry requires the running Pi entrypoint");
  }
  const piEntrypoint = realpathSync(entrypoint);
  const dispatcher = pathToFileURL(
    resolve(dirname(piEntrypoint), "../core/http-dispatcher.js"),
  ).href;
  const { configureHttpDispatcher } = await importer(dispatcher);
  if (typeof configureHttpDispatcher !== "function") {
    throw new Error("Running Pi does not expose its HTTP dispatcher configuration");
  }
  configureHttpDispatcher(timeoutMs);
}

async function errorFields(response) {
  if (![502, 503, 413].includes(response.status)) return {};
  const reader = response.clone().body?.getReader();
  if (!reader) return {};
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 65_536) return {};
      chunks.push(value);
    }
    const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const value = parsed.error ?? parsed;
    const fields = {};
    if (["bucket_no_eligible_member", "malformed_response"].includes(value.type)) {
      fields.type = value.type;
    }
    if (["request_too_large", "empty_upstream_response"].includes(value.code)) {
      fields.code = value.code;
    }
    if (value.param === "body") fields.param = "body";
    for (const name of ["actual_bytes", "limit_bytes"]) {
      if (Number.isSafeInteger(value[name]) && value[name] >= 0) fields[name] = value[name];
    }
    return fields;
  } catch {
    return {};
  } finally {
    void reader.cancel().catch(() => {});
  }
}

export function gatewayTransport(
  fetcher,
  record,
  { now = Date.now, wait = sleep, budgetMs = 360_000 } = {},
) {
  if (typeof record !== "function") throw new Error("Session retry evidence is required");
  const state = { terminal: null };
  return {
    state,
    async fetch(input, options = {}) {
      const started = now();
      const controller = new AbortController();
      const budget = setTimeout(() => controller.abort(), budgetMs);
      const external = options.signal ?? (input instanceof Request ? input.signal : undefined);
      const signal = external ? AbortSignal.any([external, controller.signal]) : controller.signal;
      const original = input instanceof Request ? input.clone() : input;
      const replayable = options.body == null || typeof options.body === "string";
      let attempt = 0;
      try {
        for (;;) {
          signal.throwIfAborted();
          attempt += 1;
          const response = await fetcher(
            original instanceof Request ? original.clone() : original,
            { ...options, signal },
          );
          const fields = await errorFields(response);
          // 429 is the gateway's own pool/queue refusal (zai concurrency ceiling):
          // transient by definition. Terminal 429 killed nearly finished Seraph
          // reviews on chiap02 (ae812f7a, bb6da93e, 2026-10-10 01:48Z).
          const transient = response.status === 504 || response.status === 429 || (
            response.status === 503 && fields.type === "bucket_no_eligible_member"
          ) || (
            response.status === 502
            && (fields.type === "malformed_response" || fields.code === "empty_upstream_response")
          );
          const limited = response.status === 429;
          const requestedDelay = limited
            ? Math.max(DELAYS_429_MS[attempt - 1] ?? DELAYS_429_MS.at(-1), retryAfterMs(response))
            : DELAYS_MS[attempt - 1];
          const remaining = budgetMs - (now() - started);
          const retry = transient && replayable
            && attempt < (limited ? MAX_ATTEMPTS_429 : MAX_ATTEMPTS)
            && remaining >= MIN_ATTEMPT_WINDOW_MS;
          const delay = retry && remaining > requestedDelay + MIN_ATTEMPT_WINDOW_MS
            ? requestedDelay : 0;
          record({
            event: retry ? "retry" : "result", attempt, http_status: response.status,
            elapsed_ms: now() - started, delay_ms: retry ? delay : 0,
            ...fields,
            ...(transient && !retry ? { reason: attempt >= (limited ? MAX_ATTEMPTS_429 : MAX_ATTEMPTS) ? "attempt_limit"
              : !replayable ? "unreplayable_request" : "budget_limit" } : {}),
          });
          if (!retry) {
            if (response.status >= 400) {
              state.terminal = { http_status: response.status, ...fields, retryable: false };
            }
            return response;
          }
          await response.body?.cancel();
          await wait(delay, undefined, { signal });
          if (now() - started >= budgetMs) {
            state.terminal = { http_status: response.status, ...fields, retryable: false };
            throw new Error("Fleet gateway recovery budget ended");
          }
        }
      } catch (error) {
        if (controller.signal.aborted) {
          state.terminal = { http_status: 0, reason: "budget_expired", retryable: false };
        }
        record({ event: "ended", attempt, elapsed_ms: now() - started,
          reason: external?.aborted ? "cancelled"
            : controller.signal.aborted ? "budget_expired" : "request_ended" });
        throw error;
      } finally {
        clearTimeout(budget);
      }
    },
  };
}

export function wrapGatewayProvider(provider, record, { budgetMs = 360_000 } = {}) {
  const wrapped = { ...provider };
  for (const method of ["stream", "streamSimple"]) {
    if (typeof provider[method] !== "function") continue;
    wrapped[method] = function (model, context, options = {}) {
      const transport = gatewayTransport(options.fetch ?? globalThis.fetch, (entry) => {
        record({ ...entry, provider: model.provider, model: model.id });
      }, { budgetMs });
      const source = provider[method].call(provider, model, context, {
        ...options, fetch: transport.fetch, maxRetries: 0,
      });
      let partial = false;
      function terminal(message) {
        if (message?.stopReason === "error" && (transport.state.terminal || partial)) {
          // Pi also has a text-matching outer retry loop. The audited transport
          // owns this request's entire budget; do not start another retry round.
          message.gatewayError = transport.state.terminal ?? {
            http_status: 0, reason: "partial_stream", retryable: false,
          };
          message.errorMessage = "Fleet gateway request ended. See session transport evidence.";
        }
        return message;
      }
      return {
        async *[Symbol.asyncIterator]() {
          for await (const event of source) {
            if (event.type?.endsWith("_delta") && event.delta) partial = true;
            if (event.type === "error") terminal(event.error);
            yield event;
          }
        },
        result() { return source.result().then(terminal); },
      };
    };
  }
  return wrapped;
}

export default function gatewayRetry(
  pi,
  { configureIdleTimeout = configurePiHttpIdleTimeout } = {},
) {
  let installed = false;
  const policy = requestPolicyForLane(process.env.SKFLEET_LANE);
  // Pi owns compaction and resumes the same agent run after it. Triggering
  // ctx.compact() from turn_end is fire-and-forget: the turn can settle and
  // print mode can exit while compaction is still running, aborting the worker.
  // Keep native auto-compaction enabled in Pi settings; do not queue a second
  // prompt to simulate a resume.
  pi.on("session_start", async (_event, context) => {
    if (installed) return;
    if (isGlmModel(context.model) && context.model.contextWindow !== GLM_CONTEXT_WINDOW) {
      const configured = await pi.setModel({ ...context.model, contextWindow: GLM_CONTEXT_WINDOW });
      if (configured === false) {
        throw new Error(`Could not cap GLM context window for ${context.model.id}`);
      }
    }
    const provider = context.modelRegistry.getProvider("skgateway");
    if (!provider) return;
    if (typeof provider.streamSimple !== "function") {
      throw new Error("Fleet gateway provider transport is unavailable");
    }
    await configureIdleTimeout({ timeoutMs: policy.idleTimeoutMs });
    pi.registerProvider(wrapGatewayProvider(provider, (entry) => {
      pi.appendEntry("skfleet.gateway_transport_retry", entry);
    }, { budgetMs: policy.budgetMs }));
    installed = true;
  });
}

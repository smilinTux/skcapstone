import { realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const MAX_ATTEMPTS = 4;
const MIN_ATTEMPT_WINDOW_MS = 30_000;
const DELAYS_MS = [10_000, 20_000, 30_000];
// 50k tokens is roughly 200 KB of prompt text and leaves room before slow turns.
const GLM_COMPACT_TOKENS = 50_000;

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
  if (response.status !== 503 && response.status !== 413) return {};
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
    if (value.type === "bucket_no_eligible_member") fields.type = value.type;
    if (value.code === "request_too_large") fields.code = value.code;
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
          const transient = response.status === 504 || (
            response.status === 503 && fields.type === "bucket_no_eligible_member"
          );
          const requestedDelay = DELAYS_MS[attempt - 1];
          const remaining = budgetMs - (now() - started);
          const retry = transient && replayable && attempt < MAX_ATTEMPTS
            && remaining >= MIN_ATTEMPT_WINDOW_MS;
          const delay = retry && remaining > requestedDelay + MIN_ATTEMPT_WINDOW_MS
            ? requestedDelay : 0;
          record({
            event: retry ? "retry" : "result", attempt, http_status: response.status,
            elapsed_ms: now() - started, delay_ms: retry ? delay : 0,
            ...fields,
            ...(transient && !retry ? { reason: attempt >= MAX_ATTEMPTS ? "attempt_limit"
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
  let compactionPending = false;
  const policy = requestPolicyForLane(process.env.SKFLEET_LANE);
  const isGlm = process.env.SKFLEET_LANE === "glm";
  pi.on("turn_end", (_event, context) => {
    const tokens = context.getContextUsage()?.tokens;
    if (!isGlm || compactionPending || tokens == null || tokens < GLM_COMPACT_TOKENS) return;
    compactionPending = true;
    context.compact({
      customInstructions: "Keep the active card contract, source revision, current step, test results, and unresolved blockers. Condense completed exploration and verbose command output.",
      onComplete: () => {
        compactionPending = false;
        pi.sendUserMessage(
          "Continue the original task from the compacted session. Resume at the first unfinished step, preserve completed work, and finish with the required evidence and report.",
          { deliverAs: "followUp" },
        );
      },
      onError: () => { compactionPending = false; },
    });
  });
  pi.on("session_start", async (_event, context) => {
    if (installed) return;
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

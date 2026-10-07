import { setTimeout as sleep } from "node:timers/promises";

const MAX_ATTEMPTS = 4;
const BUDGET_MS = 300_000;
const MIN_ATTEMPT_WINDOW_MS = 30_000;
const DELAYS_MS = [10_000, 20_000, 30_000];

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

export function gatewayTransport(fetcher, record, { now = Date.now, wait = sleep } = {}) {
  if (typeof record !== "function") throw new Error("Session retry evidence is required");
  const state = { terminal: null };
  return {
    state,
    async fetch(input, options = {}) {
      const started = now();
      const controller = new AbortController();
      const budget = setTimeout(() => controller.abort(), BUDGET_MS);
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
          const remaining = BUDGET_MS - (now() - started);
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
          if (now() - started >= BUDGET_MS) {
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

export function wrapGatewayProvider(provider, record) {
  const wrapped = { ...provider };
  for (const method of ["stream", "streamSimple"]) {
    if (typeof provider[method] !== "function") continue;
    wrapped[method] = function (model, context, options = {}) {
      const transport = gatewayTransport(options.fetch ?? globalThis.fetch, (entry) => {
        record({ ...entry, provider: model.provider, model: model.id });
      });
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

export default function gatewayRetry(pi) {
  let installed = false;
  pi.on("session_start", async (_event, context) => {
    if (installed) return;
    const provider = context.modelRegistry.getProvider("skgateway");
    if (!provider) return;
    if (typeof provider.streamSimple !== "function") {
      throw new Error("Fleet gateway provider transport is unavailable");
    }
    pi.registerProvider(wrapGatewayProvider(provider, (entry) => {
      pi.appendEntry("skfleet.gateway_transport_retry", entry);
    }));
    installed = true;
  });
}

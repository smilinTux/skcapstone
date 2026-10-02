import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import { randomUUID } from "node:crypto";

const COMPACTION_REASON = "skfleet-compaction-reason-v1";

function automaticCompactions(entries) {
  const manual = new Set(entries.filter((entry) => entry.type === "custom" &&
    entry.customType === COMPACTION_REASON && entry.data?.reason === "manual")
    .map((entry) => entry.data.entryId));
  // Older Pi entries omit the trigger. Charge unknown history conservatively.
  return entries.filter((entry) => entry.type === "compaction" && !manual.has(entry.id));
}

function writeHandoff(ctx, entries, reason) {
  let folder = os.homedir();
  for (const part of [".skcapstone", "evidence", "work", process.env.SKFLEET_CARD_ID]) {
    folder = path.join(folder, part);
    try { fs.mkdirSync(folder, { mode: 0o700 }); }
    catch (error) { if (error.code !== "EEXIST") throw error; }
    const info = fs.lstatSync(folder);
    if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid()) {
      throw new Error("Unsafe handoff directory");
    }
  }
  if ((fs.lstatSync(folder).mode & 0o777) !== 0o700) {
    throw new Error("Handoff directory must be private");
  }
  const metadata = {
    card: process.env.SKFLEET_CARD_ID,
    claim: process.env.SKFLEET_CLAIM_REVISION,
    owner: process.env.SKAGENT,
    workspace: ctx.cwd,
    session: ctx.sessionManager.getSessionId(),
    sessionFile: ctx.sessionManager.getSessionFile(),
    automaticCompactions: automaticCompactions(entries).length,
    reason,
    timestamp: new Date().toISOString(),
  };
  const latest = entries.filter((entry) => entry.type === "compaction").at(-1);
  const body = "# Fleet worker handoff\n\n" + JSON.stringify(metadata, null, 2) +
    "\n\nStopped without completing or releasing the claim. Controller must verify " +
    "source and evidence before continuing in a fresh session.\n\n" +
    "## Last Pi compaction summary (not independently verified)\n\n" +
    (latest?.summary ?? "No summary available; inspect the retained session.") + "\n";
  let destination = path.join(folder, ".handoff.md");
  let fd;
  try {
    fd = fs.openSync(destination, "wx", 0o600);
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
    destination = path.join(folder, `.handoff.${randomUUID()}.md`);
    fd = fs.openSync(destination, "wx", 0o600);
  }
  try {
    fs.writeFileSync(fd, body);
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  return destination;
}

const MUTATION = /(?:>>?|\b(?:chmod|chown|cp|install|ln|mv|rm|tee|touch|truncate)\b|\bsed\s+[^\n]*-[^\s]*i|\.(?:write|write_text|unlink|rename|replace)\s*\()/;
const EVENT_PATH = /\.skcapstone\/(?:cards\/[^\s/"';&|<>]+\/events\/[^\s/"';&|<>]+|coordination\/card_events\/[^\s/"';&|<>]+)\.jsonl/;

export function isCardEventPath(candidate, home = os.homedir()) {
  if (typeof candidate !== "string" || candidate.length === 0) return false;
  const expanded = candidate
    .replace(/^~(?=\/|$)/, home)
    .replace(/^\$\{HOME\}(?=\/|$)/, home)
    .replace(/^\$HOME(?=\/|$)/, home);
  const absolute = path.resolve(expanded);
  const cards = path.relative(path.join(home, ".skcapstone", "cards"), absolute).split(path.sep);
  const overlay = path.relative(
    path.join(home, ".skcapstone", "coordination", "card_events"), absolute
  ).split(path.sep);
  return (
    (cards.length === 3 && cards[0] && cards[1] === "events" && cards[2].endsWith(".jsonl")) ||
    (overlay.length === 1 && overlay[0] && overlay[0].endsWith(".jsonl"))
  );
}

export function isCardEventMutation(command) {
  if (typeof command !== "string") return false;
  const expanded = command
    .replaceAll("${HOME}", os.homedir())
    .replaceAll("$HOME", os.homedir())
    .replaceAll("~/.skcapstone", `${os.homedir()}/.skcapstone`);
  return EVENT_PATH.test(expanded) && MUTATION.test(expanded);
}

export function isCurrentFleetCardReclaim(command, cardId = process.env.SKFLEET_CARD_ID) {
  if (typeof command !== "string" || !/^[a-z0-9]{8}$/.test(cardId ?? "")) return false;
  const escaped = cardId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\bskcapstone\\s+coord\\s+claim\\s+["']?${escaped}["']?(?:\\s|$)`).test(command);
}

export default function cardStoreGuard(pi) {
  const fleetWorker = /^[a-z0-9]{8}$/.test(process.env.SKFLEET_CARD_ID ?? "");
  let stopped = false;
  async function stop(ctx, reason) {
    stopped = true;
    try {
      const destination = writeHandoff(ctx, ctx.sessionManager.getEntries(), reason);
      console.error(`Fleet compaction limit: handoff preserved at ${destination}`);
    } catch (error) {
      console.error(`Fleet compaction limit: handoff unavailable (${error.code ?? "unsafe-evidence"}); controller intervention required.`);
    }
    // Pi 0.84.4 print-mode shutdown() is a no-op, and abort() alone can retry
    // an overflow. SIGTERM uses Pi's cleanup handler (including detached tools).
    // Keep this event pending until exit so no post-compaction retry can start.
    await new Promise(() => {
      // Pending promises do not keep Node alive long enough to deliver a signal.
      setInterval(() => {}, 1000);
      process.kill(process.pid, "SIGTERM");
    });
  }
  if (fleetWorker) {
    pi.on("session_start", async (_event, ctx) => {
      if (automaticCompactions(ctx.sessionManager.getEntries()).length >= 2) {
        await stop(ctx, "compaction-limit-on-resume");
      }
    });
    pi.on("session_compact", async (event, ctx) => {
      const entries = ctx.sessionManager.getEntries();
      // Pi can report the first matching summary; use the latest saved entry.
      const latest = entries.filter((entry) => entry.type === "compaction").at(-1);
      try {
        pi.appendEntry(COMPACTION_REASON, { entryId: latest.id, reason: event.reason });
      } catch {
        await stop(ctx, "compaction-reason-persistence-failed");
      }
      if (automaticCompactions(ctx.sessionManager.getEntries()).length >= 2) {
        await stop(ctx, "compaction-limit");
      }
    });
  }
  pi.on("tool_call", async (event) => {
    if (stopped) {
      return { block: true, terminate: true, reason: "Compaction limit reached; session must stop." };
    }
    if (
      (event.toolName === "write" || event.toolName === "edit") &&
      isCardEventPath(event.input?.path)
    ) {
      return {
        block: true,
        terminate: true,
        reason: "Direct CardStore JSONL mutation is forbidden. Use skcapstone coord.",
      };
    }
    if (event.toolName === "bash" && isCardEventMutation(event.input?.command)) {
      return {
        block: true,
        terminate: true,
        reason: "Direct CardStore JSONL mutation is forbidden. Use skcapstone coord.",
      };
    }
    if (event.toolName === "bash" && isCurrentFleetCardReclaim(event.input?.command)) {
      return {
        block: true,
        terminate: false,
        reason: "This fleet card is already claimed at the dispatched revision. Continue without claiming it again.",
      };
    }
  });
}

import os from "node:os";
import path from "node:path";

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
  pi.on("tool_call", async (event) => {
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

import os from "node:os";
import path from "node:path";

const MUTATION = /(?:>>?|\b(?:chmod|chown|cp|install|ln|mv|rm|tee|touch|truncate)\b|\bsed\s+[^\n]*-[^\s]*i)/;
const EVENT_PATH_IN_COMMAND = /\.skcapstone\/cards\/[^\s/"';&|<>]+\/events\/[^\s/"';&|<>]+\.jsonl/;

export function isStructuralEventPath(candidate, home = os.homedir()) {
  if (typeof candidate !== "string" || candidate.length === 0) return false;
  const expanded = candidate
    .replace(/^~(?=\/|$)/, home)
    .replace(/^\$\{HOME\}(?=\/|$)/, home)
    .replace(/^\$HOME(?=\/|$)/, home);
  const relative = path.relative(path.join(home, ".skcapstone", "cards"), path.resolve(expanded));
  const parts = relative.split(path.sep);
  return (
    !relative.startsWith(`..${path.sep}`) &&
    parts.length === 3 &&
    parts[0].length > 0 &&
    parts[1] === "events" &&
    parts[2].endsWith(".jsonl")
  );
}

export function isStructuralEventMutation(command) {
  if (typeof command !== "string") return false;
  const expanded = command
    .replaceAll("${HOME}", os.homedir())
    .replaceAll("$HOME", os.homedir())
    .replaceAll("~/.skcapstone", `${os.homedir()}/.skcapstone`);
  return EVENT_PATH_IN_COMMAND.test(expanded) && MUTATION.test(expanded);
}

export default function cardStoreGuard(pi) {
  pi.on("tool_call", async (event) => {
    if (
      (event.toolName === "write" || event.toolName === "edit") &&
      isStructuralEventPath(event.input?.path)
    ) {
      return {
        block: true,
        terminate: true,
        reason: "Direct CardStore event-file writes are forbidden. Use skcapstone coord.",
      };
    }
    if (event.toolName === "bash" && isStructuralEventMutation(event.input?.command)) {
      return {
        block: true,
        terminate: true,
        reason: "Direct CardStore event-file mutation is forbidden. Use skcapstone coord.",
      };
    }
  });
}

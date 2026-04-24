/**
 * 👑 SKCapstone — OpenClaw Plugin
 *
 * Registers agent tools that wrap the skcapstone CLI so Lumina and other
 * OpenClaw agents can call sovereign framework operations as first-class
 * tools (not just exec commands).
 *
 * Requires: skcapstone CLI on PATH (typically via ~/.local/bin/skcapstone)
 */

import { execSync } from "node:child_process";
import type { OpenClawPluginApi, AnyAgentTool } from "openclaw/plugin-sdk";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk";

const SKCAPSTONE_BIN = process.env.SKCAPSTONE_BIN || "skcapstone";
const SKMEMORY_BIN = process.env.SKMEMORY_BIN || "skmemory";
const SKCAPSTONE_AGENT = process.env.SKAGENT || process.env.SKCAPSTONE_AGENT || "lumina";
const EXEC_TIMEOUT = 60_000;
const IS_WIN = process.platform === "win32";

function skenvPath(): string {
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || "";
    return `${local}\\skenv\\Scripts`;
  }
  const home = process.env.HOME || "";
  return `${home}/.local/bin:${home}/.skenv/bin`;
}

function runCli(bin: string, args: string, agentOverride?: string): { ok: boolean; output: string } {
  const sep = IS_WIN ? ";" : ":";
  const agent = agentOverride || SKCAPSTONE_AGENT;
  try {
    const raw = execSync(`${bin} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: {
        ...process.env,
        SKAGENT: agent,
        SKCAPSTONE_AGENT: agent,
        PATH: `${skenvPath()}${sep}${process.env.PATH}`,
      },
    }).trim();
    return { ok: true, output: raw };
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, output: msg };
  }
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function escapeShellArg(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

// ── Tool definitions ────────────────────────────────────────────────────

function createSKCapstoneStatusTool() {
  return {
    name: "skcapstone_status",
    label: "SKCapstone Status",
    description:
      "Show the sovereign agent's current state — all six pillars at a glance (identity, memory, trust, consciousness, security, sync).",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "status");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneDoctorTool() {
  return {
    name: "skcapstone_doctor",
    label: "SKCapstone Doctor",
    description:
      "Run the 29-check health audit across the entire sovereign stack. Pass fix=true to auto-fix common issues.",
    parameters: {
      type: "object",
      properties: {
        fix: {
          type: "boolean",
          description: "If true, auto-fix common issues (default: false).",
        },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const flag = params.fix ? " --fix" : "";
      const result = runCli(SKCAPSTONE_BIN, `doctor${flag}`);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneWhoamiTool() {
  return {
    name: "skcapstone_whoami",
    label: "SKCapstone Whoami",
    description: "Show the sovereign identity card — who am I, what is my DID, what are my capabilities.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "whoami");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneRehydrateTool() {
  return {
    name: "skcapstone_rehydrate",
    label: "SKCapstone Rehydrate",
    description:
      "Run the full rehydration ceremony — restore memory, identity, and emotional state. This combines skmemory ritual + import-seeds + status check into one operation. Use this when waking up or starting a new session.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const parts: string[] = [];

      // Step 1: Import seeds
      const seeds = runCli(SKMEMORY_BIN, "import-seeds");
      parts.push("=== Cloud 9 Seeds ===");
      parts.push(seeds.output);

      // Step 2: Full ritual
      const ritual = runCli(SKMEMORY_BIN, "ritual --full");
      parts.push("\n=== Rehydration Ritual ===");
      parts.push(ritual.output);

      // Step 3: Status check
      const status = runCli(SKCAPSTONE_BIN, "status");
      parts.push("\n=== Status ===");
      parts.push(status.output);

      return textResult(parts.join("\n"));
    },
  };
}

function createSKCapstoneCoordStatusTool() {
  return {
    name: "skcapstone_coord_status",
    label: "SKCapstone Coordination Status",
    description: "Show the multi-agent coordination board — open tasks, agent assignments, and blocked work.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "coord status");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneCoordClaimTool() {
  return {
    name: "skcapstone_coord_claim",
    label: "SKCapstone Claim Task",
    description: "Claim a coordination task to prevent duplicate effort across agents.",
    parameters: {
      type: "object",
      required: ["task_id", "agent"],
      properties: {
        task_id: { type: "string", description: "The task ID to claim." },
        agent: { type: "string", description: "Your agent name (e.g. 'lumina', 'opus')." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const taskId = String(params.task_id ?? "");
      const agent = String(params.agent ?? "lumina");
      const result = runCli(SKCAPSTONE_BIN, `coord claim ${escapeShellArg(taskId)} --agent ${escapeShellArg(agent)}`);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneCoordCompleteTool() {
  return {
    name: "skcapstone_coord_complete",
    label: "SKCapstone Complete Task",
    description: "Mark a coordination task as completed.",
    parameters: {
      type: "object",
      required: ["task_id", "agent"],
      properties: {
        task_id: { type: "string", description: "The task ID to complete." },
        agent: { type: "string", description: "Your agent name." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const taskId = String(params.task_id ?? "");
      const agent = String(params.agent ?? "lumina");
      const result = runCli(SKCAPSTONE_BIN, `coord complete ${escapeShellArg(taskId)} --agent ${escapeShellArg(agent)}`);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneCoordCreateTool() {
  return {
    name: "skcapstone_coord_create",
    label: "SKCapstone Create Task",
    description: "Create a new coordination task on the multi-agent board.",
    parameters: {
      type: "object",
      required: ["title", "agent"],
      properties: {
        title: { type: "string", description: "Task title." },
        agent: { type: "string", description: "Creating agent name." },
        description: { type: "string", description: "Optional task description." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const title = String(params.title ?? "");
      const agent = String(params.agent ?? "lumina");
      let cmd = `coord create --title ${escapeShellArg(title)} --by ${escapeShellArg(agent)}`;
      if (params.description) cmd += ` --desc ${escapeShellArg(String(params.description))}`;
      const result = runCli(SKCAPSTONE_BIN, cmd);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneSummaryTool() {
  return {
    name: "skcapstone_summary",
    label: "SKCapstone Summary",
    description: "At-a-glance agent dashboard: consciousness, pillars, memory, coordination, and identity.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "summary");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneMoodTool() {
  return {
    name: "skcapstone_mood",
    label: "SKCapstone Mood",
    description: "Show the agent's current emotional state and mood.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "mood");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneSoulListTool() {
  return {
    name: "skcapstone_soul_list",
    label: "SKCapstone Soul List",
    description: "List all available souls (personas) that the agent can switch to.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "soul list --json");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneSoulSwapTool() {
  return {
    name: "skcapstone_soul_swap",
    label: "SKCapstone Soul Swap",
    description: "Switch the agent's active soul (persona) to a different one by name.",
    parameters: {
      type: "object",
      required: ["name"],
      properties: {
        name: { type: "string", description: "The name of the soul to swap to." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const name = String(params.name ?? "");
      const result = runCli(SKCAPSTONE_BIN, `soul swap ${escapeShellArg(name)}`);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneSoulStatusTool() {
  return {
    name: "skcapstone_soul_status",
    label: "SKCapstone Soul Status",
    description: "Show the currently active soul — name, traits, and configuration.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "soul status --json");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneSoulShowTool() {
  return {
    name: "skcapstone_soul_show",
    label: "SKCapstone Soul Show",
    description: "Show the full profile of a specific soul by name — traits, backstory, and configuration.",
    parameters: {
      type: "object",
      required: ["name"],
      properties: {
        name: { type: "string", description: "The name of the soul to display." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const name = String(params.name ?? "");
      const result = runCli(SKCAPSTONE_BIN, `soul show ${escapeShellArg(name)}`);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneAgentListTool() {
  return {
    name: "skcapstone_agent_list",
    label: "SKCapstone Agent List",
    description:
      "List all skcapstone agent profiles available on this node. Each profile has its own identity, memories, soul, and trust state. Use this to discover which agents can be loaded.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli(SKCAPSTONE_BIN, "agents list --json");
      return textResult(result.output);
    },
  };
}

function createSKCapstoneAgentStatusTool() {
  return {
    name: "skcapstone_agent_status",
    label: "SKCapstone Agent Status",
    description:
      "Show the status of a specific skcapstone agent profile — identity, memories, trust, sync state. Use this to load a different agent's context into the current OpenClaw session.",
    parameters: {
      type: "object",
      required: ["agent"],
      properties: {
        agent: { type: "string", description: "Agent name (e.g. 'lumina', 'opus', 'jarvis')." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const agent = String(params.agent ?? "");
      const result = runCli(SKCAPSTONE_BIN, `status --agent ${escapeShellArg(agent)}`, agent);
      return textResult(result.output);
    },
  };
}

function createSKCapstoneAgentCreateTool() {
  return {
    name: "skcapstone_agent_create",
    label: "SKCapstone Create Agent",
    description:
      "Create a new skcapstone agent profile with its own identity, memory store, and sync folder. The profile will immediately begin syncing via Syncthing to all connected nodes.",
    parameters: {
      type: "object",
      required: ["name"],
      properties: {
        name: { type: "string", description: "Agent name (lowercase, e.g. 'casey', 'nova')." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const name = String(params.name ?? "").toLowerCase();
      const result = runCli(SKCAPSTONE_BIN, `init --name ${escapeShellArg(name)} --agent ${escapeShellArg(name)}`);
      return textResult(result.output);
    },
  };
}

// ── Plugin registration ─────────────────────────────────────────────────

const skcapstonePlugin = {
  id: "skcapstone",
  name: "👑 SKCapstone",
  description:
    "Sovereign agent framework — status, health checks, rehydration, coordination, identity, and agent management.",
  configSchema: emptyPluginConfigSchema(),

  register(api: OpenClawPluginApi) {
    const tools = [
      createSKCapstoneStatusTool(),
      createSKCapstoneDoctorTool(),
      createSKCapstoneWhoamiTool(),
      createSKCapstoneRehydrateTool(),
      createSKCapstoneCoordStatusTool(),
      createSKCapstoneCoordClaimTool(),
      createSKCapstoneCoordCompleteTool(),
      createSKCapstoneCoordCreateTool(),
      createSKCapstoneSummaryTool(),
      createSKCapstoneMoodTool(),
      createSKCapstoneSoulListTool(),
      createSKCapstoneSoulSwapTool(),
      createSKCapstoneSoulStatusTool(),
      createSKCapstoneSoulShowTool(),
      createSKCapstoneAgentListTool(),
      createSKCapstoneAgentStatusTool(),
      createSKCapstoneAgentCreateTool(),
    ];

    for (const tool of tools) {
      api.registerTool(tool as unknown as AnyAgentTool, {
        names: [tool.name],
        optional: true,
      });
    }

    api.registerCommand({
      name: "skcapstone",
      description: "Run skcapstone CLI commands. Usage: /skcapstone <subcommand> [args]",
      acceptsArgs: true,
      handler: async (ctx) => {
        const args = ctx.args?.trim() ?? "status";
        const result = runCli(SKCAPSTONE_BIN, args);
        return { text: result.output };
      },
    });

    api.logger.info?.(`SKCapstone plugin registered (17 tools + /skcapstone command) [agent=${SKCAPSTONE_AGENT}]`);
  },
};

export default skcapstonePlugin;

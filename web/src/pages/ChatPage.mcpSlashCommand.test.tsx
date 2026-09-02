import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";

// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions). These tests don't exercise that, so stub the hook to avoid
// needing a QueryClientProvider around every bare render.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({ "claude-sdk": "Claude SDK" }),
}));
// The /mcp handler fetches the session agent's servers through the shared
// authenticated fetch; mock only that export so the rest of identity
// (author id, host config) keeps its real behavior.
vi.mock("@/lib/identity", async (importOriginal) => ({
  // Type-only reference to the module namespace below — erased at compile
  // time, so it doesn't violate vi.mock's no-outer-variables hoisting rule.
  ...(await importOriginal<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));

import * as identity from "@/lib/identity";
import { BUILTIN_SLASH_COMMANDS } from "@/components/SlashCommandMenu";
import { Composer, formatMcpServersReport } from "./ChatPage";

function jsonResponse(body: unknown, { ok = true, status = 200 } = {}): Response {
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    selectedAgentId: null,
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

/** The composer textarea, located by its aria-label. */
function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

// These tests pin the fix for the reported gap: typing /mcp into the web
// composer used to match no built-in, fall through to the plaintext send
// path, and reach the model as an ordinary chat message — the user saw
// their command echoed as a bubble and got a prose reply, with no MCP
// status anywhere. /mcp is now a built-in that renders the session
// agent's MCP servers inline.
describe("Composer /mcp slash command", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_mcp", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("registers /mcp as a built-in so it can't fall through to plaintext", () => {
    expect(BUILTIN_SLASH_COMMANDS["/mcp"]).toBeTruthy();
  });

  it("renders the agent's MCP servers inline instead of sending chat text", async () => {
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [
          {
            name: "echomcp",
            transport: "http",
            url: "http://127.0.0.1:9000/mcp",
            description: null,
          },
        ],
      }),
    );
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // Trailing space closes the suggestions menu so Enter submits the command.
    fireEvent.change(ta, { target: { value: "/mcp " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    // The command executes locally: the server list appears inline…
    await waitFor(() => {
      expect(screen.getByText(/echomcp/)).toBeInTheDocument();
    });
    expect(identity.authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_mcp/agent/mcp-servers",
    );
    // …and nothing was dispatched to the model.
    expect(onSend).not.toHaveBeenCalled();
    expect(onSendSlashCommand).not.toHaveBeenCalled();
  });

  it("says so when the agent has no MCP servers configured", async () => {
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse({ object: "list", data: [] }),
    );
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/mcp " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText(/No MCP servers configured/)).toBeInTheDocument();
    });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("surfaces a fetch failure inline rather than leaking the command as chat", async () => {
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse({}, { ok: false, status: 502 }),
    );
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/mcp " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByText(/Failed to load MCP servers/)).toBeInTheDocument();
    });
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("formatMcpServersReport", () => {
  it("reports the empty configuration", () => {
    expect(formatMcpServersReport([])).toBe("No MCP servers configured for this agent.");
  });

  it("lists http servers with their endpoint and description", () => {
    const report = formatMcpServersReport([
      {
        name: "github",
        transport: "http",
        url: "https://mcp.example.com/sse",
        description: "GitHub MCP server",
      },
    ]);
    expect(report).toContain("MCP servers (1):");
    expect(report).toContain("github (http): https://mcp.example.com/sse — GitHub MCP server");
  });

  it("lists stdio servers with their command line", () => {
    const report = formatMcpServersReport([
      { name: "files", transport: "stdio", command: "uvx", args: ["mcp-server-files"] },
    ]);
    expect(report).toContain("files (stdio): uvx mcp-server-files");
  });
});

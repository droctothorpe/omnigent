import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearProvisionalConversationIds,
  isProvisionalConversationId,
  proposeConversationId,
  registerProvisionalConversationId,
  removeProvisionalConversationId,
} from "./provisionalConversationId";
import * as randomUuidModule from "./randomUUID";

afterEach(clearProvisionalConversationIds);

describe("provisionalConversationId", () => {
  it("generates a bare lowercase 32-character UUID", () => {
    vi.spyOn(randomUuidModule, "randomUUID").mockReturnValue(
      "AABBCCDD-EEFF-4011-8233-445566778899",
    );

    const id = proposeConversationId();

    expect(id).toBe("aabbccddeeff40118233445566778899");
    expect(isProvisionalConversationId(id)).toBe(false);
  });

  it("tracks membership independently of id shape", () => {
    const id = "12345678123456781234567812345678";

    expect(isProvisionalConversationId(id)).toBe(false);
    registerProvisionalConversationId(id);
    expect(isProvisionalConversationId(id)).toBe(true);
    removeProvisionalConversationId(id);
    expect(isProvisionalConversationId(id)).toBe(false);
  });
});

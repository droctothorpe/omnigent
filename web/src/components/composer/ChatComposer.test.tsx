import { createRef, type FormEvent } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ChatComposer,
  ComposerInputArea,
  ComposerTextarea,
  ComposerActionRow,
  ComposerSendButton,
} from "./ChatComposer";

describe("ChatComposer", () => {
  it("keeps route-owned input and submit handlers on the shared surface", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    const inputRef = createRef<HTMLTextAreaElement>();
    render(
      <form onSubmit={onSubmit}>
        <ChatComposer data-testid="shared-composer">
          <ComposerInputArea>
            <ComposerTextarea ref={inputRef} aria-label="Message" onChange={onChange} />
          </ComposerInputArea>
          <ComposerActionRow>
            <span>Context controls</span>
            <ComposerSendButton label="Send" />
          </ComposerActionRow>
        </ChatComposer>
      </form>,
    );
    expect(screen.getByTestId("shared-composer")).toHaveAttribute("data-composer-card");
    expect(inputRef.current).toBe(screen.getByRole("textbox"));
    fireEvent.change(inputRef.current!, { target: { value: "Keep this draft" } });
    expect(onChange).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("preserves interrupt and pending-creation states", () => {
    const { rerender } = render(<ComposerSendButton label="Interrupt" interrupt />);
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
    rerender(<ComposerSendButton label="Starting session" busy disabled />);
    expect(screen.getByRole("button", { name: "Starting session" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Starting session" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });
});

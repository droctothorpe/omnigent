import { randomUUID } from "./randomUUID";

const provisionalIds = new Set<string>();

export function proposeConversationId(): string {
  return randomUUID().replaceAll("-", "").toLowerCase();
}

export function registerProvisionalConversationId(id: string): void {
  provisionalIds.add(id);
}

export function isProvisionalConversationId(id: string | null | undefined): boolean {
  return id != null && provisionalIds.has(id);
}

export function removeProvisionalConversationId(id: string): void {
  provisionalIds.delete(id);
}

export function clearProvisionalConversationIds(): void {
  provisionalIds.clear();
}

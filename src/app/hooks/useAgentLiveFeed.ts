import { useCallback, useRef, useState } from "react";
import type { AgentAuditSnapshot, AgentLiveItem } from "../../shared/types";

export function useAgentLiveFeed() {
  const [agentLiveItems, setAgentLiveItems] = useState<AgentLiveItem[]>([]);
  const [, setAgentAuditTrail] = useState<AgentAuditSnapshot[]>([]);
  const [agentRunViewPinned, setAgentRunViewPinned] = useState(false);
  const agentLiveIdRef = useRef(0);

  const makeAgentLiveId = useCallback(() => `agent-live-${Date.now()}-${agentLiveIdRef.current++}`, []);

  const pushAgentLiveItem = useCallback((item: Omit<AgentLiveItem, "id">) => {
    setAgentLiveItems((prev) => [...prev, { ...item, id: makeAgentLiveId() }]);
  }, [makeAgentLiveId]);

  const appendAssistantLiveText = useCallback((chunk: string, tone: AgentLiveItem["tone"] = "default", exact = false) => {
    const text = exact ? chunk : chunk.trim();
    if (!text) return;
    setAgentLiveItems((prev) => {
      const next = [...prev];
      const lastAssistantIndex = (() => {
        for (let index = next.length - 1; index >= 0; index -= 1) {
          if (next[index].role === "assistant" && next[index].tone === tone) return index;
        }
        return -1;
      })();
      if (lastAssistantIndex >= 0) {
        const last = next[lastAssistantIndex];
        next[lastAssistantIndex] = {
          ...last,
          text: exact ? `${last.text}${text}` : `${last.text}${last.text ? " " : ""}${text}`,
        };
        return next;
      }
      next.push({ id: makeAgentLiveId(), role: "assistant", tone, text: exact ? text.trimStart() : text });
      return next;
    });
  }, [makeAgentLiveId]);

  const resetAgentRunView = useCallback(() => {
    setAgentRunViewPinned(false);
    setAgentLiveItems([]);
    setAgentAuditTrail([]);
  }, []);

  return {
    agentLiveItems,
    setAgentLiveItems,
    setAgentAuditTrail,
    agentRunViewPinned,
    setAgentRunViewPinned,
    makeAgentLiveId,
    pushAgentLiveItem,
    appendAssistantLiveText,
    resetAgentRunView,
  };
}

import React from "react";
import { Bot, User, Wrench } from "lucide-react";
import { type AgentLiveItem } from "../../types";

interface AgentLiveStageProps {
  items: AgentLiveItem[];
  agentStatus: "idle" | "thinking" | "error";
  personaName?: string;
  workingMsg?: string;
  emptyText?: string;
  compact?: boolean;
  includeTools?: boolean;
  conversationOnly?: boolean;
}

function roleLabel(item: AgentLiveItem, personaName: string) {
  if (item.role === "user") return "You";
  if (item.role === "tool") return "Action";
  return personaName;
}

function roleIcon(item: AgentLiveItem) {
  if (item.role === "user") return <User size={13} />;
  if (item.role === "tool") return <Wrench size={13} />;
  return <Bot size={13} />;
}

export const AgentLiveStage: React.FC<AgentLiveStageProps> = ({
  items,
  agentStatus,
  personaName = "Agent",
  workingMsg,
  emptyText = "Run agent untuk lihat jawaban Clara muncul live di sini.",
  compact = false,
  includeTools = true,
  conversationOnly = false,
}) => {
  const visibleItems = items.filter((item) => {
    if (!includeTools && item.role === "tool") return false;
    if (!conversationOnly) return true;
    if (item.role === "user") return true;
    return item.role === "assistant" && (item.tone === "default" || item.tone === "error" || !item.tone);
  });

  return (
    <div className={`agentLiveStage ${compact ? "compact" : ""}`}>
      {visibleItems.length === 0 ? <div className="agentLiveEmpty">{emptyText}</div> : null}

      {visibleItems.map((item) => (
        <div key={item.id} className={`agentLiveBubble ${item.role} ${item.tone || "default"}`}>
          <div className="agentLiveBubbleMeta">
            <span className="agentLiveBubbleIcon">{roleIcon(item)}</span>
            <span>{roleLabel(item, personaName)}</span>
          </div>
          <div className="agentLiveBubbleText">{item.text}</div>
          {item.meta ? <div className="agentLiveBubbleSubtext">{item.meta}</div> : null}
        </div>
      ))}

      {agentStatus === "thinking" ? (
        <div className="agentLiveTyping">
          <span className="spinner" />
          <span>{workingMsg || "Clara lagi jalan…"}</span>
        </div>
      ) : null}
    </div>
  );
};

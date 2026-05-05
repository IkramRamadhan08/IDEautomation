import React from "react";
import { Bot, User, Wrench } from "lucide-react";
import { type AgentLiveItem } from "../../types";

interface AgentLiveStageProps {
  items: AgentLiveItem[];
  agentStatus: "idle" | "thinking" | "error";
  workingMsg?: string;
  emptyText?: string;
  compact?: boolean;
}

function roleLabel(item: AgentLiveItem) {
  if (item.role === "user") return "You";
  if (item.role === "tool") return "Action";
  return "Clara";
}

function roleIcon(item: AgentLiveItem) {
  if (item.role === "user") return <User size={13} />;
  if (item.role === "tool") return <Wrench size={13} />;
  return <Bot size={13} />;
}

export const AgentLiveStage: React.FC<AgentLiveStageProps> = ({
  items,
  agentStatus,
  workingMsg,
  emptyText = "Run agent untuk lihat percakapan kerja dan aksi yang lagi jalan.",
  compact = false,
}) => {
  return (
    <div className={`agentLiveStage ${compact ? "compact" : ""}`}>
      {items.length === 0 ? <div className="agentLiveEmpty">{emptyText}</div> : null}

      {items.map((item) => (
        <div key={item.id} className={`agentLiveBubble ${item.role} ${item.tone || "default"}`}>
          <div className="agentLiveBubbleMeta">
            <span className="agentLiveBubbleIcon">{roleIcon(item)}</span>
            <span>{roleLabel(item)}</span>
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

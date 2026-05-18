import React from "react";
import Draggable from "react-draggable";
import { motion, AnimatePresence } from "framer-motion";
import { X, Paperclip, SendHorizontal, Play, Sparkles, MessageSquarePlus } from "lucide-react";
import { getBuildModeProfile, getModeQuickPrompts } from "../../agent/runtime";
import { AgentLiveStage } from "./AgentLiveStage";
import { type AgentLiveItem, type BuildMode, type UploadedImageAsset } from "../../types";

type OrbMode = "idle" | "playful" | "curious" | "sleepy" | "sleeping" | "working" | "celebrate" | "surprised" | "error";

interface AgentOrbProps {
  ws: string | null;
  buildMode: BuildMode;
  agentStatus: "idle" | "thinking" | "error";
  agentReply: string;
  agentWidgetOpen: boolean;
  agentOrbPosition: { x: number; y: number } | null;
  workingMsg: string;
  agentLiveItems: AgentLiveItem[];
  agentRunViewPinned: boolean;
  editorStatus: string;
  activeFile: string;
  previewUrl: string;
  agentInput: string;
  attachedImage: UploadedImageAsset | null;
  imageUploading: boolean;
  onAgentInputChange: (value: string) => void;
  onPickAgentImage: () => void;
  onClearAttachedImage: () => void;
  onRunAgent: () => void;
  onEnsurePreviewRunning: () => void;
  onToggleOpen: () => void;
  onResetRunView: () => void;
  onSetPosition: (pos: { x: number; y: number } | null) => void;
}

function pickRandom<T>(items: T[]): T {
  return items[Math.floor(Math.random() * items.length)];
}

function compactBubbleText(text: string, max = 220) {
  const clean = text.replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, max - 1).trimEnd()}…`;
}

export const AgentOrb: React.FC<AgentOrbProps> = ({
  ws,
  buildMode,
  agentStatus,
  agentReply,
  agentWidgetOpen,
  agentOrbPosition,
  workingMsg,
  agentLiveItems,
  agentRunViewPinned,
  editorStatus,
  activeFile,
  previewUrl,
  agentInput,
  attachedImage,
  imageUploading,
  onAgentInputChange,
  onPickAgentImage,
  onClearAttachedImage,
  onRunAgent,
  onEnsurePreviewRunning,
  onToggleOpen,
  onResetRunView,
  onSetPosition,
}) => {
  const nodeRef = React.useRef(null);
  const playfulUntilRef = React.useRef<number>(0);
  const reactionUntilRef = React.useRef<number>(0);
  const prevEditorStatusRef = React.useRef<string>(editorStatus);
  const prevPreviewUrlRef = React.useRef<string>(previewUrl);
  const prevAgentReplyRef = React.useRef<string>(agentReply);
  const modeProfile = getBuildModeProfile(buildMode);
  const [orbMode, setOrbMode] = React.useState<OrbMode>("idle");
  const [bubbleText, setBubbleText] = React.useState<string>(modeProfile.idleLines[0]);
  const [lastInteractionAt, setLastInteractionAt] = React.useState<number>(() => Date.now());
  const [isCompactPointer, setIsCompactPointer] = React.useState(false);

  if (!ws) return null;

  const handleStop = (_e: unknown, data: { x: number; y: number }) => {
    if (isCompactPointer) return;
    onSetPosition({ x: data.x, y: data.y });
  };

  const activeFileName = activeFile.split("/").pop() || activeFile;
  const promptHasIntent = agentInput.trim().length > 8;
  const showRunExperience = agentRunViewPinned || agentStatus === "thinking";
  const personaClass = buildMode === "full-agent" ? "clara" : "raka";
  const visibleConversationItems = agentLiveItems.filter((item) => item.role === "user" || item.role === "assistant");
  const latestAssistantText = [...agentLiveItems].reverse().find((item) => item.role === "assistant")?.text || agentReply;
  const collapsedBubbleText = agentStatus === "thinking"
    ? compactBubbleText(latestAssistantText)
    : bubbleText;
  const contextChips = [
    `${modeProfile.personaName} • ${modeProfile.personaRole}`,
    activeFileName ? `File: ${activeFileName}` : null,
    previewUrl ? "Preview live" : "Preview idle",
    attachedImage ? `Asset: ${attachedImage.name}` : null,
  ].filter(Boolean) as string[];

  const quickPrompts = getModeQuickPrompts(buildMode, { activeFile, previewUrl }).map((item) => ({
    label: item.label,
    prompt: item.prompt,
    action: item.action === "start-preview" ? () => onEnsurePreviewRunning() : undefined,
  }));

  const triggerReaction = React.useCallback((mode: OrbMode, text: string, durationMs = 5000) => {
    reactionUntilRef.current = Date.now() + durationMs;
    setLastInteractionAt(Date.now());
    setOrbMode(mode);
    setBubbleText(text);
  }, []);

  const wakeUp = React.useCallback((playful = false) => {
    const now = Date.now();
    const wasSleeping = orbMode === "sleeping" || orbMode === "sleepy";
    setLastInteractionAt(now);
    if (playful) {
      playfulUntilRef.current = now + 5000;
      setOrbMode("playful");
      setBubbleText(pickRandom(modeProfile.playfulLines));
      return;
    }
    if (agentStatus === "idle") {
      if (wasSleeping) {
        reactionUntilRef.current = now + 3000;
        setOrbMode("surprised");
        setBubbleText(pickRandom(modeProfile.surprisedLines));
      } else {
        setOrbMode("idle");
        setBubbleText(pickRandom(modeProfile.idleLines));
      }
    }
  }, [agentStatus, modeProfile, orbMode]);

  const applyQuickPrompt = React.useCallback((prompt: string) => {
    onAgentInputChange(prompt);
    setLastInteractionAt(Date.now());
    setOrbMode("curious");
    setBubbleText("Sip, ini udah lebih spesifik. Aku siap gas.");
    if (!agentWidgetOpen) onToggleOpen();
  }, [agentWidgetOpen, onAgentInputChange, onToggleOpen]);

  React.useEffect(() => {
    if (agentStatus === "thinking") {
      setOrbMode("working");
      return;
    }
    if (agentStatus === "error") {
      setOrbMode("error");
      setBubbleText(pickRandom(modeProfile.errorLines));
      return;
    }
    if (agentWidgetOpen) {
      setOrbMode("idle");
      return;
    }

    const interval = window.setInterval(() => {
      const now = Date.now();
      const idleMs = now - lastInteractionAt;
      if (reactionUntilRef.current > now || playfulUntilRef.current > now) {
        return;
      }
      if (idleMs > 42000) {
        setOrbMode("sleeping");
      } else if (idleMs > 18000) {
        setOrbMode("sleepy");
      } else {
        setOrbMode("idle");
      }
    }, 1200);

    return () => window.clearInterval(interval);
  }, [agentStatus, agentWidgetOpen, lastInteractionAt, modeProfile, workingMsg]);

  React.useEffect(() => {
    if (agentWidgetOpen || agentStatus !== "idle") return;

    if (orbMode === "sleepy") setBubbleText(pickRandom(modeProfile.sleepyLines));
    else if (orbMode === "sleeping") setBubbleText(pickRandom(modeProfile.sleepingLines));
    else if (orbMode === "playful") setBubbleText(pickRandom(modeProfile.playfulLines));
    else if (orbMode === "curious") setBubbleText(pickRandom(modeProfile.curiousLines));
    else if (orbMode === "celebrate") setBubbleText(pickRandom(modeProfile.celebrateLines));
    else if (orbMode === "surprised") setBubbleText(pickRandom(modeProfile.surprisedLines));
    else setBubbleText(pickRandom(modeProfile.idleLines));
  }, [orbMode, agentStatus, agentWidgetOpen, modeProfile]);

  React.useEffect(() => {
    if (agentWidgetOpen || agentStatus !== "idle") return;
    if (orbMode === "sleeping") return;

    const interval = window.setInterval(() => {
      if (reactionUntilRef.current > Date.now()) return;
      setBubbleText((current) => {
        const pool = orbMode === "sleepy"
          ? modeProfile.sleepyLines
          : orbMode === "playful"
            ? modeProfile.playfulLines
            : orbMode === "curious"
              ? modeProfile.curiousLines
              : modeProfile.idleLines;
        let next = pickRandom(pool);
        if (next === current && pool.length > 1) next = pool[(pool.indexOf(next) + 1) % pool.length];
        return next;
      });
    }, 11000);

    return () => window.clearInterval(interval);
  }, [orbMode, agentStatus, agentWidgetOpen, modeProfile]);

  React.useEffect(() => {
    if (agentWidgetOpen) return;
    if (prevEditorStatusRef.current === editorStatus) return;
    prevEditorStatusRef.current = editorStatus;

    if (/Saved /i.test(editorStatus)) {
      triggerReaction("celebrate", `Cakep, ${activeFileName || "file"} udah kesimpen.`);
    } else if (/Preview live/i.test(editorStatus)) {
      triggerReaction("celebrate", "Preview nyala. Ayo diliatin hasilnya.");
    } else if (/Failed/i.test(editorStatus)) {
      triggerReaction("error", pickRandom(modeProfile.errorLines));
    } else if (/Opening |Loaded /i.test(editorStatus) && activeFileName) {
      triggerReaction("curious", `Aku ngintip ${activeFileName} dulu ya.`, 4000);
    }
  }, [activeFileName, agentWidgetOpen, editorStatus, modeProfile, triggerReaction]);

  React.useEffect(() => {
    if (agentWidgetOpen) return;
    if (prevPreviewUrlRef.current === previewUrl) return;
    const hadPreview = Boolean(prevPreviewUrlRef.current);
    prevPreviewUrlRef.current = previewUrl;
    if (!hadPreview && previewUrl) {
      triggerReaction("celebrate", "Preview bangun. Sekarang enak buat dicek.", 5000);
    }
  }, [agentWidgetOpen, previewUrl, triggerReaction]);

  React.useEffect(() => {
    if (agentWidgetOpen) return;
    if (prevAgentReplyRef.current === agentReply) return;
    prevAgentReplyRef.current = agentReply;
    if (agentReply.trim()) {
      triggerReaction("playful", compactBubbleText(agentReply), 6000);
    }
  }, [agentReply, agentWidgetOpen, triggerReaction]);

  React.useEffect(() => {
    const query = window.matchMedia("(max-width: 720px), (pointer: coarse)");
    const sync = () => {
      const compact = query.matches;
      setIsCompactPointer(compact);
      if (compact && agentOrbPosition) onSetPosition(null);
    };
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, [agentOrbPosition, onSetPosition]);

  React.useEffect(() => {
    if (!agentWidgetOpen) return;
    if (agentStatus !== "idle") return;
    if (!promptHasIntent) return;
    setOrbMode("curious");
    setBubbleText(activeFileName
      ? `Oke, aku nangkep. Fokusku sekarang ${activeFileName}.`
      : `Oke, aku nangkep arahnya. ${modeProfile.personaName} standby di sini.`);
  }, [activeFileName, agentStatus, agentWidgetOpen, modeProfile, promptHasIntent]);

  const statusLabel = agentStatus === "thinking"
    ? "Working"
    : agentStatus === "error"
      ? "Attention"
      : orbMode === "sleeping"
        ? "Sleeping"
        : orbMode === "sleepy"
          ? "Sleepy"
          : orbMode === "playful"
            ? "Goofy"
            : orbMode === "curious"
              ? "Curious"
              : orbMode === "celebrate"
                ? "Hyped"
                : orbMode === "surprised"
                  ? "Alert"
                  : "Ready";

  const initialPosition = agentOrbPosition || { x: 0, y: 0 };
  const openOrb = () => {
    wakeUp(false);
    onToggleOpen();
  };

  return (
    <Draggable
      nodeRef={nodeRef}
      defaultPosition={initialPosition}
      position={isCompactPointer ? { x: 0, y: 0 } : undefined}
      disabled={isCompactPointer}
      onStop={handleStop}
      cancel=".agentOrbPanel"
    >
      <div ref={nodeRef} className={`agentOrb ${personaClass} ${agentWidgetOpen ? "open" : "collapsed"}`}>
        <AnimatePresence>
          {!agentWidgetOpen && collapsedBubbleText ? (
            <motion.div
              key={`${orbMode}:${collapsedBubbleText}`}
              initial={{ opacity: 0, y: 10, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.96 }}
              className={`agentOrbBubble ${orbMode}`}
            >
              {collapsedBubbleText}
            </motion.div>
          ) : null}
        </AnimatePresence>

        {!agentWidgetOpen && agentStatus === "idle" && quickPrompts.length > 0 ? (
          <div className="agentOrbDock">
            {quickPrompts.slice(0, 2).map((item) => (
              <button
                key={item.label}
                className="agentOrbDockChip"
                onClick={() => item.prompt ? applyQuickPrompt(item.prompt) : item.action?.()}
              >
                {item.label}
              </button>
            ))}
          </div>
        ) : null}

        <AnimatePresence>
          {agentWidgetOpen && (
            <motion.div
              initial={{ opacity: 0, scale: 0.96, y: 12 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.96, y: 12 }}
              className="agentOrbPanel"
            >
              <div className="agentOrbHeader">
                <div>
                  <div className="agentOrbTitle">{modeProfile.personaName}</div>
                  <div className={`agentStatusPill ${agentStatus}`}>{statusLabel}</div>
                </div>
                <button className="agentOrbClose" onClick={onToggleOpen}>
                  <X size={16} />
                </button>
              </div>

              <div className="agentOrbContent">
                {contextChips.length > 0 ? (
                  <div className="agentOrbContextRow">
                    {contextChips.map((chip) => (
                      <span key={chip} className="agentOrbContextChip">{chip}</span>
                    ))}
                  </div>
                ) : null}

                {showRunExperience ? (
                  <>
                    <div className="agentOrbRunHeader">
                      <div>
                        <div className="agentOrbSectionLabel">Live run</div>
                        <div className="agentOrbRunTitle">{agentStatus === "thinking" ? `${modeProfile.personaName} lagi kerja live` : `Lanjut ngobrol dengan ${modeProfile.personaName}`}</div>
                      </div>
                      {agentStatus !== "thinking" ? (
                        <button className="btn subtleBtn agentOrbNewTaskBtn" onClick={onResetRunView}>
                          <MessageSquarePlus size={14} />
                          <span>Brief baru</span>
                        </button>
                      ) : null}
                    </div>

                    <AgentLiveStage
                      items={agentLiveItems.length > 0 ? agentLiveItems : visibleConversationItems}
                      agentStatus={agentStatus}
                      personaName={modeProfile.personaName}
                      workingMsg={workingMsg}
                      emptyText={agentReply || `Begitu kamu run, jawaban ${modeProfile.personaName} bakal muncul live di sini.`}
                      includeTools={false}
                      conversationOnly
                      showTyping={false}
                    />
                    {agentStatus !== "thinking" ? (
                      <>
                        <textarea
                          className="textarea promptBox agentOrbPrompt"
                          placeholder="Tulis follow-up, minta revisi, atau kasih task berikutnya..."
                          value={agentInput}
                          onChange={(e) => onAgentInputChange(e.target.value)}
                        />

                        <div className="agentOrbActions">
                          <button className="btn subtleBtn" onClick={onPickAgentImage} disabled={imageUploading}>
                            <Paperclip size={14} />
                            <span>{imageUploading ? "Uploading..." : "Attach"}</span>
                          </button>
                          <button className="btn primary" onClick={onRunAgent} disabled={!agentInput.trim()}>
                            <SendHorizontal size={14} />
                            <span>Run</span>
                          </button>
                        </div>
                      </>
                    ) : null}
                  </>
                ) : (
                  <>
                    <textarea
                      className="textarea promptBox agentOrbPrompt"
                      placeholder={buildMode === "full-agent"
                        ? "Kasih brief, target produk, atau suruh Clara build di Full Preview..."
                        : "Ceritain blocker, file yang lagi susah, atau minta Raka bantu di titik ini..."}
                      value={agentInput}
                      onChange={(e) => onAgentInputChange(e.target.value)}
                    />

                    <div className="agentOrbQuickGrid">
                      {quickPrompts.slice(0, 4).map((item) => (
                        <button
                          key={item.label}
                          className="agentOrbQuickAction"
                          onClick={() => item.prompt ? applyQuickPrompt(item.prompt) : item.action?.()}
                        >
                          {item.action ? <Play size={13} /> : <Sparkles size={13} />}
                          <span>{item.label}</span>
                        </button>
                      ))}
                    </div>

                    <div className="agentOrbActions">
                      <button className="btn subtleBtn" onClick={onPickAgentImage} disabled={imageUploading}>
                        <Paperclip size={14} />
                        <span>{imageUploading ? "Uploading..." : "Attach"}</span>
                      </button>
                      <button className="btn primary" onClick={onRunAgent} disabled={!agentInput.trim()}>
                        <SendHorizontal size={14} />
                        <span>Run</span>
                      </button>
                    </div>

                    {attachedImage ? (
                      <div className="attachedImageChip">
                        <span className="attachedImageName">{attachedImage.name}</span>
                        <span className="attachedImagePath">{attachedImage.path}</span>
                        <button className="attachedImageRemove" onClick={onClearAttachedImage} aria-label="Remove attached image">
                          ×
                        </button>
                      </div>
                    ) : null}
                  </>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <motion.button
          type="button"
          aria-label={`${agentWidgetOpen ? "Close" : "Open"} ${modeProfile.personaName} agent command panel`}
          className={`agentOrbButton ${agentStatus} ${orbMode}`}
          onClick={openOrb}
          onMouseEnter={() => wakeUp(false)}
          onDoubleClick={() => wakeUp(true)}
          animate={
            isCompactPointer
              ? { y: 0, rotate: 0, scale: 1 }
              : orbMode === "sleeping"
              ? { y: [0, -2, 0], scale: [1, 0.98, 1] }
              : orbMode === "playful"
                ? { rotate: [0, -8, 8, 0], scale: [1, 1.03, 1] }
                : orbMode === "celebrate"
                  ? { y: [0, -8, 0], scale: [1, 1.08, 1] }
                  : orbMode === "surprised"
                    ? { scale: [1, 1.14, 1] }
                    : orbMode === "working"
                      ? { scale: [1, 1.06, 1] }
                      : { y: [0, -4, 0] }
          }
          transition={isCompactPointer ? { duration: 0.12 } : { duration: orbMode === "playful" ? 0.5 : orbMode === "surprised" ? 0.35 : orbMode === "celebrate" ? 0.7 : orbMode === "working" ? 0.9 : 2.6, repeat: orbMode === "surprised" ? 1 : Infinity, ease: "easeInOut" }}
        >
          <span className={`agentOrbFace ${orbMode}`}>
            <span className="agentOrbPersonaMark">{buildMode === "full-agent" ? "C" : "R"}</span>
            <span className="orbEye left" />
            <span className="orbEye right" />
            <span className="orbMouth" />
            <span className="orbCheek left" />
            <span className="orbCheek right" />
          </span>
          {(promptHasIntent || attachedImage || previewUrl) ? <span className="agentOrbBadgePulse" /> : null}
        </motion.button>
      </div>
    </Draggable>
  );
};

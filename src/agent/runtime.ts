import { type BuildMode } from "../types";

export type AgentQuickPrompt = {
  label: string;
  prompt?: string;
  action?: "start-preview";
};

export type AgentInputIntent = {
  kind: "command" | "conversation" | "mixed" | "inspection";
  confidence: number;
  rationale: string;
  shouldWriteFiles: boolean;
  shouldRunTools: boolean;
};

export type AgentRunPlan = {
  requestEditorStatus: string;
  shouldDrivePreview: boolean;
  shouldRunValidation: boolean;
  shouldAuditPreview: boolean;
  intent: AgentInputIntent;
};

export type BuildModeProfile = {
  mode: BuildMode;
  label: string;
  personaName: string;
  personaRole: string;
  topbarSubtitle: string;
  settingsDescription: string;
  modeSummary: string;
  requestEditorStatus: string;
  idleLines: string[];
  playfulLines: string[];
  curiousLines: string[];
  sleepyLines: string[];
  sleepingLines: string[];
  celebrateLines: string[];
  surprisedLines: string[];
  errorLines: string[];
};

const PREVIEW_INTENT_RE = /\b(preview|run|launch|start|ship|deploy)\b/i;
const VALIDATION_INTENT_RE = /\b(fix|bug|audit|review|polish|refine|build|production|preview|ship|launch|repair|bikin|buat|tambah|tambahin|ubah|rapihin|benahin|perbaiki|validasi|cek)\b/i;
const AUDIT_INTENT_RE = /\b(audit|ux|ui|design|landing|hero|preview|polish|refine)\b/i;
const WRITE_RE = /\b(fix|build|ship|implement|create|add|remove|update|change|edit|refactor|repair|wire|connect|integrate|generate|scaffold|run|start|launch|deploy|bikin|buat|tambahin|tambah|hapus|ubah|rapihin|benahin|perbaiki|jalanin|pasang|sambungin|integrasi)\b/i;
const INSPECTION_RE = /\b(debug|audit|review|validate|check|inspect|analy[sz]e|cek|validasi|analisa|analisis)\b/i;
const READONLY_AUDIT_RE = /\b(audit|review|cek|check|inspect|analy[sz]e|jelasin|explain|laporin|report)\b/i;
const BUILDER_RE = /\b(app|builder|feature|ui|ux|preview|project|repo|component|state|style|css|tsx|react|vite|file|folder|mcp|memory|agentic|agent|graph|rag)\b/i;
const CONVERSATION_RE = /\b(hi|hello|hey|hai|halo|hei|thanks|thank you|status|update|udah|sudah|gimana|gmn|bro|bang|sip|mantap|jelasin|jelaskan|kenapa|apa|apaan|why|what|how|brainstorm|ngobrol|chat)\b/i;
const QUESTION_RE = /\?|^(apa|apaan|gimana|gmn|kenapa|mengapa|why|what|how|can|could|would|is|are|do|does|did)\b/i;
const SHORT_CHAT_RE = /^(p+|hi+|hello+|hey+|hai+|halo+|hei+|yo+|ok|oke|sip|siap|bro|bang|thanks|makasih|mantap)[!.?\s]*$/i;
const BARE_FOLLOWUP_RE = /^(gas|lanjut|lanjutin|next|continue|go|oke lanjut|yaudah lanjut)[!.?\s]*$/i;
const FOLLOWUP_WRITE_RE = /^\s*(gas|lanjut|lanjutin|go|execute|eksekusi|oke lanjut|yaudah lanjut)\b/i;
const WRITE_OBJECT_RE = /\b(file|page|screen|ui|ux|component|button|modal|form|layout|style|css|tsx|react|vite|route|api|endpoint|database|schema|table|auth|login|project|app|landing|navbar|sidebar|terminal|agent|memory|provider|model)\b/i;

const PROFILES: Record<BuildMode, BuildModeProfile> = {
  "full-agent": {
    mode: "full-agent",
    label: "Full agent",
    personaName: "Clara",
    personaRole: "Autonomous product builder",
    topbarSubtitle: "Clara drives the build from rough brief to finished product",
    settingsDescription: "Clara takes ownership, builds broadly, and pushes toward a preview-ready result.",
    modeSummary: "Best when you want the agent to take over the build and ship something coherent end to end.",
    requestEditorStatus: "Clara lagi build produk ini sampai rapi…",
    idleLines: [
      "Clara standby. Kasih brief, nanti aku jahit sampai jadi produk.",
      "Kalau mau pasrahkan build-nya, aku ambil alih dari sini.",
      "Aku lagi mikirin cara bikin ini berasa kayak produk beneran.",
    ],
    playfulLines: [
      "Clara aktif. Aku bisa jadi terlalu niat kalau disuruh polish.",
      "Kalau brief-nya setengah matang, biar aku yang matengin.",
      "Aku udah siap nyapu edge case sambil benahin copy.",
    ],
    curiousLines: [
      "Oke, aku baca brief dan bentuk produknya dulu.",
      "Aku lagi nyocokin intent user sama hasil preview.",
      "Sip, aku lihat struktur app-nya sebelum mulai ngebut.",
    ],
    sleepyLines: [
      "Kalau ada produk yang mau dibangun, bangunin Clara ya.",
      "Aku mulai ngantuk. Kasih target build yang jelas dong.",
    ],
    sleepingLines: [
      "zZz... Clara tidur sampai ada produk yang harus diship.",
      "...tidur sambil mimpi layout yang rapi dan CTA yang masuk akal...",
    ],
    celebrateLines: [
      "Nah, ini baru kelihatan kayak produk ✨",
      "Cakep. Hasilnya makin siap dipamerin.",
      "Sip, Clara suka arah build yang ini.",
    ],
    surprisedLines: [
      "Eh, ada gerakan. Clara bangun.",
      "Oke, aku on lagi. Mari bikin ini jadi serius.",
    ],
    errorLines: [
      "Ada yang jebol dikit. Clara benerin.",
      "Oke, ada ledakan kecil. Aku ambil alih debugging-nya.",
    ],
  },
  hybrid: {
    mode: "hybrid",
    label: "Hybrid",
    personaName: "Raka",
    personaRole: "Live coding copilot",
    topbarSubtitle: "Raka watches your context and helps right where the build gets tricky",
    settingsDescription: "Raka stays close to your active file, current preview, and the exact problem you're solving.",
    modeSummary: "Best when you are still driving the code and only want sharp help at the hard parts.",
    requestEditorStatus: "Raka lagi mantau context editormu dan bantu di titik yang susah…",
    idleLines: [
      "Raka jagain context-mu. Kalau mentok, panggil aja.",
      "Aku lihat alur coding-mu. Lempar bagian susahnya ke sini.",
      "Kamu yang nyetir, aku yang bantu pas belokannya tajam.",
    ],
    playfulLines: [
      "Raka standby. Aku nggak takeover kok, kecuali kamu minta.",
      "Kalau bug-nya licin, aku bantu pegangin.",
      "Aku diem dulu, tapi kalau kamu mentok aku nyamber.",
    ],
    curiousLines: [
      "Oke, aku baca file yang lagi kamu sentuh.",
      "Sebentar, aku cocokkan file aktif sama preview-nya.",
      "Sip, aku lihat dulu kenapa bagian ini terasa seret.",
    ],
    sleepyLines: [
      "Kalau sudah ada bagian susah, bangunin Raka ya.",
      "Masih sepi. Aku standby kalau kamu butuh assist.",
    ],
    sleepingLines: [
      "zZz... Raka tidur tipis sambil jaga repo.",
      "...ngorok kecil sambil nunggu ada bug yang bandel...",
    ],
    celebrateLines: [
      "Nah, sekarang alurnya lebih enak diterusin.",
      "Sip, titik susahnya kebuka.",
      "Cakep, sekarang kamu bisa lanjut gas lagi.",
    ],
    surprisedLines: [
      "Eh, oke. Raka bangun, kita lihat bagian ini.",
      "Gerak dikit langsung kebaca. Aku bantu cek.",
    ],
    errorLines: [
      "Ada yang meledak kecil. Raka bantu bongkar.",
      "Sip, error ketemu. Kita beresin pelan-pelan.",
    ],
  },
};

export function getBuildModeProfile(mode: BuildMode): BuildModeProfile {
  return PROFILES[mode] ?? PROFILES.hybrid;
}

export function getModeQuickPrompts(
  mode: BuildMode,
  options: { activeFile: string; previewUrl: string }
): AgentQuickPrompt[] {
  if (mode === "full-agent") {
    return [
      { label: "Ship feature", prompt: "Ambil alih feature ini dan ship hasil yang rapi, konsisten, dan siap dipreview." },
      { label: "Polish app", prompt: "Polish keseluruhan app ini biar terasa seperti produk jadi. Rapikan UX, copy, states, dan detail visual." },
      options.previewUrl
        ? { label: "Audit preview", prompt: "Audit preview yang lagi jalan lalu perbaiki semua hal yang bikin hasilnya terasa belum matang." }
        : { label: "Start preview", action: "start-preview" },
      { label: "Build from brief", prompt: "Ambil brief yang ada sekarang lalu bangun hasil end-to-end yang coherent dan production-ready." },
    ];
  }

  const activeFileName = options.activeFile.split("/").pop() || options.activeFile;
  return [
    activeFileName
      ? { label: "Review file", prompt: `Review ${activeFileName}. Cari bug, state aneh, atau refactor yang paling worth it.` }
      : { label: "Review context", prompt: "Lihat context editor sekarang dan bantu cari bagian yang paling rawan atau membingungkan." },
    activeFileName
      ? { label: "Polish file", prompt: `Bantu polish ${activeFileName} tanpa takeover project. Fokus ke titik yang lagi aku kerjain.` }
      : { label: "Polish area", prompt: "Bantu polish area yang lagi aktif tanpa ngerombak app secara luas." },
    options.previewUrl
      ? { label: "Audit current UI", prompt: "Audit UI yang lagi live dan kasih perbaikan scoped yang bisa langsung bantu progresku." }
      : { label: "Start preview", action: "start-preview" },
    { label: "Explain blocker", prompt: "Lihat apa yang lagi kubangun dan bantu pecahkan blocker paling mungkin di titik ini." },
  ];
}

export function classifyAgentInputIntent(input: string, buildMode: BuildMode): AgentInputIntent {
  const normalizedInput = input.trim();
  const lowered = normalizedInput.toLowerCase();
  const wordCount = normalizedInput ? normalizedInput.split(/\s+/).length : 0;
  const hasQuestion = QUESTION_RE.test(normalizedInput);
  const isShortChat = SHORT_CHAT_RE.test(normalizedInput);
  const isBareFollowup = BARE_FOLLOWUP_RE.test(normalizedInput);
  const hasWriteObject = WRITE_OBJECT_RE.test(normalizedInput);
  if (!normalizedInput || isShortChat) {
    return {
      kind: "conversation",
      confidence: 0.99,
      rationale: "short conversational prompt",
      shouldWriteFiles: false,
      shouldRunTools: false,
    };
  }
  let writeScore = 0;
  let inspectionScore = 0;
  let conversationScore = 0;
  const signals: string[] = [];

  if (WRITE_RE.test(normalizedInput)) {
    writeScore += 1.45;
    signals.push("explicit build language");
  }
  if (INSPECTION_RE.test(normalizedInput)) {
    inspectionScore += 1.1;
    signals.push("inspection language");
  }
  if (BUILDER_RE.test(normalizedInput)) {
    writeScore += 0.55;
    inspectionScore += 0.35;
    signals.push("app-builder context");
  }
  if (CONVERSATION_RE.test(normalizedInput)) {
    conversationScore += 0.9;
    signals.push("chat language");
  }
  if (normalizedInput.includes("?")) conversationScore += 0.2;
  if (/\n/.test(normalizedInput)) {
    writeScore += 0.15;
    inspectionScore += 0.1;
  }
  if (normalizedInput.split(/\s+/).length <= 4 && conversationScore > 0 && writeScore < 1.5 && inspectionScore < 1.35) conversationScore += 0.45;
  if (/\b(agentic app builder|app builder)\b/i.test(lowered)) {
    writeScore += 0.4;
    inspectionScore += 0.2;
    signals.push("agentic builder framing");
  }

  const explicitWrite = /\b(can you|please|tolong|implement|build|fix|bikin|buat|tambahin|ubah|rapihin|perbaiki)\b/i.test(normalizedInput)
    || (FOLLOWUP_WRITE_RE.test(normalizedInput) && hasWriteObject);
  const readonlyAudit = READONLY_AUDIT_RE.test(normalizedInput);
  if (explicitWrite) writeScore += 0.75;
  if (hasQuestion && !explicitWrite) {
    conversationScore += 0.65;
    writeScore *= 0.55;
  }
  if (isBareFollowup && !hasWriteObject) {
    conversationScore += 0.7;
    writeScore = Math.min(writeScore, 0.8);
  }

  let kind: AgentInputIntent["kind"] = "conversation";
  if (hasQuestion && !explicitWrite && inspectionScore < 1.1) kind = "conversation";
  else if (readonlyAudit && !explicitWrite && writeScore < 2.1) kind = "inspection";
  else if (writeScore >= 1.7 && conversationScore >= 0.95) kind = "mixed";
  else if (explicitWrite || writeScore >= 1.85) kind = "command";
  else if (inspectionScore >= 1.1 && writeScore < 1.7) kind = "inspection";
  else if (buildMode === "full-agent" && writeScore >= 1.1) kind = explicitWrite || (hasWriteObject && wordCount >= 5) ? "command" : "conversation";
  else if (readonlyAudit && writeScore < 1.7) kind = "inspection";

  if (kind === "mixed" && !explicitWrite && writeScore < 1.95) kind = readonlyAudit ? "inspection" : "conversation";
  if ((kind === "command" || kind === "mixed") && !explicitWrite && !hasWriteObject) kind = "conversation";

  const shouldWriteFiles = (kind === "command" || kind === "mixed") && (explicitWrite || (writeScore >= 1.95 && hasWriteObject));
  const shouldRunTools = shouldWriteFiles && (writeScore + inspectionScore) >= 2.15;
  const total = Math.max(writeScore + inspectionScore + conversationScore, 0.001);
  const confidence = Math.max(writeScore, inspectionScore, conversationScore) / total;

  return {
    kind,
    confidence: Math.max(0.51, Math.min(0.99, Number(confidence.toFixed(2)))),
    rationale: signals[0] || "fallback heuristic",
    shouldWriteFiles,
    shouldRunTools,
  };
}

export function getAgentRunPlan(buildMode: BuildMode, input: string, previewUrl: string): AgentRunPlan {
  const normalizedInput = input.trim();
  const intent = classifyAgentInputIntent(normalizedInput, buildMode);
  const wantsPreview = Boolean(previewUrl) || PREVIEW_INTENT_RE.test(normalizedInput);
  const wantsValidation = Boolean(previewUrl) || PREVIEW_INTENT_RE.test(normalizedInput) || VALIDATION_INTENT_RE.test(normalizedInput);
  const wantsAudit = Boolean(previewUrl) && AUDIT_INTENT_RE.test(normalizedInput);
  const requestEditorStatus = intent.kind === "command" || intent.kind === "mixed"
    ? PROFILES[buildMode].requestEditorStatus
    : intent.kind === "inspection"
      ? "Agent lagi audit dan baca context dulu, belum masuk mode ubah file…"
      : "Agent lagi nangkep maksudmu dulu, belum masuk mode ubah file…";

  if (buildMode === "full-agent") {
    return {
      requestEditorStatus,
      shouldDrivePreview: intent.shouldWriteFiles,
      shouldRunValidation: intent.shouldWriteFiles,
      shouldAuditPreview: intent.shouldWriteFiles && (wantsAudit || wantsPreview),
      intent,
    };
  }

  return {
    requestEditorStatus,
    shouldDrivePreview: intent.shouldWriteFiles && wantsPreview,
    shouldRunValidation: intent.shouldWriteFiles && wantsValidation,
    shouldAuditPreview: intent.shouldWriteFiles && wantsAudit,
    intent,
  };
}

export function buildRepairPrompt(
  buildMode: BuildMode,
  originalInput: string,
  validationReport?: string | null,
  previewAuditReport?: string | null,
  shellReport?: string | null,
  passNumber?: number,
  maxPasses?: number,
): string {
  const profile = getBuildModeProfile(buildMode);
  const modeDirective = buildMode === "full-agent"
    ? `${profile.personaName}, stay in full ownership mode. Tighten the product until it feels coherent, runnable, and ready to show.`
    : `${profile.personaName}, stay in scoped copilot mode. Fix the blocker cleanly without turning this into a broad rewrite.`;

  const sections = [originalInput.trim(), modeDirective];

  if (passNumber && maxPasses) {
    sections.push(
      `Repair loop pass ${passNumber} of ${maxPasses}. Do not repeat the same failed approach; use the latest command/validation/audit output as ground truth.`
    );
  }

  if (validationReport?.trim()) {
    sections.push(
      `Validation failed after applying the draft. Fix only what is necessary so the project passes these checks.\n\nValidation results:\n${validationReport.trim()}`
    );
  }

  if (shellReport?.trim()) {
    sections.push(
      `One or more project commands were executed after applying the draft. Read the command output, identify the actual blocker, and fix the project so the command succeeds on the next run.\n\nCommand results:\n${shellReport.trim()}`
    );
  }

  if (previewAuditReport?.trim()) {
    sections.push(
      buildMode === "full-agent"
        ? `The live preview still feels weak or unfinished. Improve the implementation until the result feels production-ready.\n\nPreview audit:\n${previewAuditReport.trim()}`
        : `The live preview still has local UX or clarity issues around the current task. Fix them without taking over unrelated areas.\n\nPreview audit:\n${previewAuditReport.trim()}`
    );
  }

  return sections.filter(Boolean).join("\n\n");
}

export function buildVerifierRepairPrompt(
  buildMode: BuildMode,
  originalInput: string,
  verifierReport: string,
): string {
  const profile = getBuildModeProfile(buildMode);
  const modeDirective = buildMode === "full-agent"
    ? `${profile.personaName}, stay in full ownership mode and produce a complete, valid implementation.`
    : `${profile.personaName}, stay scoped, but return a valid actionable fix.`;

  return [
    originalInput.trim(),
    modeDirective,
    "Your previous output failed the agent verifier before it could be safely applied.",
    `Verifier failures:\n${verifierReport.trim()}`,
    "Return a corrected JSON result. If this is a build/edit request, include valid file changes or valid shell actions. Do not return raw tool/MCP actions as the final output.",
  ].filter(Boolean).join("\n\n");
}

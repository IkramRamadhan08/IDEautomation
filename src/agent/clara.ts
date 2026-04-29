import { type BuildModeProfile, type AgentQuickPrompt, type AgentRunPlan } from "./modeTypes";

const PREVIEW_INTENT_RE = /\b(preview|run|launch|start|ship|deploy)\b/i;
const VALIDATION_INTENT_RE = /\b(fix|bug|audit|review|polish|refine|build|production|preview|ship|launch|repair)\b/i;
const AUDIT_INTENT_RE = /\b(audit|ux|ui|design|landing|hero|preview|polish|refine)\b/i;

export const claraProfile: BuildModeProfile = {
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
};

export function buildClaraRunPlan(input: string): AgentRunPlan {
  const normalizedInput = input.trim();
  return {
    requestEditorStatus: claraProfile.requestEditorStatus,
    shouldDrivePreview: true,
    shouldRunValidation: true,
    shouldAuditPreview:
      AUDIT_INTENT_RE.test(normalizedInput) ||
      PREVIEW_INTENT_RE.test(normalizedInput) ||
      VALIDATION_INTENT_RE.test(normalizedInput) ||
      true,
  };
}

export function buildClaraRepairPrompt(
  originalInput: string,
  validationReport?: string | null,
  previewAuditReport?: string | null,
): string {
  const sections = [
    originalInput.trim(),
    `${claraProfile.personaName}, stay in full ownership mode. Tighten the product until it feels coherent, runnable, and ready to show.`,
  ];

  if (validationReport?.trim()) {
    sections.push(
      `Validation failed after applying the draft. Fix only what is necessary so the project passes these checks.\n\nValidation results:\n${validationReport.trim()}`
    );
  }

  if (previewAuditReport?.trim()) {
    sections.push(
      `The live preview still feels weak or unfinished. Improve the implementation until the result feels production-ready.\n\nPreview audit:\n${previewAuditReport.trim()}`
    );
  }

  return sections.filter(Boolean).join("\n\n");
}

export function getClaraQuickPrompts(previewUrl: string): AgentQuickPrompt[] {
  return [
    { label: "Ship feature", prompt: "Ambil alih feature ini dan ship hasil yang rapi, konsisten, dan siap dipreview." },
    { label: "Polish app", prompt: "Polish keseluruhan app ini biar terasa seperti produk jadi. Rapikan UX, copy, states, dan detail visual." },
    previewUrl
      ? { label: "Audit preview", prompt: "Audit preview yang lagi jalan lalu perbaiki semua hal yang bikin hasilnya terasa belum matang." }
      : { label: "Start preview", action: "start-preview" },
    { label: "Build from brief", prompt: "Ambil brief yang ada sekarang lalu bangun hasil end-to-end yang coherent dan production-ready." },
  ];
}

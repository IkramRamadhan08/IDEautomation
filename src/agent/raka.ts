import { type BuildModeProfile, type AgentQuickPrompt, type AgentRunPlan } from "./modeTypes";

const PREVIEW_INTENT_RE = /\b(preview|run|launch|start|ship|deploy)\b/i;
const VALIDATION_INTENT_RE = /\b(fix|bug|audit|review|polish|refine|build|production|preview|ship|launch|repair)\b/i;
const AUDIT_INTENT_RE = /\b(audit|ux|ui|design|landing|hero|preview|polish|refine)\b/i;

export const rakaProfile: BuildModeProfile = {
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
};

export function buildRakaRunPlan(input: string, previewUrl: string): AgentRunPlan {
  const normalizedInput = input.trim();
  return {
    requestEditorStatus: rakaProfile.requestEditorStatus,
    shouldDrivePreview: Boolean(previewUrl) || PREVIEW_INTENT_RE.test(normalizedInput),
    shouldRunValidation:
      Boolean(previewUrl) || PREVIEW_INTENT_RE.test(normalizedInput) || VALIDATION_INTENT_RE.test(normalizedInput),
    shouldAuditPreview: Boolean(previewUrl) && AUDIT_INTENT_RE.test(normalizedInput),
  };
}

export function buildRakaRepairPrompt(
  originalInput: string,
  validationReport?: string | null,
  previewAuditReport?: string | null,
): string {
  const sections = [
    originalInput.trim(),
    `${rakaProfile.personaName}, stay in scoped copilot mode. Fix the blocker cleanly without turning this into a broad rewrite.`,
  ];

  if (validationReport?.trim()) {
    sections.push(
      `Validation failed after applying the draft. Fix only what is necessary so the project passes these checks.\n\nValidation results:\n${validationReport.trim()}`
    );
  }

  if (previewAuditReport?.trim()) {
    sections.push(
      `The live preview still has local UX or clarity issues around the current task. Fix them without taking over unrelated areas.\n\nPreview audit:\n${previewAuditReport.trim()}`
    );
  }

  return sections.filter(Boolean).join("\n\n");
}

export function getRakaQuickPrompts(activeFile: string, previewUrl: string): AgentQuickPrompt[] {
  const activeFileName = activeFile.split("/").pop() || activeFile;

  return [
    activeFileName
      ? { label: "Review file", prompt: `Review ${activeFileName}. Cari bug, state aneh, atau refactor yang paling worth it.` }
      : { label: "Review context", prompt: "Lihat context editor sekarang dan bantu cari bagian yang paling rawan atau membingungkan." },
    activeFileName
      ? { label: "Polish file", prompt: `Bantu polish ${activeFileName} tanpa takeover project. Fokus ke titik yang lagi aku kerjain.` }
      : { label: "Polish area", prompt: "Bantu polish area yang lagi aktif tanpa ngerombak app secara luas." },
    previewUrl
      ? { label: "Audit current UI", prompt: "Audit UI yang lagi live dan kasih perbaikan scoped yang bisa langsung bantu progresku." }
      : { label: "Start preview", action: "start-preview" },
    { label: "Explain blocker", prompt: "Lihat apa yang lagi kubangun dan bantu pecahkan blocker paling mungkin di titik ini." },
  ];
}

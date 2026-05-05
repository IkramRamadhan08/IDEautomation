export type ToastMessage = {
  kind: "success" | "warning" | "error";
  message: string;
};

function unwrapErrorDetail(message: string): string {
  const trimmed = message.trim();
  if (!trimmed) return "Unknown error";
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail.trim();
    }
  } catch {
    // fall through to raw text
  }
  return trimmed;
}

export function normalizeErrorMessage(message: string): string {
  const detail = unwrapErrorDetail(message);
  const lower = detail.toLowerCase();

  if (
    lower.includes("exceeded your current quota") ||
    lower.includes("insufficient_quota") ||
    lower.includes("billing details")
  ) {
    return "Kuota API provider ini habis atau project API key-nya belum punya akses billing. Coba ganti model yang lebih ringan, pakai provider lain, atau cek quota/billing key yang sedang aktif.";
  }

  if (
    lower.includes("does not have access to model") ||
    lower.includes("model_not_found") ||
    lower.includes("unknown model") ||
    lower.includes("unsupported model")
  ) {
    return "Model yang dipilih belum bisa dipakai oleh API key ini. Coba pilih model lain yang lebih ringan atau yang memang aktif di akun/provider itu.";
  }

  if (
    lower.includes("cannot run the javascript preview because npm/pnpm/yarn/bun is not installed") ||
    lower.includes("javascript tooling is not available in this runtime")
  ) {
    return "Environment ini bisa ngedit file, tapi belum punya runtime JavaScript buat jalanin preview. Jadi agent masih bisa nulis kode, tapi preview live tidak bisa dinyalakan di host ini.";
  }

  if (
    lower.includes("request entity too large") ||
    lower.includes("function_payload_too_large") ||
    lower.includes("payload too large")
  ) {
    return "Folder yang diimport kebesaran buat sekali kirim. Coba kecilkan isi import, buang file berat seperti build output, atau upload per bagian.";
  }

  return detail;
}

export function errorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return normalizeErrorMessage(raw);
}

export function notifyToast(
  notify: {
    success: (message: string) => void;
    warning: (message: string) => void;
    error: (message: string) => void;
  },
  toast: ToastMessage,
) {
  if (toast.kind === "success") notify.success(toast.message);
  else if (toast.kind === "warning") notify.warning(toast.message);
  else notify.error(toast.message);
}

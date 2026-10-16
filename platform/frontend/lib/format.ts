// Display formatting only. Values stay decimal strings everywhere else.
const eur = new Intl.NumberFormat("bg-BG", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const eurFine = new Intl.NumberFormat("bg-BG", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 6 });
const num = new Intl.NumberFormat("bg-BG", { maximumFractionDigits: 2 });

export const toNum = (v: string | number | null | undefined): number | null =>
  v === null || v === undefined || v === "" ? null : Number(v);

export function fmtEur(v: string | number | null | undefined, fine = false): string {
  const n = toNum(v);
  return n === null ? "—" : (fine ? eurFine : eur).format(n);
}

export function fmtSignedEur(v: string | number | null | undefined): string {
  const n = toNum(v);
  if (n === null) return "—";
  return (n > 0 ? "+" : "") + eur.format(n);
}

export function fmtBtc(v: string | number | null | undefined): string {
  const n = toNum(v);
  return n === null ? "—" : `${n.toFixed(8)} BTC`;
}

export function fmtPrice(v: string | number | null | undefined): string {
  const n = toNum(v);
  return n === null ? "—" : num.format(n);
}

export function fmtPct(v: string | number | null | undefined, digits = 2): string {
  const n = toNum(v);
  return n === null ? "—" : `${n.toFixed(digits)}%`;
}

export function fmtProb(v: string | number | null | undefined): string {
  const n = toNum(v);
  return n === null ? "—" : `${Math.round(n * 100)}%`;
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `преди ${s} с`;
  if (s < 3600) return `преди ${Math.round(s / 60)} мин`;
  if (s < 86400) return `преди ${(s / 3600).toFixed(1)} ч`;
  return `преди ${Math.round(s / 86400)} дни`;
}

const dt = new Intl.DateTimeFormat("bg-BG", { dateStyle: "short", timeStyle: "medium" });
export function fmtTime(iso: string | null | undefined): string {
  return iso ? dt.format(new Date(iso)) : "—";
}

export const ACTION_LABEL: Record<string, string> = {
  BUY: "КУПИ",
  SELL: "ПРОДАЙ",
  HOLD: "ЗАДРЪЖ",
  NONE: "НЯМА СИГНАЛ",
};

export const INTENT_LABEL: Record<string, string> = {
  BUY: "покупка по сигнал",
  SELL: "продажба по сигнал",
  EXIT_TP: "изход: цел",
  EXIT_SL: "изход: лимит на загубата",
  EXIT_TIME: "изход: изтекъл срок",
  MANUAL_SELL: "ръчна продажба",
};

export const STATUS_LABEL: Record<string, string> = {
  new: "нова",
  open: "отворена",
  partially_filled: "частично изпълнена",
  filled: "изпълнена",
  cancelled: "отменена",
  rejected: "отхвърлена",
  unknown: "неизвестен статус",
};

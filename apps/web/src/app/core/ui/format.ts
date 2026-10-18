/** Formatting helpers shared by the dashboard components (device-local time). */

const pad = (n: number) => String(n).padStart(2, '0');

export function isoToLocalInput(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function localInputToIso(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}

export function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

export function formatDue(iso: string): { label: string; cls: string } {
  const d = new Date(iso);
  const now = new Date();
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const label = sameDay(d, now)
    ? `Today ${timeFmt.format(d)}`
    : sameDay(d, tomorrow)
      ? `Tomorrow ${timeFmt.format(d)}`
      : dateTimeFmt.format(d);
  const cls = d < now ? 'overdue' : sameDay(d, now) ? 'today' : '';
  return { label, cls };
}

export function formatEventTime(startIso: string, endIso: string): string {
  const s = new Date(startIso),
    e = new Date(endIso);
  return sameDay(s, e)
    ? `${timeFmt.format(s)} – ${timeFmt.format(e)}`
    : `${dateTimeFmt.format(s)} – ${dateTimeFmt.format(e)}`;
}

export function isCoarsePointer(): boolean {
  return !!window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
}

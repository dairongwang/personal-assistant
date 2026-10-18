/** Semantic-version comparison shared by the update policy (mirrors services/account/versioning.py). */
const RE = /^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?([0-9A-Za-z][0-9A-Za-z.-]*))?\s*$/;

type Parsed = [number, number, number, number, (string | number)[]];

export function parseVersion(text: string | null | undefined): Parsed {
  const m = RE.exec(text ?? '');
  if (!m) return [-1, -1, -1, 0, []];
  const pre = m[4]
    ? m[4].split(/[.-]/).map((p) => (/^\d+$/.test(p) ? Number(p) : p.toLowerCase()))
    : [];
  return [Number(m[1]), Number(m[2] ?? 0), Number(m[3] ?? 0), m[4] ? 0 : 1, pre];
}

/** -1 if a < b, 0 if equal, 1 if a > b (pre-releases sort before their release). */
export function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a),
    pb = parseVersion(b);
  for (let i = 0; i < 4; i++) {
    if (pa[i] !== pb[i]) return (pa[i] as number) < (pb[i] as number) ? -1 : 1;
  }
  const n = Math.max(pa[4].length, pb[4].length);
  for (let i = 0; i < n; i++) {
    const x = pa[4][i],
      y = pb[4][i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (x === y) continue;
    if (typeof x === 'number' && typeof y === 'number') return x < y ? -1 : 1;
    if (typeof x === 'number') return -1;
    if (typeof y === 'number') return 1;
    return x < y ? -1 : 1;
  }
  return 0;
}

export const isNewer = (candidate: string, current: string) =>
  compareVersions(candidate, current) > 0;

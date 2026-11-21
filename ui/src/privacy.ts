// Shape of ui/src/generated/privacy.json (written by scripts/build-privacy.mjs from docs/MEMORY.md).

export interface HandledForm {
  form: string;
  formParts?: { code: boolean; text: string }[];
  examples: string[];
  test: string;
  minimumLength: string | null;
  source: string; // the test file's short name ("round3", "stage9", ...)
}

export interface KnownGap {
  text: string;
  children: string[];
}

export interface PrivacyData {
  generatedFrom: { docs: string; readme: string; docsCommit: string | null };
  verifiedMeans: string;
  handledForms: HandledForm[];
  knownGaps: KnownGap[];
  testCounts: { line: string; testsText: string; tests: number; passed: number; xfailed: number } | null;
}

export function matchesSearch(row: HandledForm, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return [row.form, row.test, row.minimumLength ?? "", ...row.examples].some((t) => t.toLowerCase().includes(needle));
}

// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PrivacyPage } from "../components/PrivacyPage";
import type { PrivacyData } from "../privacy";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const EVIL = "<img src=x onerror=alert(1)>";
const FIXTURE: PrivacyData = {
  generatedFrom: { docs: "docs/MEMORY.md", readme: "README.md", docsCommit: "abc1234" },
  verifiedMeans: "Verified means: these exact examples, as a fixture.",
  handledForms: [
    { form: "label, `is`, value", examples: ["the password is swordfish"], test: "round3 test_plain", minimumLength: "none", source: "round3" },
    { form: "URL userinfo", examples: ["postgres://admin:sycamore99@db.example.com:5432/prod", EVIL], test: "stage9 test_url", minimumLength: "none", source: "stage9" },
    { form: "glpat token", examples: ["Trail Tracker CI uses glpat-AbCdEfGhIjKlMnOpQrSt12 for pushes"], test: "stage10 test_tok", minimumLength: "glpat body 20+", source: "stage10" },
  ],
  knownGaps: [{ text: "A value split across two lines.", children: ["one", "two"] }, { text: `Gap with ${EVIL}`, children: [] }],
  testCounts: { line: "The suite has 1,217 tests", testsText: "1,217", tests: 1217, passed: 1181, xfailed: 36 },
};

let container: HTMLDivElement;
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); });

async function render(data: PrivacyData | null) {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<PrivacyPage data={data} />); });
  await act(settle);
}

describe("PrivacyPage", () => {
  it("renders every handled-forms row, the gaps and the definition, as text", async () => {
    stubApi({});
    await render(FIXTURE);
    expect(container.querySelectorAll(".form-row")).toHaveLength(3);
    expect(container.textContent).toContain(FIXTURE.verifiedMeans);
    expect(container.querySelectorAll(".gap-item")).toHaveLength(2);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelectorAll("a[href^='postgres'], a[href^='http']")).toHaveLength(0);
    expect(container.textContent).toContain("3 tested forms");
    expect(container.textContent).toContain("abc1234");
  });

  it("shows exact examples in monospace when a row is opened", async () => {
    stubApi({});
    await render(FIXTURE);
    const row = container.querySelectorAll<HTMLElement>(".form-row")[1];
    await act(async () => row.querySelector<HTMLButtonElement>("button")!.click());
    const examples = [...row.querySelectorAll("code.example")].map((c) => c.textContent);
    expect(examples).toEqual(["postgres://admin:sycamore99@db.example.com:5432/prod", EVIL]);
  });

  it("filters rows by search text and by test source", async () => {
    stubApi({});
    await render(FIXTURE);
    const input = container.querySelector<HTMLInputElement>("input[type=search]")!;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "glpat");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect([...container.querySelectorAll(".form-row .form-name")].map((e) => e.textContent)).toEqual(["glpat token"]);
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector<HTMLButtonElement>("button.chip[data-source='stage9']")!.click());
    expect([...container.querySelectorAll(".form-row .form-name")].map((e) => e.textContent)).toEqual(["URL userinfo"]);
  });

  it("degrades to an empty state with no digits when the generated data is missing", async () => {
    stubApi({});
    await render(null);
    expect(container.textContent).toContain("not been generated");
    const text = container.textContent ?? "";
    expect(text.replace(/[0-9]/g, "")).toBe(text);
  });
});

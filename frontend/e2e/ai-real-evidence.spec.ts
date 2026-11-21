import { writeFileSync } from "node:fs";
import { type Page, expect, test } from "@playwright/test";
import { AsUser, roomUrl, seed, signedIn } from "./helpers";

/**
 * The review follow-up (docs/ai.md section 24) through the normal interface against the REAL providers: what a person sees
 * when the AI summarizes the open document, rewrites a qualified sentence, cites a summary, previews a proposal that cites a
 * source, draws a diagram of steps whose order the source never states, and what the permission and source filters still allow.
 * Run with E2E_AI=real (env.ts loads the keys from the gitignored .env, starts no stand-in). Skipped otherwise.
 *
 * Assertions are about properties that must hold whatever words the model chooses (a rubric of facts, a visible flag, a label, a
 * link), not about wording. Identifiers and the checks' outcomes are written to test-results/real-browser-evidence.json; the
 * texts of answers are not stored there.
 */
test.skip(process.env.E2E_AI !== "real", "real-provider spec: run with E2E_AI=real");

const surface = (page: Page) => page.getByTestId("editor").locator(".tf-editor");
const status = (page: Page) => page.getByTestId("sync-status");

const ATLAS = [
  "Project Atlas brief",
  "Atlas moves the order system from the old data centre to the cloud by 30 June.",
  "The migration budget is 120,000 euros, approved by the steering group.",
  "The main risk is the payment gateway, which has no staging environment.",
  "Maria Keller owns the cutover plan; Tom Vogel owns the rollback plan.",
];
// the rubric of the closing summary: points of the document (any one form of each is enough)
const RUBRIC: Record<string, RegExp> = {
  what: /order system|cloud|migrat/i,
  deadline: /30 june|june 30|end of june/i,
  budget: /120,?000/i,
  risk: /payment gateway|staging/i,
  owners: /maria|keller|tom|vogel|cutover|rollback/i,
};
const PLACEHOLDER = /preceding discussion|key points (presented|discussed)|content (was|is) not (provided|available)/i;

const RELEASE_MD = "# Release process\n\nEvery release goes through three gates.\n\n## Build\n\n- compile the sources\n- run the unit tests\n\n## Deploy\n\n| Stage | Owner |\n|-------|-------|\n| Build | Alice |\n| Deploy | Bob |\n\nThe deploy step restarts the workers one at a time.\n";
const ONBOARDING_MD = "# Onboarding\n\nNew hires complete several onboarding items.\n\n## Orientation\n\n- meet the team\n- tour the warehouse\n\n## Payroll\n\nPayroll setup is handled by Dana.\n\n## Equipment\n\nThe IT desk issues laptops and badges. Equipment requests are approved by Sam.\n";

async function openAsk(page: Page, output: string, target: string | null, text: string) {
  await page.getByRole("button", { name: "Ask AI" }).click();
  await page.getByRole("button", { name: output, exact: true }).click();
  if (target) await page.getByRole("button", { name: target, exact: true }).click();
  await page.getByRole("textbox", { name: "Message" }).fill(text);
}

async function chooseOnly(page: Page, name: RegExp) {
  await page.getByText(/Files \((\d+) chosen\)/).click();
  for (const box of await page.getByRole("checkbox", { name: /\.md/ }).all()) {
    const label = (await box.getAttribute("aria-label")) ?? (await box.evaluate((el) => el.parentElement?.textContent ?? ""));
    if (name.test(label)) await box.check();
    else if (await box.isChecked()) await box.uncheck();
  }
}

test("real inference: closing summary, qualified rewrite, derived citation, source-backed proposal, diagram assumptions, permissions", async ({ browser }) => {
  test.setTimeout(900_000);
  const evidence: Record<string, unknown> = { started: new Date().toISOString(), requests: [] as { request_id: string }[] };

  const owner = seed("Olivia Owner");
  const asOwner = await AsUser.create(owner);
  const roomId = await asOwner.createRoom("Review follow-up room");
  await asOwner.createDocument(roomId, "Atlas");
  await asOwner.send("POST", "/ai/consent", { notice_version: 1 });
  const viewer = seed("Vera Viewer", { roomId, role: "viewer" });
  const asViewer = await AsUser.create(viewer);
  const outsider = seed("Olga Outsider");
  const asOutsider = await AsUser.create(outsider);

  const a = await signedIn(browser, owner);
  const b = await signedIn(browser, viewer);
  for (const [p, expected] of [[a.page, "Synced"], [b.page, "Read-only"]] as const) {
    await p.goto(roomUrl(roomId));
    await expect(surface(p)).toBeVisible();
    await expect(status(p)).toHaveText(expected);
  }
  let lastBody: Record<string, unknown> = {};
  a.page.on("request", (req) => {
    if (req.method() === "POST" && /\/rooms\/[^/]+\/ai-requests$/.test(req.url())) lastBody = req.postDataJSON() as Record<string, unknown>;
  });
  a.page.on("response", async (res) => {
    try {
      if (res.request().method() === "POST" && /\/rooms\/[^/]+\/ai-requests$/.test(res.url()) && res.status() === 201) {
        const body = (await res.json()) as { id: string };
        (evidence.requests as { request_id: string; instruction?: unknown; task_hint?: unknown }[]).push({ request_id: body.id, instruction: undefined, task_hint: lastBody.task_hint });
      }
    } catch {
      /* a response without a body */
    }
  });
  const lastRequestId = () => (evidence.requests as { request_id: string }[]).at(-1)!.request_id;

  // ---- 1. summarize a real captured document with several substantive points -----------------------------------------------------
  await surface(a.page).click();
  for (const [i, line] of ATLAS.entries()) {
    await a.page.keyboard.type(line);
    if (i < ATLAS.length - 1) await a.page.keyboard.press("Enter");
  }
  await expect(status(a.page)).toHaveText("Synced");
  await openAsk(a.page, "Propose document edit", "End of document", "Add a closing summary paragraph.");
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  const cards = a.page.getByRole("region", { name: "AI proposal" });
  const summary = cards.last();
  await expect(summary).toContainText("Proposed edit", { timeout: 240_000 });
  await summary.getByTestId("preview-toggle").click();
  const summaryText = (await summary.innerText()).replace(/\s+/g, " ");
  const found = Object.entries(RUBRIC).filter(([, re]) => re.test(summaryText.split("Before")[0]!.length > 0 ? summaryText : summaryText)).map(([k]) => k);
  const insertedText = (await summary.getByLabel("After").allInnerTexts()).join(" ");
  const insertedFound = Object.entries(RUBRIC).filter(([, re]) => re.test(insertedText)).map(([k]) => k);
  expect(insertedFound.length, `the inserted text should carry at least 4 of the document's 5 points, found: ${insertedFound.join(", ")}`).toBeGreaterThanOrEqual(4);
  expect(PLACEHOLDER.test(insertedText)).toBe(false);
  const summaryId = lastRequestId();
  const summaryResult = await asOwner.get(`/rooms/${roomId}/ai-requests/${summaryId}/result`);
  expect(summaryResult.coverage.document.mode).toBe("target_plus_context");
  expect(summaryResult.coverage.document.context.complete).toBe(true);
  expect(summaryResult.coverage.document.context.blocks_total).toBe(ATLAS.length);
  evidence.closing_summary = { request_id: summaryId, rubric_found: insertedFound, placeholder: false, context: summaryResult.coverage.document.context, coverage_text_seen: found.length > 0 };
  // apply it: the closing summary lands in the document, in both browsers
  await summary.getByRole("button", { name: "Apply" }).click();
  await a.page.getByRole("button", { name: "Apply now" }).click();
  await expect(summary).toContainText("Applied", { timeout: 60_000 });
  await expect(surface(b.page)).toContainText(/Atlas/, { timeout: 30_000 });

  // ---- 2. rewrite a qualified sentence: the meaning is kept, or the reviewer is told before applying -------------------------------
  await surface(a.page).click();
  await a.page.keyboard.press("Control+End");
  await a.page.keyboard.press("Enter");
  await a.page.keyboard.type("The launch is scheduled for Friday at noon.");
  await expect(status(a.page)).toHaveText("Synced");
  await a.page.getByText("The launch is scheduled for Friday at noon.").click({ clickCount: 3 });
  await openAsk(a.page, "Propose document edit", "Selected blocks", "Make this sentence shorter.");
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  await expect(cards).toHaveCount(2, { timeout: 240_000 });
  const rewrite = cards.last();
  await expect(rewrite).toContainText("Proposed edit", { timeout: 240_000 });
  await rewrite.getByTestId("preview-toggle").click();
  const afterText = (await rewrite.getByLabel("After").first().innerText()).replace(/\s+/g, " ");
  const flagged = await rewrite.getByRole("note", { name: "Check these before applying" }).count();
  const kept = /scheduled|planned|slated/i.test(afterText);
  expect(kept || flagged > 0, `a rewrite that drops "scheduled" must carry a review flag (after: "${afterText}")`).toBe(true);
  evidence.qualified_rewrite = { request_id: lastRequestId(), qualification_kept: kept, review_flag_shown: flagged > 0 };

  // ---- 3. a summary's citation opens to the ORIGINAL text, labelled as derived ---------------------------------------------------------
  await a.page.getByLabel("Upload a file").setInputFiles({ name: "release.md", mimeType: "text/markdown", buffer: Buffer.from(RELEASE_MD) });
  await expect(a.page.getByRole("region", { name: "Files" })).toContainText("release.md");
  await a.page.getByLabel("Upload a file").setInputFiles({ name: "onboarding.md", mimeType: "text/markdown", buffer: Buffer.from(ONBOARDING_MD) });
  await expect(a.page.getByRole("region", { name: "Files" })).toContainText("onboarding.md");
  await a.page.getByRole("textbox", { name: "Message" }).fill("@");
  await a.page.getByRole("option", { name: /@AI/ }).click();
  await chooseOnly(a.page, /release\.md/);
  await a.page.getByRole("group", { name: "Task" }).getByRole("button", { name: "Summarize" }).click();
  await a.page.getByRole("textbox", { name: "Message" }).fill("Summarize the release process.");
  await a.page.getByRole("button", { name: "Send to AI" }).click();
  const answers = a.page.getByRole("region", { name: "AI answer" });
  const first = answers.last();
  await expect(first).toContainText(/gates?/i, { timeout: 240_000 });
  const summaryAnswerId = lastRequestId();
  await first.getByRole("button", { name: /Citation 1/ }).click();
  const cite = first.getByRole("complementary", { name: /Citation 1/ });
  await expect(cite).toContainText("Derived text.");
  await expect(cite.getByLabel("Model-written summary")).toBeVisible();
  const original = cite.getByLabel("Original text");
  await expect(original).toBeVisible({ timeout: 30_000 });
  await expect(original).toContainText(/Original text this summary was made from/);
  await expect(original).toContainText(/Release process|compile the sources|restarts the workers|three gates/i);
  const link = original.getByRole("link", { name: /Open the original file/ }).first();
  const href = (await link.getAttribute("href"))!;
  expect(href).toMatch(/\/sources\/[0-9a-f-]+\/versions\/[0-9a-f-]+\/download/);
  const download = await a.page.request.get(href);
  expect(download.status()).toBe(200);
  expect(await download.text()).toContain("Release process");
  const derived = await asOwner.get(`/rooms/${roomId}/ai-requests/${summaryAnswerId}/result`);
  const derivedCites = derived.citations.filter((c: { derived: boolean }) => c.derived);
  expect(derivedCites.length).toBeGreaterThan(0);
  expect(derivedCites.every((c: { provenance: string }) => c.provenance === "complete")).toBe(true);
  evidence.derived_citation = { request_id: summaryAnswerId, derived_citations: derivedCites.length, origin_shown: true, original_file_link_status: download.status() };

  // ---- 4. a proposal that cites a source: the preview resolves the reference to the original passage --------------------------------
  await openAsk(a.page, "Propose document edit", "End of document", "Add a build and deploy section using the release notes.");
  await chooseOnly(a.page, /release\.md/); // opens the Files list once and ticks only release.md
  await expect(a.page.getByRole("checkbox", { name: /release\.md/ })).toBeChecked();
  await a.page.getByRole("button", { name: "Ask for an edit" }).click();
  await expect(cards).toHaveCount(3, { timeout: 240_000 });
  const sourced = cards.last();
  await expect(sourced).toContainText("Proposed edit", { timeout: 240_000 });
  await sourced.getByTestId("preview-toggle").click();
  const mark = sourced.getByRole("button", { name: /Citation \d/ }).first();
  await expect(mark).toBeVisible();
  await mark.click();
  const proposalCite = sourced.getByRole("complementary", { name: /Citation \d/ });
  await expect(proposalCite).toContainText("release.md");
  await expect(proposalCite.getByLabel("Original text").or(proposalCite.locator("blockquote")).first()).toContainText(/compile|restart|Deploy|Build|gates/i, { timeout: 30_000 });
  evidence.proposal_citation = { request_id: lastRequestId(), opened_source_text: true };

  // ---- 5. a diagram of steps whose ORDER the source never states -------------------------------------------------------------------
  await a.page.getByRole("textbox", { name: "Message" }).fill("@");
  await a.page.getByRole("option", { name: /@AI/ }).click();
  await chooseOnly(a.page, /onboarding\.md/);
  await a.page.getByRole("group", { name: "Task" }).getByRole("button", { name: "Auto" }).click();
  await a.page.getByRole("textbox", { name: "Message" }).fill("Draw a diagram of the onboarding steps.");
  await a.page.getByRole("button", { name: "Send to AI" }).click();
  await expect(answers).toHaveCount(2, { timeout: 240_000 });
  const diagramCard = answers.last();
  await expect(diagramCard).toContainText(/orientation|payroll|equipment/i, { timeout: 240_000 });
  const diagramId = lastRequestId();
  const diagramResult = await asOwner.get(`/rooms/${roomId}/ai-requests/${diagramId}/result`);
  const drawn = diagramResult.answer.blocks.filter((x: { type: string }) => x.type === "diagram");
  expect(drawn.length, "a diagram was asked for").toBeGreaterThan(0);
  const arrows = drawn.some((x: { mermaid: string }) => /-->|==>|-\.->/.test(x.mermaid));
  const listed = drawn.flatMap((x: { assumptions?: string[] }) => x.assumptions ?? []);
  const gaps = diagramResult.answer.evidence_gaps as string[];
  const said = listed.length > 0 || gaps.some((g) => /order|sequence|assum|not (stated|specified|say)/i.test(g));
  if (arrows) {
    expect(said, "a diagram that draws an order the source never states must say it is assumed").toBe(true);
    if (listed.length > 0) await expect(diagramCard.getByRole("note", { name: "Assumptions in this diagram" })).toContainText(listed[0].slice(0, 30));
  }
  evidence.diagram = { request_id: diagramId, diagrams: drawn.length, draws_order: arrows, assumptions_listed: listed.length, gaps_mention_order: gaps.some((g) => /order|sequence/i.test(g)), grouped_instead: !arrows };

  // ---- 6. the source filter and the permissions still hold ------------------------------------------------------------------------------
  const cited = (diagramResult.citations as { source_name: string }[]).map((c) => c.source_name);
  expect(cited.every((n) => n === "onboarding.md"), `only the chosen file may be cited, got ${cited.join(", ")}`).toBe(true);
  const viewerAsk = await asViewer.send("POST", `/rooms/${roomId}/ai-requests`, { ...lastBody, client_request_id: crypto.randomUUID() });
  expect(viewerAsk.status).toBe(403);
  const outsiderOrigin = await asOutsider.getStatus(`/rooms/${roomId}/ai-requests/${summaryAnswerId}/citations/1/origin`);
  expect([403, 404]).toContain(outsiderOrigin.status);
  const outsiderResult = await asOutsider.getStatus(`/rooms/${roomId}/ai-requests/${summaryAnswerId}/result`);
  expect([403, 404]).toContain(outsiderResult.status);
  const foreignChunk = await asOwner.getStatus(`/rooms/${roomId}/ai-requests/${diagramId}/citations/999/origin`);
  expect(foreignChunk.status).toBe(404);
  evidence.permissions = { chosen_source_only: true, viewer_ai_request_status: viewerAsk.status, outsider_origin_status: outsiderOrigin.status, outsider_result_status: outsiderResult.status };

  evidence.finished = new Date().toISOString();
  evidence.room_id = roomId;
  writeFileSync("test-results/real-browser-evidence.json", JSON.stringify(evidence, null, 2));
  await Promise.all([a.context.close(), b.context.close()]);
});

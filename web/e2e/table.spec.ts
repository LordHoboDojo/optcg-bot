import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { answerDialogs, expect, startGame, test } from "./fixtures";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

const over = (page: Page) => page.locator("#mbox h2", { hasText: /^(Victory|Defeat|Draw)$/ });

/** Answer the table's decisions until the game ends: always end the turn, and otherwise take the
 * first option offered (no block, no counter, keep the hand). Returns how many answers it gave. */
async function playToTheEnd(page: Page, limit = 600): Promise<number> {
  let answers = 0;
  while (answers < limit) {
    if (await over(page).isVisible()) return answers;
    const modal = page.locator("#modal:visible #mbox [data-opt]");
    if (await modal.count()) {
      await modal.first().click();
      answers++;
      continue;
    }
    for (const id of ["#skipBot", "#toNow"]) {
      if (await page.locator(id).isVisible()) await page.locator(id).click();
    }
    const offered = page.locator("#prompt [data-opt]:visible");
    if (await page.locator("#endturn").isVisible()) {
      await page.locator("#endturn").click();
      answers++;
    } else if (await offered.count()) {
      // a question with its answers on the prompt (go first or second?)
      await offered.first().click();
      answers++;
    } else if (await page.locator("#allopts").isVisible()) {
      if (await page.locator("#optlist").isHidden()) await page.locator("#allopts").click();
      await page.locator("#optlist [data-opt]").first().click({ timeout: 5000 });
      answers++;
    } else {
      await page.waitForTimeout(50); // the bot is playing, or the answer is on its way
    }
  }
  throw new Error(`no end of game after ${limit} answers`);
}

test.describe("game table", () => {
  test("plays a whole game against greedy through the page", async ({ page }) => {
    test.setTimeout(240_000);
    await startGame(page, "greedy", 0);
    const answers = await playToTheEnd(page);
    expect(answers).toBeGreaterThan(0);
    // never attacking, we lose
    await expect(over(page)).toHaveText("Defeat");
    // the server agrees the game is over
    const st = await (await page.request.get("/api/state?from=0")).json();
    expect(st.over).toBe(true);
    expect(st.winner).toBe(1);
  });

  test("conceding ends the game", async ({ page }) => {
    page.on("dialog", (d) => d.accept());
    await startGame(page, "random", 0);
    await answerDialogs(page);
    await expect(page.locator("#concede")).toBeVisible();
    await page.locator("#concede").click();
    await expect(page.locator("#prompt")).toContainText("Game over");
    await expect(page.locator("#concede")).toBeHidden();
    const st = await (await page.request.get("/api/state?from=0")).json();
    expect(st.over).toBe(true);
  });

  test("new game starts a fresh game", async ({ page }) => {
    page.on("dialog", (d) => d.accept());
    await startGame(page, "random", 0);
    await answerDialogs(page);
    const before = await (await page.request.get("/api/state?from=0")).json();
    await page.locator("#newgame").click();
    await page.waitForLoadState("load");
    await expect(page.locator("#stage")).toBeVisible();
    const after = await (await page.request.get("/api/state?from=0")).json();
    expect(after.over).toBe(false);
    expect(after.frames.length).toBeLessThanOrEqual(before.frames.length + 1);
  });

  test("the timeline steps back and returns to the game", async ({ page }) => {
    await startGame(page, "random", 0);
    // play a few turns first so there is something to look back at
    for (let k = 0; k < 6; k++) {
      if (await page.locator("#skipBot").isVisible()) await page.locator("#skipBot").click();
      const modal = page.locator("#modal:visible #mbox [data-opt]");
      if (await modal.count()) await modal.first().click();
      else if (await page.locator("#endturn").isVisible()) await page.locator("#endturn").click();
      else await page.waitForTimeout(100);
    }
    await page.locator("#hBack").click();
    await page.locator("#hBack").click();
    await expect(page.locator("#prompt")).toContainText("History");
    await page.locator("#toNow").click();
    await expect(page.locator("#prompt")).not.toContainText("History");
  });
});

test.describe("replay file", () => {
  test("a recorded game plays back from start to finish", async ({ page }) => {
    const out = join(mkdtempSync(join(tmpdir(), "optcg-replay-")), "replay.html");
    execFileSync(join(ROOT, ".venv/bin/python"), ["-m", "optcg.replay", "--p0", "greedy", "--p1", "greedy", "--seed", "3", "--out", out],
      { cwd: ROOT, stdio: "pipe" });
    await page.goto(`file://${out}`);
    await expect(page).toHaveTitle("OPTCG · Replay");
    await expect(page.locator("#prompt")).not.toBeEmpty();
    const bar = page.locator("#hBar");
    const last = Number(await bar.getAttribute("max"));
    expect(last).toBeGreaterThan(10);
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(bar).toHaveValue("2");
    await page.keyboard.press("ArrowLeft");
    await expect(bar).toHaveValue("1");
    await page.keyboard.press("End");
    await expect(bar).toHaveValue(String(last));
    await expect(page.locator("#prompt")).toContainText("Game over");
  });
});

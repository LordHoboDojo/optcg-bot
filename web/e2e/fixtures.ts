import { expect, test as base, type Page } from "@playwright/test";

/** Every test fails on a JavaScript error in the page (an uncaught exception or console.error).
 * Failed requests aren't counted: an unknown address is meant to come back 404. */
export const test = base.extend<{ errors: string[] }>({
  errors: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });
    await use(errors);
    expect(errors, "JavaScript errors in the page").toEqual([]);
  }, { auto: true }],
});
export { expect };

export const MY_DECK = "rocks";
export const BOT_DECK = "(Yellow) Charlotte Linlin";

/** Sign in with the dev login (the test server allows it). */
export async function signIn(page: Page, name = "alice") {
  await page.goto(`/login/dev?name=${encodeURIComponent(name)}`);
}

/** Start a browser game through the API and open the table. */
export async function startGame(page: Page, bot = "greedy", seat = 0) {
  const r = await page.request.post("/api/games", { data: { deck: MY_DECK, bot_deck: BOT_DECK, bot, seat } });
  expect(r.ok(), await r.text()).toBeTruthy();
  await page.goto("/game");
  await expect(page.locator("#stage")).toBeVisible();
}

/** Answer whatever the table is asking in its dialog (the mulligan, a block, a counter...) with
 * the first option, until it asks nothing there. */
export async function answerDialogs(page: Page) {
  const option = page.locator("#modal:visible #mbox [data-opt]");
  for (let k = 0; k < 20 && await option.first().isVisible({ timeout: 1000 }).catch(() => false); k++) {
    await option.first().click();
    await page.waitForTimeout(100);
  }
}

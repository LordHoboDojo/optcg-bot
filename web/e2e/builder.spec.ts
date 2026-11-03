import { expect, MY_DECK, test } from "./fixtures";

test.describe("deck builder", () => {
  test("builds a deck card by card and keeps it in the browser", async ({ page }) => {
    await page.goto("/builder");
    await expect(page.getByRole("heading", { level: 1, name: "Deck builder" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Leaders" })).toHaveAttribute("aria-selected", "true");
    const search = page.getByRole("searchbox", { name: "Search cards" });
    await search.fill("OP17-039");
    await page.locator(".tile", { hasText: "OP17-039" }).getByRole("button", { name: "Choose" }).click();
    await expect(page.getByRole("textbox", { name: "Name" })).toHaveValue("Rocks.D.Xebec");
    await expect(page.getByRole("tab", { name: "All cards" })).toHaveAttribute("aria-selected", "true");

    await search.fill("OP08-051");
    const add = page.locator(".pool").getByRole("button", { name: "Add a Buckin" });
    for (let k = 0; k < 4; k++) await add.click();
    await expect(add).toBeDisabled();  // at most 4 copies
    await expect(page.locator(".summary")).toContainText("4 / 50 cards");
    await expect(page.getByText("The deck has 4 cards; it needs 50.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Play this deck" })).toBeDisabled();

    // a red card can't join a blue Leader's deck, and the Leader's colors filter hides it
    await search.fill("OP01-016");
    await expect(page.getByText("No cards match.")).toBeVisible();
    await page.getByRole("button", { name: "Leader's colors" }).click();
    await expect(page.locator(".pool").getByRole("button", { name: "Add a Nami" })).toBeDisabled();

    await page.reload();
    await expect(page.getByRole("textbox", { name: "Name" })).toHaveValue("Rocks.D.Xebec");
    await expect(page.locator(".summary")).toContainText("4 / 50 cards");
    await page.locator(".decklist").getByRole("button", { name: "Remove a Buckin" }).click();
    await expect(page.locator(".summary")).toContainText("3 / 50 cards");
  });

  test("reads a pasted list and says what's wrong with it", async ({ page }) => {
    await page.goto("/builder");
    await page.getByRole("button", { name: "Copy or paste a list" }).click();
    const list = page.getByRole("textbox", { name: "Decklist" });
    await list.fill("1 OP17-039 Rocks.D.Xebec\n1 OP01-001 Roronoa Zoro\n4 OP08-051 Buckin");
    await page.getByRole("button", { name: "Make a deck from this list" }).click();
    await expect(page.getByText("A list needs exactly one Leader; this one has 2.")).toBeVisible();
    await list.fill("# Mixed up\n1xOP17-039\n4xOP08-051\n4xOP01-016");
    await page.getByRole("button", { name: "Make a deck from this list" }).click();
    await expect(page.getByRole("textbox", { name: "Name" })).toHaveValue("Mixed up");
    await expect(page.getByText("Nami (OP01-016) shares no color with your Leader.")).toBeVisible();
  });

  test("copies a site deck and plays it against a bot", async ({ page }) => {
    await page.goto("/builder");
    await page.getByRole("button", { name: "Copy or paste a list" }).click();
    await page.getByRole("combobox", { name: "Start from a site deck" }).selectOption(MY_DECK);
    await expect(page.getByRole("textbox", { name: "Name" })).toHaveValue(`${MY_DECK} (copy)`);
    await expect(page.getByText("Ready to play.")).toBeVisible();
    await page.getByRole("button", { name: "Play this deck" }).click();

    await expect(page).toHaveURL(/\/play\?deck=built:/);
    await expect(page.locator(".summary")).toContainText(`${MY_DECK} (copy)`);
    await expect(page.locator(".decks .deck", { hasText: `${MY_DECK} (copy)` })).toContainText("Yours");
    await page.getByRole("button", { name: "Start game" }).click();
    await expect(page).toHaveURL(/\/game$/);
    await expect(page.locator("#stage")).toBeVisible();
  });

  test("an unfinished deck can't start a game", async ({ page }) => {
    await page.goto("/builder");
    await page.getByRole("searchbox", { name: "Search cards" }).fill("OP17-039");
    await page.getByRole("button", { name: "Choose", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Name" })).toHaveValue("Rocks.D.Xebec");
    await page.goto("/play");
    await page.locator(".decks .deck", { hasText: "Rocks.D.Xebec" }).filter({ hasText: "Yours" }).click();
    await expect(page.getByText("Your deck has 0 of 50 cards.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Start game" })).toBeDisabled();
  });
});

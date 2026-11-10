import { expect, signIn, test } from "./fixtures";

test.describe("pages", () => {
  test("the home page links to the rest of the site", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Play against bots");
    const nav = page.getByRole("navigation", { name: "Main" });
    for (const name of ["Play", "Analysis", "Your bots", "Bot API"]) {
      await expect(nav.getByRole("link", { name })).toBeVisible();
    }
  });

  test("moving between pages never leaves a blank page", async ({ page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Main" });
    for (const [link, heading] of [["Play", "New game"], ["Analysis", "Analysis"], ["Your bots", "Your bots"],
      ["Bot API", null], ["Play", "New game"]] as const) {
      await nav.getByRole("link", { name: link }).click();
      if (heading) await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      else await expect(page.locator("main")).not.toBeEmpty();
      await expect(page.getByText("Something went wrong on this page")).toHaveCount(0);
    }
  });

  test("an unknown address shows the not-found page", async ({ page }) => {
    await page.goto("/no/such/page");
    await expect(page.getByText("That page doesn't exist")).toBeVisible();
  });

  test("the bot API docs render", async ({ page }) => {
    await page.goto("/docs/bot-api");
    await expect(page.locator("main h1, main h2").first()).toBeVisible();
    await expect(page.getByText("Couldn't load the docs.")).toHaveCount(0);
  });
});

test.describe("play page", () => {
  test("lists the decks and filters them", async ({ page }) => {
    await page.goto("/play");
    await expect(page.getByRole("heading", { name: "New game" })).toBeVisible();
    const decks = page.locator(".decks .deck");
    await expect(decks).toHaveCount(2);
    // the engine implements every card, so every deck is marked Full
    await expect(decks.getByText("Full", { exact: true })).toHaveCount(2);
    await page.getByRole("searchbox", { name: "Search decks" }).fill("Linlin");
    await expect(decks).toHaveCount(1);
    await expect(decks.first()).toContainText("Charlotte Linlin");
    await page.getByRole("searchbox", { name: "Search decks" }).fill("nothing like this");
    await expect(page.getByText("No decks match.")).toBeVisible();
    await page.getByRole("searchbox", { name: "Search decks" }).fill("");
    await page.getByRole("button", { name: "Yellow", exact: true }).click();
    await expect(decks).toHaveCount(1);
    // every card is implemented, so "Fully implemented" keeps both
    await page.getByRole("button", { name: "Yellow", exact: true }).click();
    await page.getByRole("button", { name: "Fully implemented" }).click();
    await expect(decks).toHaveCount(2);
  });

  test("picks the decks and the opponent, and starts a game", async ({ page }) => {
    await page.goto("/play");
    await page.getByRole("tab", { name: "Bot's deck" }).click();
    await page.locator(".decks .deck", { hasText: "Charlotte Linlin" }).click();
    await expect(page.locator(".summary")).toContainText("(Yellow) Charlotte Linlin");
    await page.getByRole("radio", { name: /random/ }).click();
    await expect(page.getByRole("radio", { name: /random/ })).toHaveAttribute("aria-checked", "true");
    await page.getByRole("button", { name: "Start game" }).click();
    await expect(page).toHaveURL(/\/game$/);
    await expect(page.locator("#stage")).toBeVisible();
    await expect(page.locator("#prompt")).not.toBeEmpty();
  });

  test("with no game in progress, /game goes back to the play page", async ({ page }) => {
    await page.goto("/game");
    await expect(page).toHaveURL(/\/play$/);
  });
});

test.describe("signed out", () => {
  test("your bots and account ask you to sign in", async ({ page }) => {
    await page.goto("/bots");
    await expect(page.getByRole("heading", { name: "Sign in to register a bot" })).toBeVisible();
    await page.goto("/account");
    await expect(page.getByRole("heading", { name: "Sign in to see your account" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Sign in (dev)" })).toBeVisible();
  });

  test("the dev sign-in button signs you in", async ({ page }) => {
    await page.goto("/account");
    await page.getByRole("link", { name: "Sign in (dev)" }).click();
    await expect(page).toHaveURL(/\/signin$/);
    await page.getByRole("textbox", { name: "Name" }).fill("carol");
    await page.getByRole("button", { name: "Sign in (dev)" }).click();
    await expect(page).toHaveURL(/\/account$/);
    await expect(page.getByText("carol").first()).toBeVisible();
  });
});

test.describe("signed in", () => {
  test("shows who you are and lets you rename yourself", async ({ page }) => {
    await signIn(page, "alice");
    await page.goto("/account");
    await expect(page.getByRole("heading", { name: "alice" })).toBeVisible();
    await page.getByRole("button", { name: "Change display name" }).click();
    await page.getByRole("textbox", { name: "Display name" }).fill("Alice Q");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("heading", { name: "Alice Q" })).toBeVisible();
    await expect(page.locator(".topbar .who")).toContainText("Alice Q");
    await expect(page.getByText("No finished games yet")).toBeVisible();
  });

  test("registers a bot, makes and revokes an API key, and retires the bot", async ({ page }) => {
    page.on("dialog", (d) => d.accept());
    await signIn(page, "bob");
    await page.goto("/bots");
    await page.getByRole("textbox", { name: "Bot name" }).fill("e2e-bot");
    await page.getByRole("button", { name: "Register" }).click();
    const card = page.locator(".card", { has: page.getByRole("heading", { name: "e2e-bot" }) });
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "New API key" }).click();
    await expect(page.getByText(/[A-Za-z0-9_-]{24,}/).first()).toBeVisible();
    await page.getByRole("button", { name: "Done" }).click();
    await expect(card.getByRole("button", { name: "Revoke" })).toHaveCount(1);
    await card.getByRole("button", { name: "Revoke" }).click();
    await expect(card.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    await card.getByRole("button", { name: "Retire" }).click();
    await expect(page.getByRole("heading", { name: "e2e-bot" })).toHaveCount(0);
  });

  test("signing out goes back to the home page as a guest", async ({ page }) => {
    await signIn(page, "carol");
    await page.goto("/account");
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/$/);
    await page.goto("/account");
    await expect(page.getByRole("heading", { name: "Sign in to see your account" })).toBeVisible();
  });
});

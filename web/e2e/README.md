# Browser tests

End-to-end tests for the site's pages (`site.spec.ts`) and the game table (`table.spec.ts`), in
Chromium with [Playwright](https://playwright.dev). The config builds the pages and starts the real
server (`python -m optcg.server`) on port 8765 with test settings: dev sign-in, an in-memory
database, the `greedy` and `random` house bots and two decks. Any JavaScript error in a page fails
the test.

    cd web
    npm install && npx playwright install chromium   # once
    npm run test:e2e                                 # all of them
    npx playwright test table --headed               # one file, in a visible browser
    npx playwright show-report                       # after a failure: traces and screenshots

They need the project's `.venv` with the server's packages (`requirements-server.txt`) and the
Rust engine built (`engine-rs/README.md`). A server already running on port 8765 is reused.

from pathlib import Path
import sys

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_FILE = (
    PROJECT_ROOT
    / "playwright"
    / ".auth"
    / "facebook_state.json"
)

TEST_URL = (
    "https://www.facebook.com/groups/"
    "2415122391976246/permalink/3681329492022190/"
)


def main() -> None:
    """Test the saved Facebook authentication state."""
    if not AUTH_FILE.exists():
        print("Facebook authentication file was not found.")
        print(f"Expected path: {AUTH_FILE}")
        sys.exit(1)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
        )

        context = browser.new_context(
            storage_state=str(AUTH_FILE),
            viewport={
                "width": 1400,
                "height": 900,
            },
        )

        page = context.new_page()

        print("Opening the authorized Facebook post...")

        page.goto(
            TEST_URL,
            wait_until="domcontentloaded",
            timeout=120_000,
        )

        print(f"Page title: {page.title()}")
        print(f"Current URL: {page.url}")
        print()
        input(
            "Check that the Facebook post is visible, "
            "then press Enter to close the browser..."
        )

        browser.close()


if __name__ == "__main__":
    main()
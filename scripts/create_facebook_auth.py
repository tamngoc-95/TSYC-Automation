from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_DIRECTORY = PROJECT_ROOT / "playwright" / ".auth"
AUTH_FILE = AUTH_DIRECTORY / "facebook_state.json"

FACEBOOK_LOGIN_URL = "https://www.facebook.com/login/"


def main() -> None:
    """Open Facebook login and save the authenticated browser state."""
    AUTH_DIRECTORY.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
        )

        context = browser.new_context(
            viewport={
                "width": 1400,
                "height": 900,
            },
            locale="en-US",
        )

        page = context.new_page()

        print("Opening Facebook login page...")

        page.goto(
            FACEBOOK_LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=120_000,
        )

        print()
        print("A Chromium browser window has been opened.")
        print("Complete the cookie settings if Facebook displays them.")
        print()
        print("If the cookie page does not close:")
        print("1. Press Ctrl + L in the Chromium window.")
        print("2. Enter: https://www.facebook.com/login/")
        print("3. Press Enter.")
        print()
        print("Log in to Facebook manually.")
        print("Complete any security or verification steps.")
        print("Wait until the Facebook home page is fully visible.")
        print()

        input(
            "After Facebook login is complete, "
            "return to this CMD window and press Enter..."
        )

        current_url = page.url
        print()
        print(f"Current browser URL: {current_url}")

        login_indicators = (
            "login",
            "checkpoint",
            "recover",
        )

        while any(
            indicator in page.url.lower()
            for indicator in login_indicators
        ):
            print()
            print("Facebook login does not appear to be complete.")
            print("Return to the Chromium window and finish logging in.")
            print("Do not close the Chromium browser.")

            input(
                "After login is fully complete, "
                "return to CMD and press Enter again..."
            )

            print(f"Current browser URL: {page.url}")

        context.storage_state(
            path=str(AUTH_FILE)
        )

        print()
        print("Facebook authentication state saved successfully.")
        print(f"Authentication file: {AUTH_FILE}")

        browser.close()


if __name__ == "__main__":
    main()
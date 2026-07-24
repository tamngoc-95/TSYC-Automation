from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIRECTORY = PROJECT_ROOT / "playwright" / "facebook-profile"

FACEBOOK_URL = "https://www.facebook.com/"


def main() -> None:
    """Open Facebook with a persistent Google Chrome profile."""
    PROFILE_DIRECTORY.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIRECTORY),
            channel="chrome",
            headless=False,
            no_viewport=True,
            locale="de-DE",
        )

        page = context.pages[0] if context.pages else context.new_page()

        print("Opening Facebook with the persistent profile...")

        page.goto(
            FACEBOOK_URL,
            wait_until="domcontentloaded",
            timeout=120_000,
        )

        print()
        print("Complete login or verification in the Chrome window.")
        print("The browser profile is saved automatically.")
        print()

        input(
            "After Facebook is usable, return to CMD and press Enter..."
        )

        print(f"Current URL: {page.url}")
        print("Closing the browser and saving the profile.")

        context.close()


if __name__ == "__main__":
    main()
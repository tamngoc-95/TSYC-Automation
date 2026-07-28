import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth


PROJECT_ROOT = Path(__file__).resolve().parents[1]

TESTER_NAME = "woocommerce_connection_tester"
TESTER_VERSION = "1.0.0"


def get_required_environment_variable(
    variable_name: str,
) -> str:
    """Return a required environment variable."""
    value = os.getenv(
        variable_name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {variable_name}"
        )

    return value


def get_timeout_seconds() -> int:
    """Return the configured HTTP timeout."""
    raw_value = os.getenv(
        "WOOCOMMERCE_TIMEOUT_SECONDS",
        "30",
    ).strip()

    try:
        timeout_seconds = int(
            raw_value
        )

    except ValueError as error:
        raise RuntimeError(
            "WOOCOMMERCE_TIMEOUT_SECONDS must be an integer."
        ) from error

    if timeout_seconds <= 0:
        raise RuntimeError(
            "WOOCOMMERCE_TIMEOUT_SECONDS must be greater than zero."
        )

    return timeout_seconds


def build_api_url(
    store_url: str,
    api_version: str,
    endpoint: str,
) -> str:
    """Build a WooCommerce REST API URL."""
    clean_store_url = store_url.rstrip(
        "/"
    )

    clean_api_version = api_version.strip(
        "/"
    )

    clean_endpoint = endpoint.strip(
        "/"
    )

    return (
        f"{clean_store_url}/wp-json/"
        f"{clean_api_version}/"
        f"{clean_endpoint}"
    )


def hide_secret(
    value: str,
) -> str:
    """Return a masked credential for console output."""
    if len(value) <= 10:
        return "*" * len(value)

    return (
        value[:6]
        + "..."
        + value[-4:]
    )


def parse_error_response(
    response: requests.Response,
) -> str:
    """Return a readable WooCommerce error message."""
    try:
        response_data: Any = response.json()

    except ValueError:
        return response.text[:1000]

    if isinstance(
        response_data,
        dict,
    ):
        error_code = response_data.get(
            "code"
        )

        error_message = response_data.get(
            "message"
        )

        return (
            f"code={error_code}, "
            f"message={error_message}"
        )

    return str(
        response_data
    )[:1000]


def test_system_status_access(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
) -> None:
    """Test authenticated read access to WooCommerce."""
    api_url = build_api_url(
        store_url=store_url,
        api_version=api_version,
        endpoint="system_status",
    )

    print()
    print(
        f"Request URL: {api_url}"
    )

    response = requests.get(
        api_url,
        auth=HTTPBasicAuth(
            consumer_key,
            consumer_secret,
        ),
        headers={
            "Accept": "application/json",
            "User-Agent": (
                f"{TESTER_NAME}/"
                f"{TESTER_VERSION}"
            ),
        },
        timeout=timeout_seconds,
    )

    print(
        f"HTTP status: {response.status_code}"
    )

    if response.status_code != 200:
        error_details = parse_error_response(
            response
        )

        raise RuntimeError(
            "WooCommerce connection test failed. "
            f"{error_details}"
        )

    response_data = response.json()

    environment = response_data.get(
        "environment",
        {}
    )

    print()
    print(
        "Authenticated WooCommerce connection succeeded."
    )

    print(
        "WooCommerce version: "
        f"{environment.get('version') or '[not returned]'}"
    )

    print(
        "WordPress version: "
        f"{environment.get('wp_version') or '[not returned]'}"
    )

    print(
        "Site URL: "
        f"{environment.get('site_url') or store_url}"
    )


def test_product_read_access(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
) -> None:
    """Test product read access without changing store data."""
    api_url = build_api_url(
        store_url=store_url,
        api_version=api_version,
        endpoint="products",
    )

    response = requests.get(
        api_url,
        auth=HTTPBasicAuth(
            consumer_key,
            consumer_secret,
        ),
        headers={
            "Accept": "application/json",
            "User-Agent": (
                f"{TESTER_NAME}/"
                f"{TESTER_VERSION}"
            ),
        },
        params={
            "per_page": 1,
            "page": 1,
        },
        timeout=timeout_seconds,
    )

    print()
    print(
        "Testing product read access..."
    )

    print(
        f"HTTP status: {response.status_code}"
    )

    if response.status_code != 200:
        error_details = parse_error_response(
            response
        )

        raise RuntimeError(
            "WooCommerce product read test failed. "
            f"{error_details}"
        )

    products = response.json()

    print(
        "Product API read access succeeded."
    )

    print(
        "Products returned in test: "
        f"{len(products)}"
    )


def main() -> None:
    """Test WooCommerce REST API connectivity."""
    load_dotenv(
        PROJECT_ROOT / ".env"
    )

    print("=" * 72)
    print("WOOCOMMERCE CONNECTION TEST")
    print("=" * 72)

    print(
        f"Version: {TESTER_VERSION}"
    )

    store_url = get_required_environment_variable(
        "WOOCOMMERCE_URL"
    )

    consumer_key = get_required_environment_variable(
        "WOOCOMMERCE_CONSUMER_KEY"
    )

    consumer_secret = get_required_environment_variable(
        "WOOCOMMERCE_CONSUMER_SECRET"
    )

    api_version = os.getenv(
        "WOOCOMMERCE_API_VERSION",
        "wc/v3",
    ).strip()

    timeout_seconds = get_timeout_seconds()

    if not store_url.startswith(
        "https://"
    ):
        raise RuntimeError(
            "WOOCOMMERCE_URL must use HTTPS."
        )

    if not consumer_key.startswith(
        "ck_"
    ):
        raise RuntimeError(
            "WOOCOMMERCE_CONSUMER_KEY does not have "
            "the expected ck_ prefix."
        )

    if not consumer_secret.startswith(
        "cs_"
    ):
        raise RuntimeError(
            "WOOCOMMERCE_CONSUMER_SECRET does not have "
            "the expected cs_ prefix."
        )

    print()
    print(
        f"Store URL: {store_url}"
    )

    print(
        f"API version: {api_version}"
    )

    print(
        "Consumer key: "
        f"{hide_secret(consumer_key)}"
    )

    print(
        "Consumer secret: "
        f"{hide_secret(consumer_secret)}"
    )

    test_system_status_access(
        store_url=store_url,
        api_version=api_version,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        timeout_seconds=timeout_seconds,
    )

    test_product_read_access(
        store_url=store_url,
        api_version=api_version,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        timeout_seconds=timeout_seconds,
    )

    print()
    print("=" * 72)
    print("CONNECTION TEST RESULT")
    print("=" * 72)

    print(
        "WooCommerce REST API connection is ready."
    )

    print(
        "No product was created or modified by this test."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "WooCommerce connection test was cancelled."
        )
        sys.exit(130)

    except requests.Timeout:
        print()
        print(
            "WooCommerce connection test failed."
        )
        print(
            "Error type: Timeout"
        )
        print(
            "The WooCommerce server did not respond before "
            "the configured timeout."
        )
        sys.exit(1)

    except requests.ConnectionError as error:
        print()
        print(
            "WooCommerce connection test failed."
        )
        print(
            "Error type: ConnectionError"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)

    except Exception as error:
        print()
        print(
            "WooCommerce connection test failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)
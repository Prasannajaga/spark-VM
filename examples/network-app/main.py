import json
import os

import requests


DEFAULT_URL = "https://api.github.com/repos/python/cpython"
DEFAULT_TIMEOUT_SEC = 30.0


def main() -> None:
    url = os.getenv("NETWORK_APP_URL", DEFAULT_URL).strip() or DEFAULT_URL
    timeout = float(os.getenv("NETWORK_APP_TIMEOUT", DEFAULT_TIMEOUT_SEC))

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)


if __name__ == "__main__":
    main()

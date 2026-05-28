import requests


def main():
    url = "https://api.github.com/repos/python/cpython"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()

        print("Repository:", data["full_name"])
        print("Description:", data["description"])
        print("Stars:", data["stargazers_count"])
        print("Forks:", data["forks_count"])
        print("Open Issues:", data["open_issues_count"])
        print("URL:", data["html_url"])

    except requests.exceptions.RequestException as error:
        print("Request failed:", error)


if __name__ == "__main__":
    main()
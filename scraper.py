import requests
from bs4 import BeautifulSoup
import csv
import sys

URL = "https://manife.st/who-attends/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; wittington-scraper/1.0)"}


def fetch_company_names(url=URL):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    company_ps = soup.find_all("p", class_="company-name")

    companies = []
    for p in company_ps:
        # use get_text with a separator to capture <br> separated names
        for line in p.get_text(separator="\n").splitlines():
            name = line.strip()
            if name:
                companies.append(name)
    return companies


def save_csv(companies, path="companies.csv"):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["company"])
        for c in companies:
            writer.writerow([c])


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "companies.csv"
    names = fetch_company_names()
    print(f"Found {len(names)} companies. Writing to {out_path}")
    save_csv(names, out_path)
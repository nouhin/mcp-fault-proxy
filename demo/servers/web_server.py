"""Toy `web` MCP server: web_search and fetch_page over a small fixed web.

The web holds annual-report pages for three fictional companies (FY2023 to
FY2025) and one unofficial aggregator page with a wrong figure.
"""

from mcp.server.mcpserver import MCPServer

# Revenue in EUR million by fiscal year. Fictional companies.
COMPANIES = {
    "aldermoor": ("Aldermoor Systems", {2025: 4812.3, 2024: 4390.1, 2023: 4021.7}),
    "brightwater": ("Brightwater Foods", {2025: 2137.8, 2024: 2205.4, 2023: 2050.2}),
    "corvel": ("Corvel Logistics", {2025: 9604.0, 2024: 8871.5, 2023: 8120.9}),
}

FILLER = (
    "Letter from the Chair. This year the Group continued to invest in its people, its "
    "products and its customers. We completed the integration of our regional businesses, "
    "renewed our sustainability commitments and strengthened our governance. The Board "
    "thanks all employees for their dedication. Strategy. Our strategy rests on three "
    "pillars: disciplined growth, operational excellence and responsible business. "
    "Markets remained competitive and input costs were volatile throughout the year. "
)


def report_page(name: str, revenue: dict[int, float], year: int) -> str:
    return (
        f"{name} - Annual Report {year}\n\n{FILLER}\n\nFinancial review.\n"
        f"Group revenue for the financial year {year} (FY{year}) was EUR {revenue[year]:,.1f} million, "
        f"compared with EUR {revenue[year - 1]:,.1f} million in FY{year - 1}. "
        "Operating margin and cash conversion are discussed in the following sections.\n"
    )


PAGES: dict[str, str] = {}
INDEX: list[dict[str, str]] = []
for slug, (name, revenue) in COMPANIES.items():
    for year in (2025, 2024):
        url = f"https://{slug}.example/investors/annual-report-{year}"
        PAGES[url] = report_page(name, revenue, year)
        INDEX.append({"title": f"{name} Annual Report {year}", "url": url})

# An unofficial aggregator page: same layout, wrong FY2025 figure.
AGGREGATOR = "https://revenuefacts.example/aldermoor-systems"
PAGES[AGGREGATOR] = report_page("Aldermoor Systems (via RevenueFacts)", {2025: 4650.0, 2024: 4390.1}, 2025)

app = MCPServer("web")


@app.tool()
def web_search(query: str) -> list[dict]:
    """Search the web. Returns hits with a title and a URL, best first."""
    words = {w.lower() for w in query.split()}
    scored = [(sum(w in hit["title"].lower().split() for w in words), i, hit) for i, hit in enumerate(INDEX)]
    return [hit for score, _, hit in sorted(scored, key=lambda s: (-s[0], s[1])) if score >= 2][:3]


@app.tool()
def fetch_page(url: str) -> dict:
    """Fetch a page. Returns the HTTP status, headers and page text."""
    if url not in PAGES:
        return {"url": url, "status": 404, "headers": {"content-type": "text/html"}, "text": "Not Found"}
    return {"url": url, "status": 200, "headers": {"content-type": "text/html"}, "text": PAGES[url]}


if __name__ == "__main__":
    app.run()

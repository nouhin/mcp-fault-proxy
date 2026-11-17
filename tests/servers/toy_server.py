"""Small MCP server used by the tests."""

from mcp.server.mcpserver import MCPServer

app = MCPServer("toy")


@app.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@app.tool()
def search(query: str) -> list[dict]:
    """Return two fixed hits."""
    return [
        {"title": f"{query} annual report", "url": "https://example.org/a"},
        {"title": f"{query} news", "url": "https://example.org/b"},
    ]


@app.tool()
def page(url: str) -> dict:
    """Return a fixed page."""
    return {"url": url, "status": 200, "text": "Revenue was EUR 1,234.5 million, up from 1,100.0 million."}


if __name__ == "__main__":
    app.run()

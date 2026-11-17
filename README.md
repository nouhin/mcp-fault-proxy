# mcp-fault-proxy

A minimal fault-injecting proxy for MCP (Model Context Protocol) servers that use the stdio transport.
It sits between an MCP client and an MCP server, forwards normal traffic unchanged, and injects
configured faults into `tools/call` traffic.

Work in progress.

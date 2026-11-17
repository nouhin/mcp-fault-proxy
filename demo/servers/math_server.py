"""Toy `math` MCP server: a calculator for arithmetic expressions."""

import ast
import operator

from mcp.server.mcpserver import MCPServer

OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](evaluate(node.left), evaluate(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](evaluate(node.operand))
    raise ValueError("only numbers, + - * / ** and parentheses are allowed")


app = MCPServer("math")


@app.tool()
def calculator(expression: str) -> float:
    """Evaluate an arithmetic expression, e.g. (4812.3 - 4390.1) / 4390.1 * 100."""
    return float(evaluate(ast.parse(expression, mode="eval")))


if __name__ == "__main__":
    app.run()

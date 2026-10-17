#!/usr/bin/env python3
"""Evaluate a JS expression held in a file against the capture browser.

Usage: jsfile.py <port> <expr.js>
Prints {"value": ...} or {"error": ...}. Keeps long expressions out of the shell
entirely (no quoting, no heredoc), which is what makes these probes reproducible.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_eval import WS, evaluate, pick_page  # noqa: E402


def main():
    port, path = sys.argv[1], sys.argv[2]
    expr = open(path, encoding="utf-8").read()
    ws = WS(pick_page(port)["webSocketDebuggerUrl"])
    print(json.dumps(evaluate(ws, expr)))


if __name__ == "__main__":
    main()

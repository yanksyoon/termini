"""Minimal YAML-subset parser — enough for Termini remediation playbooks.

The controller is stdlib-only by decision (the headless security scanner blocks
pip installs, so PyYAML is not available). This module implements the subset of
YAML that playbooks use, deterministically and with zero dependencies:

  * block mappings          key: value  /  key: (nested block)
  * block sequences         - item      /  - key: value (sequence of mappings)
  * nested indentation (spaces only)
  * inline flow sequences   [a, b, c]
  * inline flow mappings    {a: b, c: d}
  * scalars                 str / int / float / bool (true/false/yes/no/on/off)
                            / null / "quoted" / 'quoted'
  * comments                # to end of line (quote-aware)

Deliberately NOT supported (playbooks must not use them): block scalars (| and
>), anchors/aliases, multi-document streams, tabs for indentation. A playbook
that uses any of these is a load-time error, not silently mis-parsed.
"""
from __future__ import annotations

import re

_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"null", "~"}

_INT_RE = re.compile(r"[-+]?\d+")
_FLOAT_RE = re.compile(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?")


class YamlError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    """Strip a trailing comment, respecting single/double quotes."""
    in_s = in_d = False
    for i, ch in enumerate(line):
        if ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "'" and not in_d:
            in_s = not in_s
        elif ch == "#" and not in_s and not in_d:
            return line[:i]
    return line


def _split_flow(inner: str) -> list[str]:
    """Split a flow-sequence/mapping body on top-level commas."""
    parts: list[str] = []
    depth = 0
    in_s = in_d = False
    cur: list[str] = []
    for ch in inner:
        if ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "'" and not in_d:
            in_s = not in_s
        elif ch in "[{" and not in_s and not in_d:
            depth += 1
        elif ch in "]}" and not in_s and not in_d:
            depth -= 1
        if ch == "," and depth == 0 and not in_s and not in_d:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_scalar(s: str):
    s = s.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    if s in _NULL:
        return None
    low = s.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if inner == "":
            return []
        return [_parse_scalar(p) for p in _split_flow(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if inner == "":
            return {}
        out: dict = {}
        for p in _split_flow(inner):
            if ":" not in p:
                raise YamlError(f"bad flow mapping entry: {p!r}")
            k, v = p.split(":", 1)
            out[_parse_scalar(k)] = _parse_scalar(v)
        return out
    if _INT_RE.fullmatch(s):
        return int(s)
    if _FLOAT_RE.fullmatch(s):
        return float(s)
    return s


def _tokenize(text: str) -> list[tuple[int, str]]:
    toks: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if line.strip() == "":
            continue
        if "\t" in line[: len(line) - len(line.lstrip(" "))]:
            raise YamlError("tab indentation is not supported; use spaces")
        indent = len(line) - len(line.lstrip(" "))
        toks.append((indent, line.strip()))
    return toks


def loads(text: str):
    """Parse a YAML-subset document into Python objects (dict/list/scalar)."""
    toks = _tokenize(text)
    if not toks:
        return {}
    value, idx = _parse_block(toks, 0, toks[0][0])
    if idx != len(toks):
        raise YamlError(f"unexpected content at line token {idx}: {toks[idx]!r}")
    return value


def _parse_block(toks: list[tuple[int, str]], i: int, indent: int):
    if i >= len(toks) or toks[i][0] != indent:
        return None, i
    content = toks[i][1]
    if content == "-" or content.startswith("- "):
        return _parse_sequence(toks, i, indent)
    return _parse_mapping(toks, i, indent)


def _parse_sequence(toks: list[tuple[int, str]], i: int, indent: int):
    items: list = []
    while i < len(toks):
        ind, content = toks[i]
        if ind != indent or not (content == "-" or content.startswith("- ")):
            break
        rest = content[1:].strip()
        if rest == "":
            nxt = i + 1
            if nxt < len(toks) and toks[nxt][0] > indent:
                child, nxt = _parse_block(toks, nxt, toks[nxt][0])
                items.append(child)
                i = nxt
            else:
                items.append(None)
                i = nxt
        elif _looks_like_mapping(rest):
            # "- key: value" — sequence of mappings; the first entry is inline,
            # continuation lines sit at a deeper indent.
            child, nxt = _parse_inline_mapping_item(toks, i, indent, rest)
            items.append(child)
            i = nxt
        else:
            items.append(_parse_scalar(rest))
            i += 1
    return items, i


def _looks_like_mapping(s: str) -> bool:
    # A mapping entry has an unquoted "key:" prefix before any value.
    if s.startswith(("[", "{", '"', "'")):
        return False
    colon = s.find(":")
    return colon != -1 and s[:colon].strip() != "" and " " not in s[:colon].strip()


def _parse_inline_mapping_item(toks, i, indent, rest):
    """Parse a sequence item that begins a mapping on the same line."""
    key, _, val = rest.partition(":")
    key = key.strip()
    val = val.strip()
    out: dict = {}
    if val == "":
        # nested block follows on deeper-indented lines
        nxt = i + 1
        if nxt < len(toks) and toks[nxt][0] > indent:
            nested, nxt = _parse_block(toks, nxt, toks[nxt][0])
            out[key] = nested
        else:
            out[key] = None
        return out, nxt
    out[key] = _parse_scalar(val)
    # consume continuation lines (deeper indent) into this same mapping
    nxt = i + 1
    if nxt < len(toks) and toks[nxt][0] > indent:
        cont_indent = toks[nxt][0]
        cont, nxt = _parse_mapping(toks, nxt, cont_indent)
        if isinstance(cont, dict):
            out.update(cont)
    return out, nxt


def _parse_mapping(toks: list[tuple[int, str]], i: int, indent: int):
    out: dict = {}
    while i < len(toks):
        ind, content = toks[i]
        if ind != indent:
            break
        if content == "-" or content.startswith("- "):
            break
        if _looks_like_mapping(content):
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                nxt = i + 1
                if nxt < len(toks) and toks[nxt][0] > indent:
                    child, nxt = _parse_block(toks, nxt, toks[nxt][0])
                    out[key] = child
                else:
                    out[key] = None
                i = nxt
            else:
                out[key] = _parse_scalar(val)
                i += 1
        else:
            raise YamlError(f"bad mapping line: {content!r}")
    return out, i

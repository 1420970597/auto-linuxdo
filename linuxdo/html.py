"""Minimal HTML helpers shared by the Linux.do and V2EX readers."""

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode


def node_text(node: LexborNode) -> str:
    """Visible text of one node on a single line."""
    return " ".join(node.text(separator=" ").split())


def text(fragment: str) -> str:
    """Visible text of an HTML fragment, one trimmed line per block."""
    tree = HTMLParser(fragment)
    node = tree.body or tree.root
    if node is None:
        return ""
    lines = (line.strip() for line in node.text(separator="\n").splitlines())
    return "\n".join(line for line in lines if line)


def first_text(html: str, selector: str) -> str:
    tree = HTMLParser(html)
    node = tree.css_first(selector)
    return node_text(node) if node else ""

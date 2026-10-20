"""Faithfulness: every text and button on a page equals the captured payload it claims to show.

``assert_faithful`` parses the page (the generated HTML, or the DOM Chrome dumped after loading it) and
checks, for every ``.msg``: the ``data-payload-sha256`` and ``data-seq`` of the captured message, the
text content of ``.msg-text`` against the payload's visible text, and the button labels row by row.
Text anywhere else must sit inside a ``data-chrome`` element; the reply quote and the pinned bar must be
the first line of the message they reference. Every shown message must be on the page exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

from demokit.chatstate import Frame, Message
from demokit.frame import first_line, visible_text

VOID = frozenset({"meta", "link", "br", "hr", "img", "input"})


class Unfaithful(AssertionError):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Node | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self) -> list[Node]:
        out: list[Node] = [self]
        for c in self.children:
            if isinstance(c, Node):
                out.extend(c.walk())
        return out

    def find(self, cls: str) -> list[Node]:
        return [n for n in self.walk() if cls in n.classes]


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.cur.children.append(Node(tag, {k: v or "" for k, v in attrs}, self.cur))

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is None or node.parent is None:
            raise Unfaithful(f"unbalanced </{tag}>")
        self.cur = node.parent

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def parse(page: str) -> Node:
    tree = _Tree()
    tree.feed(page)
    tree.close()
    return tree.root


def _inside(node: Node, pred: str) -> bool:
    cur: Node | None = node
    while cur is not None:
        if pred == "chrome" and "data-chrome" in cur.attrs:
            return True
        if pred != "chrome" and pred in cur.classes:
            return True
        cur = cur.parent
    return False


def _stray_text(root: Node) -> list[str]:
    body = next((n for n in root.walk() if n.tag == "body"), None)
    if body is None:
        raise Unfaithful("no <body>")
    stray: list[str] = []
    for node in body.walk():
        for child in node.children:
            if (
                isinstance(child, str)
                and child.strip()
                and not (_inside(node, "chrome") or _inside(node, "msg-text") or _inside(node, "btn"))
            ):
                stray.append(child.strip()[:60])
    return stray


def _prefix_of_first_line(text: str, msg: Message, what: str) -> None:
    line = first_line(msg)
    if not text or not line.startswith(text):
        raise Unfaithful(f"{what} {text!r:.60} is not the first line of {msg.key} ({line!r:.60})")


def assert_faithful(page: str, frame: Frame) -> int:
    """Raise ``Unfaithful`` on any mismatch; returns the number of messages checked."""
    root = parse(page)
    stray = _stray_text(root)
    if stray:
        raise Unfaithful(f"text outside data-chrome, message text and buttons: {stray[:3]}")
    by_key = {m.key: m for m in frame.messages}
    shown = [n for n in root.walk() if "msg" in n.classes and "data-key" in n.attrs]
    keys = [n.attrs["data-key"] for n in shown]
    if keys != [m.key for m in frame.messages]:
        raise Unfaithful(f"page shows {keys}, the frame has {[m.key for m in frame.messages]}")
    for node in shown:
        msg = by_key[node.attrs["data-key"]]
        if node.attrs.get("data-payload-sha256") != msg.sha256():
            raise Unfaithful(f"{msg.key}: payload hash differs")
        if node.attrs.get("data-seq") != str(msg.seq):
            raise Unfaithful(f"{msg.key}: seq differs")
        texts = node.find("msg-text")
        if len(texts) != 1:
            raise Unfaithful(f"{msg.key}: {len(texts)} text elements")
        if texts[0].text() != visible_text(msg):
            raise Unfaithful(f"{msg.key}: shown text differs from the captured payload")
        rows = [[b.text() for b in row.find("btn")] for row in node.find("kb-row")]
        if rows != msg.labels():
            raise Unfaithful(f"{msg.key}: buttons {rows} differ from the payload {msg.labels()}")
        for quote in node.find("reply"):
            ref = frame.state.messages.get(int(quote.attrs.get("data-ref", "m0")[1:]))
            if ref is None:
                raise Unfaithful(f"{msg.key}: reply to an unknown message")
            _prefix_of_first_line(quote.text(), ref, "reply quote")
    for pin in root.find("pinned"):
        if frame.pinned is None or pin.attrs.get("data-ref") != frame.pinned.key:
            raise Unfaithful("pinned bar without the pinned message")
        _prefix_of_first_line(pin.find("pin-text")[0].text(), frame.pinned, "pinned bar")
    if frame.pinned is not None and not root.find("pinned"):
        raise Unfaithful("the pinned message is missing from the pinned bar")
    return len(shown)

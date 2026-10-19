"""The icon vocabulary (DESIGN §14.17.1). One leading icon per line, always followed by a word, never
inside verbatim blocks. Kit templates use only these as leading emoji (``tests/telegram/test_kit_icons.py``)."""

from __future__ import annotations

PR = "🧩"
REVISION = "🔁"
COMMENT = "💬"
CLAIM = "🙋"
SKILL = "🧠"
SUBMITTED = "🚀"
MERGED = "🎉"
CLOSED = "🗂"
DRY_RUN = "🧪"
LIVE = "🔴"
OK = "✅"
FAIL = "❌"
WARN = "⚠️"
PENDING = "⏳"
SKIPPED = "⏭"
NOT_APPROVABLE = "⛔"
MODEL = "🤖"
EXPLORER = "🧭"
FILES = "🧾"
INTERDIFF = "🔀"
CONTROL = "🚦"
UP = "🟢"
DRAINING = "🟡"
DOWN = "💤"
PAUSED = "⏸"
KILLED = "🛑"
MIRROR = "🐙"
FEED = "📈"
QUEUE = "📥"
SEARCH = "🔎"
PAGE = "📄"
QUIET = "🌙"
PROPOSAL = "💡"
USER = "👤"
SIZE = "📏"
CEILING = "🧱"
WORK = "🛠"
LINK = "🔗"
REVIEWS = "✅"
ASSISTANT = "💬"

# §14.10.1 system topics: name -> (title, icon colour). The title's icon is the flat-mode section icon.
SYSTEM_TOPICS: dict[str, tuple[str, int]] = {
    "control": (f"{CONTROL} Control", 0x6FB9F0),
    "reviews": (f"{REVIEWS} Reviews", 0x8EEE98),
    "feed": (f"{FEED} OSS feed", 0xFFD67E),
    "assistant": (f"{ASSISTANT} Assistant", 0xCB86DB),
}
SECTION_ICON: dict[str, str] = {name: title.split(" ", 1)[0] for name, (title, _) in SYSTEM_TOPICS.items()}

VOCABULARY: frozenset[str] = frozenset(
    v for k, v in globals().items() if k.isupper() and isinstance(v, str) and k not in {"VOCABULARY"}
) | frozenset(
    {
        "✏️",
        "📝",
        "📋",
        "📨",
        "🗑",
        "↩️",
        "▶️",
        "⬇️",
        "⬆️",
        "🔄",
        "🗳",
        "📂",
        "📚",
        "🚫",
        "⏰",
        "✂️",
        "🧹",
        "💾",
        "🔒",
        "🔓",
        "🌐",
        "🎚",
        "🗓",
        "🔔",
        "🔕",
        "↪️",
        "🔍",
        "✍️",
        "📊",
        "🏷",
        "🧰",
        "🆕",
    }
)

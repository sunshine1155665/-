"""Platform prompt variants: which capabilities each entry point may use.

The stable prefix is no longer one fixed string per deployment: the desktop
window, a QQ group and a roaming room each get the tool descriptions and rules
they can actually execute, and nothing else.  Three layers decide the set:

1. **Platform -> variant** (:func:`variant_for_platform`,
   :func:`platform_from_session`): the adapter passes the platform explicitly;
   session-id inference is only a fallback for CLI/tests/older callers.
2. **Variant -> capabilities** (:data:`VARIANT_CAPABILITIES`): the product
   decision (2026-11-15 boss mandate):
   * ``full``      -- owner QQ private chat / CLI / unknown: every action.
   * ``desktop``   -- the pet shell: same partner abilities **including**
     sharing, but no ``send_voice`` (the shell speaks every bubble through the
     local TTS automatically), plus the desktop rules block.
   * ``qq_group``  -- minimal set: chat + stickers + group rules; no voice, no
     memory writes, no sharing/qzone/read/roam/media/photo/sing.
   * ``roam_room`` -- full abilities **plus** the roaming rules block.
3. **Runtime availability** (:func:`filter_capabilities`): capabilities the
   process cannot serve right now are removed before the prompt is built --
   notably QQ history reads require an adapter that implements
   ``fetch_friend_history`` / ``fetch_group_history`` (NapCat/OneBot); the
   official Tencent QQ Bot API has no history endpoint, so the read/talk
   descriptions must not be promised there.

Everything here is a pure function over strings: no DB, no network, trivially
testable.
"""

from __future__ import annotations

from hashlib import sha256

from .prompt_blocks import ACTION_BLOCKS, ACTION_ORDER, PLATFORM_RULE_BLOCKS

# --------------------------------------------------------------- capability ids
CAP_CHAT = "chat"
CAP_STICKER = "sticker"
CAP_WAKEUP = "wakeup"
CAP_VOICE = "voice.file"
CAP_MEMORY_WRITE = "memory.write"
CAP_MEMORY_SEARCH = "memory.search"
CAP_SHARE_OWNER = "share.owner"
CAP_SHARE_FRIEND = "share.friend"
CAP_QZONE_POST = "qzone.post"
CAP_QZONE_READ = "qzone.read"
CAP_QQ_READ = "qq.read"
CAP_QQ_TALK = "qq.talk"
CAP_ROAM = "roam"
CAP_MEDIA = "media"
CAP_PHOTO = "photo"
CAP_SING = "sing"
CAP_RULES_GROUP = "rules.group"
CAP_RULES_DESKTOP = "rules.desktop"
CAP_RULES_ROAM = "rules.roam"

#: Capability -> the action blocks it contributes.  ``chat`` carries no action
#: of its own (the JSON envelope header/footer always ride along).
CAPABILITY_ACTIONS: dict[str, tuple[str, ...]] = {
    CAP_CHAT: (),
    CAP_STICKER: ("send_sticker",),
    CAP_WAKEUP: ("schedule_wakeup",),
    CAP_VOICE: ("send_voice",),
    CAP_MEMORY_WRITE: ("remember", "open_loop", "close_loop"),
    CAP_MEMORY_SEARCH: ("search_memory",),
    CAP_SHARE_OWNER: ("share_with_owner",),
    CAP_SHARE_FRIEND: ("share_to_friend",),
    CAP_QZONE_POST: ("post_qzone",),
    CAP_QZONE_READ: ("read_qzone",),
    CAP_QQ_READ: ("read_chat",),
    CAP_QQ_TALK: ("talk_to",),
    CAP_ROAM: ("roam_visit", "roam_return"),
    CAP_MEDIA: ("watch_video", "search_videos"),
    CAP_PHOTO: ("make_photo",),
    CAP_SING: ("sing",),
}

#: Capability -> platform rule block rendered at the end of the stable prefix.
CAPABILITY_RULES: frozenset[str] = frozenset(
    {CAP_RULES_GROUP, CAP_RULES_DESKTOP, CAP_RULES_ROAM}
)

#: Capabilities that require an adapter / service; dropped at build time when
#: the process cannot serve them (never promise an action that is unavailable).
RUNTIME_GATED: dict[str, str] = {
    CAP_QQ_READ: "qq_history",
    CAP_QQ_TALK: "qq_history",
    CAP_PHOTO: "photo",
    CAP_SING: "sing",
}

FULL_CAPABILITIES: frozenset[str] = frozenset(
    {CAP_CHAT, CAP_STICKER, CAP_WAKEUP, CAP_VOICE, CAP_MEMORY_WRITE, CAP_MEMORY_SEARCH,
     CAP_SHARE_OWNER, CAP_SHARE_FRIEND, CAP_QZONE_POST, CAP_QZONE_READ,
     CAP_QQ_READ, CAP_QQ_TALK, CAP_ROAM, CAP_MEDIA, CAP_PHOTO, CAP_SING}
)

VARIANT_CAPABILITIES: dict[str, frozenset[str]] = {
    "full": FULL_CAPABILITIES,
    # The shell renders the reply bubble and speaks it with the local TTS, so
    # ``send_voice`` would be a second, invisible voice channel; the 2026-11-15
    # boss decision is "desktop auto-speaks, change allowed".
    "desktop": (FULL_CAPABILITIES - {CAP_VOICE}) | {CAP_RULES_DESKTOP},
    # Minimal by product decision: chat + stickers + group rules, no voice.
    "qq_group": frozenset({CAP_CHAT, CAP_STICKER, CAP_RULES_GROUP}),
    "roam_room": FULL_CAPABILITIES | {CAP_RULES_ROAM},
}

#: Adapter-supplied platform ids -> variant.  ``qq_private`` is the same owner
#: chat as ``full`` (the QQ private adapter owns every action).
PLATFORM_VARIANTS: dict[str, str] = {
    "full": "full",
    "qq_private": "full",
    "qq": "full",
    "cli": "full",
    "desktop": "desktop",
    "qq_group": "qq_group",
    "group": "qq_group",
    "roam_room": "roam_room",
    "roam": "roam_room",
}

PLATFORM_IDS: tuple[str, ...] = ("qq_private", "qq_group", "desktop", "roam_room")

MAX_ACTIONS_PER_PREFIX = len(ACTION_ORDER)


def variant_for_platform(platform: str | None) -> str:
    """Map an adapter-supplied platform id to a variant (``full`` fallback)."""
    key = str(platform or "").strip().lower()
    return PLATFORM_VARIANTS.get(key, "full")


def platform_from_session(session_id: str | None) -> str:
    """Best-effort platform inference for callers that pass no platform.

    Only the shapes that are unambiguous are recognised: the desktop surface
    owns ``desktop:*``, group sessions contain ``:group:``; everything else
    (owner private, CLI, wakeup/activity sessions) is the full variant.  Roam
    rooms look like ordinary private sessions, so the runtime resolves them
    through the trip ledger instead of guessing here.
    """
    value = str(session_id or "").strip().lower()
    if value.startswith("desktop:"):
        return "desktop"
    if ":group:" in value or value.startswith(("group:", "qq_group:")):
        return "qq_group"
    return "full"


def capabilities_for(variant: str) -> frozenset[str]:
    return VARIANT_CAPABILITIES.get(str(variant or "").strip(), FULL_CAPABILITIES)


def actions_for_capabilities(capabilities: frozenset[str]) -> frozenset[str]:
    """The action tool names a capability set can actually execute."""
    if capabilities is FULL_CAPABILITIES:
        return frozenset(ACTION_ORDER)
    out: set[str] = set()
    for capability in capabilities:
        out.update(CAPABILITY_ACTIONS.get(capability, ()))
    return frozenset(name for name in out if name in ACTION_BLOCKS)


def filter_capabilities(
    capabilities: frozenset[str],
    *,
    qq_history: bool = True,
    photo: bool = True,
    sing: bool = True,
) -> frozenset[str]:
    """Drop capabilities the current process cannot serve.

    ``qq_history`` is the one that matters in production: ``read_chat`` /
    ``talk_to`` are built on NapCat/OneBot extensions (``fetch_*_history``);
    the official Tencent QQ Bot API has no message-history endpoint, so those
    descriptions must not reach the prompt there.
    """
    available = {"qq_history": bool(qq_history), "photo": bool(photo), "sing": bool(sing)}
    dropped = {
        capability
        for capability, gate in RUNTIME_GATED.items()
        if capability in capabilities and not available[gate]
    }
    return frozenset(capabilities - dropped) if dropped else capabilities


def capability_signature(capabilities: frozenset[str]) -> str:
    """Stable hash of a resolved capability set (metrics / prefix-hash key)."""
    joined = "|".join(sorted(capabilities))
    return sha256(joined.encode("utf-8")).hexdigest()[:16]


def rules_for_capabilities(capabilities: frozenset[str]) -> list[str]:
    """Rule blocks for the capability set, in canonical order."""
    out: list[str] = []
    for name in (CAP_RULES_GROUP, CAP_RULES_DESKTOP, CAP_RULES_ROAM):
        if name in capabilities:
            text = PLATFORM_RULE_BLOCKS.get(name, "")
            if text:
                out.append(text)
    return out

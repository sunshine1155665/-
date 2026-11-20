"""Action policy: trust levels, action allowlist, caps and write rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .trust import (
    TrustLevel,
    can_write,
    redact_outbound,
    trust_for_session,
    wrap_untrusted,
)


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    min_trust: TrustLevel
    max_calls: int
    allow_group: bool = False
    write: bool = False
    required_text: str | None = None
    # T-0082: the action exists only on the owner's verbatim instruction.
    # Enforced in the runtime (after policy.validate, before the self-action
    # gate) via ``explicit_request(user_text, tool)`` over the REAL inbound
    # message -- self-generated/ephemeral turns pass user_text="" and refuse;
    # args flags, desire scores and trust levels can never substitute.
    requires_explicit: bool = False


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    spec: ActionSpec | None = None

    def __bool__(self) -> bool:
        return self.allowed


ACTION_SPECS: dict[str, ActionSpec] = {
    "send_sticker": ActionSpec("send_sticker", TrustLevel.GROUP, 1, allow_group=True, required_text="need"),
    "send_voice": ActionSpec("send_voice", TrustLevel.PRIVATE, 1, allow_group=False),
    "schedule_wakeup": ActionSpec("schedule_wakeup", TrustLevel.PRIVATE, 1, allow_group=False, write=True, required_text="when"),
    "remember": ActionSpec("remember", TrustLevel.PRIVATE, 2, allow_group=False, write=True, required_text="text"),
    "open_loop": ActionSpec("open_loop", TrustLevel.PRIVATE, 2, allow_group=False, write=True, required_text="text"),
    "close_loop": ActionSpec("close_loop", TrustLevel.PRIVATE, 2, allow_group=False, write=True, required_text="id"),
    # Publishing a QQ Zone shuoshuo: private-trust, one per turn, and never a
    # direct post -- the action only opens a draft that the owner must approve
    # (or that the desire threshold auto-publishes; ADR-0005).
    "post_qzone": ActionSpec("post_qzone", TrustLevel.PRIVATE, 1, allow_group=False, write=True, required_text="content"),
    # Sharing something with the owner on her own initiative: schedules a
    # wakeup (never sends directly), so the ProactiveGate still owns quiet
    # hours, pacing and the daily proactive budget. Private only -- she does
    # not interrupt group chats uninvited.
    "share_with_owner": ActionSpec("share_with_owner", TrustLevel.PRIVATE, 2, allow_group=False, write=True, required_text="text"),
    # Sharing something with the bound friend slot holder (姐姐): the ONE
    # extra person she may proactively DM. The target is resolved from
    # owner_bindings at execution time -- never from the LLM's args -- and a
    # missing/unreachable binding is a refusal, not a guess.
    "share_to_friend": ActionSpec("share_to_friend", TrustLevel.PRIVATE, 2, allow_group=False, write=True, required_text="text"),
    # Singing on request: private-only, one song per turn, and never a
    # background job -- the render holds the shared VRAM lease for a minute or
    # two (TTS unloaded -> YuE2/RVC -> TTS reloaded), so it must stay a deliberate
    # act in the current conversation.
    "sing": ActionSpec("sing", TrustLevel.PRIVATE, 1, allow_group=False, write=True),
    # Self-initiated read actions (T-0042 L2): she may look something up in her
    # own memory or read the Zone feed without being asked. Private only --
    # self-actions never fire in a group chat -- and ``write`` stays False on
    # purpose: nothing leaves the machine, the result only lands in
    # self_action_evidence for a later turn.
    "search_memory": ActionSpec("search_memory", TrustLevel.PRIVATE, 1, allow_group=False, required_text="query"),
    "read_qzone": ActionSpec("read_qzone", TrustLevel.PRIVATE, 1, allow_group=False),
    # Reading a QQ conversation through the person-card layer (T-0072). The
    # ``scope`` arg (group | forward | private) is mandatory text; whether the
    # concrete target is allowed (group whitelist / bound session / cached
    # forward) is the runtime branch's job -- this spec only pins trust,
    # budget and the "she must name what she wants to read" floor. Group
    # reads happen *from* a private session; the action itself never fires
    # inside a group chat, like every other self-action.
    "read_chat": ActionSpec("read_chat", TrustLevel.PRIVATE, 1, allow_group=False, required_text="scope"),
    # Watching one Bilibili video on demand (T-0074). Private-only, one per
    # turn, read-only. No ``required_text`` on purpose: "she names what to
    # watch" is an *either-or* (a direct ``bvid`` or a ``query`` to search),
    # which a single required-text floor cannot express -- the runtime branch
    # refuses when neither is present and validates the depth. The watch
    # itself is bounded by the runtime budgets (daily cap / per-session
    # cooldown / per-query dry counter), not here.
    "watch_video": ActionSpec("watch_video", TrustLevel.PRIVATE, 1, allow_group=False),
    # ADR-0014 roaming: send her to one bound private room.  The target must
    # resolve to a standing-grant room (family/teacher/friend); the runtime
    # refuses strangers, groups and a second concurrent trip.  Private-only
    # because a group must not be able to send her anywhere.
    "roam_visit": ActionSpec(
        "roam_visit", TrustLevel.PRIVATE, 1, allow_group=False, write=True,
        required_text="target",
    ),
    # Bring her home from the current trip.  No recipient, no content.
    "roam_return": ActionSpec("roam_return", TrustLevel.PRIVATE, 1, allow_group=False, write=True),
    # Talking TO a named person on the owner's verbatim instruction (T-0082):
    # 群内接话 (scope=group, the message goes to the current group) and 私聊
    # 单发 (scope=private, a DM to a person-card holder). The whole freedom of
    # this action comes from one sentence of the owner's -- ``requires_explicit``
    # makes ``explicit_request(user_text, "talk_to")`` the only door; she can
    # never open a conversation on her own. Two calls per turn (one
    # instruction, at most two lines), plain text only.
    #
    # ``min_trust`` is GROUP and ``write`` is False *deliberately* -- deviating
    # from the design note's (PRIVATE, write=True): a group turn carries GROUP
    # trust, and either ``trust < min_trust`` or ``write and not
    # can_write(trust)`` would refuse every group turn at this layer, killing
    # the scope=group case whose only legal home IS a group turn. The
    # outbound-write safety instead rides on the owner-verbatim gate, local
    # -data-only target resolution (group members / person cards -- a uin
    # typed by the model is never a passport), the per-target cooldown and
    # the max_text_chars cap in the runtime branch.
    "talk_to": ActionSpec(
        "talk_to",
        TrustLevel.GROUP,
        2,
        allow_group=True,
        required_text="text",
        requires_explicit=True,
    ),
    # Self-initiated *costly* actions (T-0042 L3). A photo holds the shared
    # VRAM lease for a minute or two; a video search costs a network round
    # trip and writes new rows into memory. Both are private-only and at most
    # once per turn -- ``write`` stays False because nothing is sent anywhere:
    # the artefact and the evidence stay local for a later turn.
    "make_photo": ActionSpec("make_photo", TrustLevel.PRIVATE, 1, allow_group=False, required_text="prompt"),
    "search_videos": ActionSpec("search_videos", TrustLevel.PRIVATE, 1, allow_group=False, required_text="topic"),
}


class PolicyEngine:
    def __init__(self, config: Any | None = None):
        self.config = config
        self.specs = dict(ACTION_SPECS)

    def trust_for(self, session_id: str | None, *, is_group: bool = False) -> TrustLevel:
        return trust_for_session(session_id, is_group=is_group)

    def validate(
        self,
        tool: str,
        args: dict[str, Any] | None,
        *,
        trust: TrustLevel,
        count: int = 0,
        allowed: frozenset[str] | None = None,
    ) -> PolicyDecision:
        """Trust / budget validation, plus the platform capability gate.

        ``allowed`` is the set of action names the current prompt variant can
        actually execute (2026-11-15 platform variants).  ``None`` keeps the
        historical "every known action is legal" behaviour for callers that do
        not have a variant.  The capability check runs first: an action the
        platform never offers should read as unsupported, not as a trust
        problem.
        """
        spec = self.specs.get(str(tool or "").strip())
        if spec is None:
            return PolicyDecision(False, "unknown_action")
        if trust == TrustLevel.GROUP and not spec.allow_group:
            return PolicyDecision(False, "group_not_allowed", spec)
        if trust < spec.min_trust:
            return PolicyDecision(False, f"trust_too_low:{trust.name.lower()}", spec)
        if spec.write and not can_write(trust):
            return PolicyDecision(False, "write_requires_private_trust", spec)
        if count >= spec.max_calls:
            return PolicyDecision(False, "too_many_tool_calls", spec)
        # Platform variants: an action the entry point never offers reads as
        # unsupported.  Deliberately after the trust/safety checks so a group
        # sing still says ``group_not_allowed`` rather than "your prompt did
        # not list it".
        if allowed is not None and str(tool or "").strip() not in allowed:
            return PolicyDecision(False, "unsupported_on_platform", spec)
        payload = args or {}
        if spec.required_text:
            value = str(payload.get(spec.required_text) or "").strip()
            if not value:
                return PolicyDecision(False, f"missing_{spec.required_text}", spec)
        return PolicyDecision(True, "ok", spec)

    def redact(self, text: str) -> tuple[str, list[str]]:
        return redact_outbound(str(text or ""))

    def redact_soft(self, text: str) -> str:
        return redact_outbound(str(text or ""))[0]

    def wrap_recall(self, text: str, *, source: str = "memory/rag") -> str:
        return wrap_untrusted(
            text,
            kind="recalled_content",
            source=source,
            trust=TrustLevel.MEMORY,
        )

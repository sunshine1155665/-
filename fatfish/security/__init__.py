"""Security spine: identity kernel, trust levels, injection and actions."""

from .kernel import (
    IdentityKernel,
    KernelError,
    KernelIntegrityError,
    KernelWriteError,
    kernel_policy_text,
    load_kernel,
)
from .policy import ACTION_SPECS, ActionSpec, PolicyDecision, PolicyEngine
from .trust import (
    SanitizedText,
    TrustLevel,
    can_write,
    clip_tool_output,
    contains_credentials,
    redact_outbound,
    sanitize_inbound,
    trust_for_scope,
    trust_for_session,
    trust_label,
    wrap_untrusted,
)

__all__ = [
    "ACTION_SPECS",
    "ActionSpec",
    "IdentityKernel",
    "KernelError",
    "KernelIntegrityError",
    "KernelWriteError",
    "PolicyDecision",
    "PolicyEngine",
    "SanitizedText",
    "TrustLevel",
    "can_write",
    "clip_tool_output",
    "contains_credentials",
    "kernel_policy_text",
    "load_kernel",
    "redact_outbound",
    "sanitize_inbound",
    "trust_for_scope",
    "trust_for_session",
    "trust_label",
    "wrap_untrusted",
]

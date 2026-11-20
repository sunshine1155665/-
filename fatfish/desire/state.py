"""状态键约定（T-0052）：``<domain>:state:<ident>``。

三个域的阈值状态各自落在一个 ``schema_meta`` 键里，**键不同 = 状态互不影响**：

* ``ai_chat:state:<peer_uin>``     —— T-0041 已在用，字符串必须一模一样；
* ``self_action:state:<scope>``    —— 自主动作（L2/L3 写，L4 读）；
* ``owner_chat:state:<ident>``     —— 占位，本轮没人写。

只做命名，不做 IO：读写在 ``ai_chat.service``（已存在）和后续 L2/L3 的服务里，
这样引擎层保持"无 db、无网络"。
"""

from __future__ import annotations

__all__ = ["state_key"]


def state_key(domain: str, ident: str) -> str:
    """``f"{domain}:state:{ident}"``; every domain must go through this.

    ``state_key("ai_chat", "2002") == "ai_chat:state:2002"``, which is exactly
    what ``AiChatService`` already writes, so the convention is additive.
    """
    return f"{domain}:state:{ident}"

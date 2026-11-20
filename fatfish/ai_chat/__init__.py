"""大肥鱼间聊天 (AI-to-AI QQ chat): settings + (L2/L3) threshold engine and service.

架构分层（T-0041）：
* settings  —— 本层：只读 ``[ai_chat]``，返回不可变、已校验的配置对象；
* engine    —— 阈值线性衰减 / 抬升 / 夹取 / 是否开口的纯函数；
* service   —— 状态落库、outbox 投递、审计。

账号未绑定（``peer_uin`` 留空）时功能完全关闭，所有路径都不得外发。
"""

from .settings import AiChatSettings, read_settings

__all__ = ["AiChatSettings", "read_settings"]

"""模型供应商预设。

统一按 OpenAI 兼容协议调用；各家差异（base URL、字段名、developer role 等）集中在
``compat`` 里描述，后续 M1 的 llm/client.py 负责消化。
这里**不含任何 Key**，只是"填哪家的地址"这类公开信息。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    base_url: str
    default_model: str
    models: tuple[str, ...] = ()
    docs: str = ""
    compat: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["models"] = list(self.models)
        return data


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="deepseek",
        label="DeepSeek 官方",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
        docs="https://platform.deepseek.com/api_keys",
        note="国内直连稳定，新用户有赠送额度。",
    ),
    ProviderPreset(
        id="aliyun-bailian",
        label="阿里云百炼（通义/DeepSeek/GLM 多模型）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3.7-plus",
        models=("qwen3.7-plus", "qwen3.8-flash", "deepseek-v4-pro"),
        docs="https://bailian.console.aliyun.com/",
        compat={"max_tokens_field": "max_tokens", "supports_developer_role": False},
        note="国内直连，模型最全，新用户有免费额度；视觉模型要单独声明图片能力。",
    ),
    ProviderPreset(
        id="zhipu",
        label="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-flash",
        models=("glm-4-flash", "glm-4v-flash"),
        docs="https://open.bigmodel.cn/usercenter/apikeys",
        compat={"max_tokens_field": "max_tokens", "supports_developer_role": False},
        note="glm-4-flash 有免费档，适合练手。",
    ),
    ProviderPreset(
        id="siliconflow",
        label="SiliconFlow 硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        default_model="Qwen/Qwen2.5-7B-Instruct",
        docs="https://cloud.siliconflow.cn/account/ak",
        note="聚合多家开源模型，便宜。",
    ),
    ProviderPreset(
        id="moonshot",
        label="Moonshot Kimi",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k",
        docs="https://platform.moonshot.cn/console/api-keys",
        note="长上下文友好。",
    ),
    ProviderPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=("gpt-4o-mini", "gpt-4o"),
        docs="https://platform.openai.com/api-keys",
        note="国内需要自备网络环境。",
    ),
    ProviderPreset(
        id="ollama",
        label="本地 Ollama",
        base_url="http://localhost:11434/v1",
        default_model="qwen2.5:7b",
        docs="https://ollama.com/download",
        note="完全本地、不花钱；需要在本机先跑起 ollama serve。",
    ),
)

PRESET_BY_ID = {preset.id: preset for preset in PRESETS}

#: 自定义 baseURL 白名单策略：只允许 https（本机 Ollama 例外）
ALLOWED_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def list_presets() -> list[dict]:
    return [preset.to_dict() for preset in PRESETS]
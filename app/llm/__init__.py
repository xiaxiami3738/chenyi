"""LLM 接入层（M1 起补充 client / schemas / fake）。"""

from .providers import PRESET_BY_ID, PRESETS, ProviderPreset, list_presets

__all__ = ["PRESETS", "PRESET_BY_ID", "ProviderPreset", "list_presets"]
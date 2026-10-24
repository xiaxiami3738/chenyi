"""角色人格（鲸小鱼）的单元测试。

这些断言守的是"人设不能被改坏"：称呼、尾巴动作、禁胖梗、中文与去 emoji。
"""

from __future__ import annotations

from app.services.persona import (
    PERSONA_ID,
    PERSONA_NAME,
    build_system_prompt,
    opener,
    scripted_reply,
)


def test_persona_identity_is_jingxiaoyu() -> None:
    assert PERSONA_NAME == "鲸小鱼"
    assert PERSONA_ID == "jingxiaoyu"


def test_system_prompt_encodes_character_rules() -> None:
    prompt = build_system_prompt()
    # 身份与说话方式
    assert "鲸小鱼" in prompt
    assert "主人" in prompt
    assert "尾巴" in prompt
    assert "简体中文" in prompt
    # 禁止项：不承认胖、被叫大肥鱼要抗议、不装 AI、不用 emoji
    assert "绝对不承认自己胖" in prompt
    assert "大肥鱼" in prompt
    assert "不暴露自己是 AI" in prompt
    assert "不使用 emoji" in prompt


def test_opener_uses_tail_action() -> None:
    line = opener()
    assert "尾巴" in line
    assert "（" in line and "）" in line


def test_scripted_reply_is_deterministic() -> None:
    message = "今天天气怎么样"
    assert scripted_reply(message) == scripted_reply(message)


def test_scripted_reply_protests_when_called_fat_fish() -> None:
    reply = scripted_reply("你是不是大肥鱼")
    assert "肥鱼" in reply
    assert "（" in reply  # 用动作描写表达情绪


def test_scripted_reply_loves_rice() -> None:
    assert "米饭" in scripted_reply("今天有米饭吗")


def test_scripted_reply_is_chinese_without_emoji() -> None:
    reply = scripted_reply("你好呀")
    assert reply.strip()
    # 站点视觉不用 emoji，台词里也不应出现
    emoji = "🐳🐟😀😊🍚✨!"
    assert not any(char in reply for char in emoji)
    assert any("\u4e00" <= char <= "\u9fff" for char in reply)


def test_scripted_reply_handles_blank_message() -> None:
    assert scripted_reply("   ")
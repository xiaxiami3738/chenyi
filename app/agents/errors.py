"""Agent 层异常与错误码。

PipelineError 原本在 services.pipeline 里定义，M2 把六个角色提升为独立积木层后
把异常类型也下沉到这里（services.pipeline 再原样导出，对外导入路径保持不变）。
"""

from __future__ import annotations


class PipelineError(Exception):
    """流水线自身可预期的失败（参数缺失 / 模型输出不合法等）。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_code: str = "internal",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class OutputInvalidError(PipelineError):
    """LLM 输出未通过 schema 校验 / JSON 解析（重试后仍失败）。

    按 M2 约定，连续两次解析失败统一映射为 error_code="invalid_output"。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="invalid_output")
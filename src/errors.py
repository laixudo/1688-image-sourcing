# -*- coding: utf-8 -*-
"""业务异常分级：环境问题与业务失败分开。"""

from __future__ import annotations


class EnvRetryError(RuntimeError):
    """外部环境异常（Excel 占用 / 验证码 / 图搜未跳转），应标「待重试」并熔断。"""

    status: str = "待重试(环境异常)"

    def __init__(self, message: str, *, status: str | None = None) -> None:
        super().__init__(message)
        if status:
            self.status = status


class ExcelLockedError(EnvRetryError):
    status = "Excel锁定"


class ImageSearchNavError(EnvRetryError):
    """上传后未进入图搜结果页，禁止解析首页假数据。"""

    status = "待重试(环境异常)"


class LocalImageIOError(RuntimeError):
    """单张本地图片读取/哈希失败；不得触发页面刷新或登录探测。"""

    status = "本地文件失败"


class UploadMetadataRejected(ImageSearchNavError):
    """1688 明确拒绝或无法再识别此前成功上传得到的临时元数据。"""

    status = "上传元数据失效"


class ManualActionRequired(EnvRetryError):
    """需要用户在可见浏览器内处理的状态，任务必须暂停而不能自行绕过。"""

    status = "等待人工处理"

    def __init__(self, message: str, *, kind: str = "verification") -> None:
        super().__init__(message, status=self.status)
        self.kind = kind

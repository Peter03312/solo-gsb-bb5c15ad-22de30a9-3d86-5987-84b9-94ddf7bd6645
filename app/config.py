"""运行时配置，全部通过环境变量注入。"""
import os

from pydantic import BaseModel


class Settings(BaseModel):
    # 超温上限（°C）：有效读数温度严格大于该值视为“超限”
    temp_limit: float
    # SQLite 数据库文件路径
    db_path: str


def get_settings() -> Settings:
    """每次调用实时读取环境变量，便于测试与容器注入配置。"""
    return Settings(
        temp_limit=float(os.environ.get("TEMP_LIMIT", "8.0")),
        db_path=os.environ.get("DB_PATH", "coldchain.db"),
    )

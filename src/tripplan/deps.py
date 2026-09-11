"""外部依赖的集合。穿在调用链上，避免每层都摊开一堆参数。"""

from dataclasses import dataclass

from tripplan.llm.client import LlmClient
from tripplan.providers.base import GeoProvider


@dataclass(frozen=True)
class Deps:
    client: LlmClient
    provider: GeoProvider

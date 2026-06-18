"""品牌 / campaign 配置 —— 让 journofinder 可跨品牌复用。

一个 campaign 由一个小 YAML 描述（见 brands/example.yaml）。`campaign` 命令读它驱动
整条漏斗。所有字段都有合理默认值，最小配置（brand + positioning + themes）即可跑。

改编自 pitchfinder 的 config.py，字段换成记者发现语境。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DiscoveryCfg:
    # 数据源：newsapi_ai 为主，querit/brave/apodex 补召（缺 key 自动跳过）
    providers: list[str] = field(default_factory=lambda: ["newsapi_ai", "querit", "brave"])
    date_window_days: int = 30          # 只看近 N 天的报道（NewsAPI 免费档上限 ~30）
    articles_count: int = 100           # NewsAPI.ai 单页上限 100
    pages: int = 1                      # 翻几页扩大池子
    sort_by: str = "date"               # date | sourceImportance(顶 Tier-1 大刊) | rel
    web_augment: bool = True            # 是否用 querit/brave/apodex 补召
    languages: list[str] = field(default_factory=lambda: ["eng"])


@dataclass
class TieringCfg:
    # A/B/drop 是判断任务 —— 用旗舰模型（便宜模型会高估内容农场）
    model: str = "anthropic/claude-sonnet-4.6"
    min_score: int = 60                 # relevance 低于此分的记者不进 tier 判断
    max_journalists: int = 60           # 进 tier 判断的记者上限（按 coverage 排序后截断）


@dataclass
class EnrichCfg:
    tier_a_top_n: int = 10              # 保留向后兼容（阶段 2 上限实际用 budget.max_deepdive）
    search_all: bool = True            # 阶段 1：对全部 A+B 跑便宜网搜（Querit/Brave + 文章正文）
    apodex_fallback: bool = True       # 阶段 2：缺 LinkedIn/X 时 Apodex 兜底


@dataclass
class BudgetCfg:
    max_deepdive: int = 10             # 每次 run 的 Apodex 深挖硬上限
    pitch_b_top_n: int = 60            # pitch angle：A 全做，B 只给分数最高的前 N（省 LLM）


@dataclass
class BrandConfig:
    brand: str
    one_liner: str = ""
    positioning: str = ""
    launch: str = ""                    # 本次产品发布的具体信息（pitch angle 素材）
    themes: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    do_not: list[str] = field(default_factory=list)
    discovery: DiscoveryCfg = field(default_factory=DiscoveryCfg)
    tiering: TieringCfg = field(default_factory=TieringCfg)
    enrich: EnrichCfg = field(default_factory=EnrichCfg)
    budget: BudgetCfg = field(default_factory=BudgetCfg)

    def all_keywords(self) -> list[str]:
        """构造关键词集：品牌名 + themes + competitors（去重保序）。

        竞品名也进关键词 —— 反查谁在报道竞品，往往就是该报道我们的记者。
        """
        seen: set[str] = set()
        out: list[str] = []
        for kw in [self.brand, *self.themes, *self.competitors]:
            k = (kw or "").strip()
            kl = k.lower()
            if k and kl not in seen:
                seen.add(kl)
                out.append(k)
        return out

    def brand_summary(self) -> str:
        """喂给 LLM 打分/分层/pitch 的品牌上下文。"""
        parts = [self.one_liner, self.positioning]
        if self.launch:
            parts.append("Launch: " + self.launch)
        if self.themes:
            parts.append("Themes: " + ", ".join(self.themes) + ".")
        if self.competitors:
            parts.append("Competitors: " + ", ".join(self.competitors) + ".")
        return " ".join(p for p in parts if p).strip()


def _sub(cls, data: Any):
    """从 dict 构造嵌套 dataclass，忽略未知 key。"""
    if not isinstance(data, dict):
        return cls()
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in known})


def load_brand_config(path: str | Path) -> BrandConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if "brand" not in raw:
        raise ValueError(f"{path}: 品牌配置必须有 'brand' 字段")
    return BrandConfig(
        brand=raw["brand"],
        one_liner=raw.get("one_liner", ""),
        positioning=raw.get("positioning", ""),
        launch=raw.get("launch", ""),
        themes=list(raw.get("themes", []) or []),
        competitors=list(raw.get("competitors", []) or []),
        do_not=list(raw.get("do_not", []) or []),
        discovery=_sub(DiscoveryCfg, raw.get("discovery")),
        tiering=_sub(TieringCfg, raw.get("tiering")),
        enrich=_sub(EnrichCfg, raw.get("enrich")),
        budget=_sub(BudgetCfg, raw.get("budget")),
    )

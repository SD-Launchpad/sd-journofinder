"""journofinder CLI —— 一条命令跑完整 campaign，也支持单步重跑 + 人工捞回。

主命令：
  journofinder campaign brands/<brand>.yaml      # 完整漏斗 → A/B 分层报告

单步 / 运维：
  journofinder tier <search_id> <journalist_id> {A|B|drop}   # 人工覆盖分层（捞回）
  journofinder show <search_id> brands/<brand>.yaml          # 用现有数据重渲报告
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from . import db
from .config import load_brand_config

app = typer.Typer(add_completion=False, help="媒体/记者搜索引擎")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _default_out(brand: str) -> Path:
    return Path("reports") / f"{brand}.html"


@app.command()
def campaign(
    brand_yaml: str = typer.Argument(..., help="品牌配置 YAML 路径"),
    db_path: str = typer.Option("journofinder.db", "--db", help="SQLite 路径"),
    out: str = typer.Option("", "--out", help="HTML 输出路径（默认 reports/<brand>.html）"),
    skip_discovery: bool = typer.Option(False, "--skip-discovery", help="跳过抓取，用库内已有文章重跑"),
    no_deepdive: bool = typer.Option(False, "--no-deepdive", help="跳过 Tier-A Apodex 深挖（很慢），秒级出报告；之后可异步 enrich + show 补全"),
):
    """跑完整漏斗：discover → aggregate → score → tier → enrich → pitch → render。"""
    from . import pipeline  # 延迟导入，避免无 key 时 import 链报错

    cfg = load_brand_config(brand_yaml)
    out_path = Path(out) if out else _default_out(cfg.brand)
    summary = pipeline.run_campaign(cfg, db_path, out_path,
                                    skip_discovery=skip_discovery, no_deepdive=no_deepdive)
    typer.echo("")
    typer.echo(f"✅ 完成 search #{summary['search_id']}：Tier A={summary['tier_a']} · "
               f"Tier B={summary['tier_b']} · drop={summary['dropped']}")
    typer.echo(f"   HTML: {summary['html']}")
    typer.echo(f"   CSV : {summary['csv']}")
    typer.echo(f"   MD  : {summary['md']}")


@app.command()
def tier(
    search_id: int = typer.Argument(..., help="search id"),
    journalist_id: int = typer.Argument(..., help="记者 id"),
    value: str = typer.Argument(..., help="A | B | drop"),
    db_path: str = typer.Option("journofinder.db", "--db"),
):
    """人工覆盖某记者的分层（source=manual，campaign 重跑不会被覆盖）。用于捞回被 drop 的人。"""
    v = value.strip()
    if v not in ("A", "B", "drop"):
        raise typer.BadParameter("value 必须是 A / B / drop")
    db.init_schema(db_path)  # 幂等：确保 schema/迁移最新（老库补 linkedin 列）
    conn = db.get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO journo_tiers (search_id, journalist_id, tier, rationale, source) "
            "VALUES (?, ?, ?, '(manual override)', 'manual')",
            (search_id, journalist_id, v),
        )
        conn.commit()
        typer.echo(f"✅ 记者 #{journalist_id} 在 search #{search_id} 手动设为 {v}")
    finally:
        conn.close()


@app.command()
def enrich(
    search_id: int = typer.Argument(..., help="search id"),
    brand_yaml: str = typer.Argument(..., help="品牌配置 YAML（取 enrich/budget 配置）"),
    db_path: str = typer.Option("journofinder.db", "--db"),
):
    """对某次 search 的 A/B 记者补全联系方式（只取真实来源，绝不推测）。

    阶段 1：Querit/Brave 网搜 + 文章正文邮箱（便宜，全员）；阶段 2：缺 LinkedIn/X
    者上 Apodex 兜底。优先级 LinkedIn > X > Email。异步交付：先
    `campaign --no-deepdive` 秒出报告 → 本命令补全 → `show` 重渲。
    """
    from . import aggregate
    from . import enrich as enrich_mod

    cfg = load_brand_config(brand_yaml)
    db.init_schema(db_path)  # 幂等：确保 schema/迁移最新（老库补 linkedin 列）
    conn = db.get_conn(db_path)
    try:
        journalists = aggregate.coverage_metrics(conn)
        rows = conn.execute(
            "SELECT journalist_id, tier, rationale FROM journo_tiers WHERE search_id = ?",
            (search_id,),
        ).fetchall()
        tiers = {r["journalist_id"]: {"tier": r["tier"], "rationale": r["rationale"]} for r in rows}
        stats = enrich_mod.run_enrichment(
            conn, search_id, tiers, journalists,
            max_deepdive=cfg.budget.max_deepdive,
            search_all=cfg.enrich.search_all,
            apodex_fallback=cfg.enrich.apodex_fallback,
        )
        typer.echo(f"✅ 补全完成：web_hits={stats['web_hits']}/{stats['searched']} · "
                   f"deepdived={stats['deepdived']} · 留空={stats['empty']}。"
                   f"运行 `journofinder show {search_id} {brand_yaml}` 重渲报告。")
    finally:
        conn.close()


@app.command()
def show(
    search_id: int = typer.Argument(...),
    brand_yaml: str = typer.Argument(..., help="品牌配置 YAML（用于报告标题/品牌上下文）"),
    db_path: str = typer.Option("journofinder.db", "--db"),
    out: str = typer.Option("", "--out"),
):
    """用库内现有数据重渲报告（不重新抓取/打分），常配合 tier 捞回后使用。"""
    from . import report

    cfg = load_brand_config(brand_yaml)
    out_path = Path(out) if out else _default_out(cfg.brand)
    db.init_schema(db_path)  # 幂等：确保 schema/迁移最新（老库补 linkedin 列）
    conn = db.get_conn(db_path)
    try:
        summary = report.render(conn, search_id, cfg, out_path)
        typer.echo(f"✅ 重渲 search #{search_id}：A={summary['tier_a']} B={summary['tier_b']} → {summary['html']}")
    finally:
        conn.close()


if __name__ == "__main__":
    app()

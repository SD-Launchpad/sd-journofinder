# SD-JournoFinder

**媒体/记者搜索引擎** —— 给一个品牌 + 产品发布信息，做深度调研，以顶级 PR agency 的水准产出一个**精准的 media/journalist 建联名单**：谁最近在报道你的赛道/竞品、怎么联系、用什么 angle pitch。

> 解决两个老问题：① PR agency 不知道去哪找靠谱的记者；② pitch 千篇一律、发了没人回。

## 核心思路

不靠 RSS、不靠人脉库，靠 **NewsAPI.ai 文章署名反查记者**：

```
品牌关键词(行业/赛道/竞品) → NewsAPI.ai 查近 30 天文章
  → 解析每篇的 authors(记者署名) + source(媒体)
  → 谁在密集报道这个领域，谁就是目标记者
```

每篇文章的作者署名里，`source` 给媒体（如 `TechCrunch`），`authors[].uri` 还常常直接是
`first_last@domain` 格式的真实邮箱 —— 这就是建联名单的种子。

## 七步漏斗

| 步骤 | 做什么 | 用什么 |
|---|---|---|
| 1. discover | 品牌关键词查文章，解析记者署名 + 媒体；再补召 | NewsAPI.ai 主源 + MiroMind/Querit/Brave 补召 |
| 2. aggregate | 文章按记者归一化聚合，算 coverage 指标 | 本地 |
| 3. score | 每个记者对品牌空间的相关度 0-100 | deepseek（便宜批量） |
| 4. tier | A/B/drop 分层，中立第三方过滤（drop 竞品 owned media/通稿/农场） | claude-sonnet（旗舰判断） |
| 5. enrich | Tier-A 深挖 verified 邮箱/twitter + sharp quotes；Tier-B 邮箱规则推断 | MiroMind + 规则 |
| 6. pitch | 每个记者 1-3 个 angle + **一封可直接发的完整 pitch（subject+body，"为什么他们必须报道你"）** | claude-sonnet（并发） |
| 7. render | **两层报告**：📰 媒体清单（Layer 1，含"为什么相关"+层级）→ 👤 记者明细（Layer 2，含 pitch）。🟢A/🟡B + HTML/CSV/MD | 本地 |

### 报告两层结构

- **Layer 1 媒体清单**：按媒体聚合，标注层级（Tier-1 主流 / 科技 / AI 垂直 / Newsletter / 地方）+ "为什么这家媒体相关"（权威性 + 该媒体哪些记者近期写了什么）
- **Layer 2 记者花名册 + 明细**：先一张花名册 summary table（记者 / 媒体 / 层级 / Tier / score / Email，一眼扫完、与媒体表呼应）→ 再每个记者的明细：分层理由（为什么是他）+ 1-3 angle + 完整 pitch（subject + body）

### 召回与 Tier-1 大刊（NewsAPI 调参）

`brands/<brand>.yaml` 的 `discovery` 段：
- `sort_by: sourceImportance` —— 按来源权威度排序，把 WSJ/NYT/Bloomberg 等 **Tier-1 大刊顶上来**（默认 `date` 会被高产低质源淹没）
- `pages: 3` —— 翻多页扩大池子
- `date_window_days: 30` —— **NewsAPI 免费档只索引近 ~30 天**，设更大也取不到（要 60/90 天需付费档）
- 关键词含宽词（`AI model`/`artificial intelligence`）才能反查到大刊的 AI 口记者；纯 niche 词只能捞到垂直媒体
- `tiering.min_score` 调低（如 40）= recall 优先，把二级媒体也捞进来靠分层过滤
- paywall 不影响发现：大刊记者署名照常拿得到（只有正文素材会少）

## 第 0 步：品牌 intake（AI 负责）

品牌输入是零散的（官网链接、一句话定位、一份 PDF、几句口头描述）。**你不用自己写
`brands/<brand>.yaml`** —— 把零散材料丢给 AI，AI 读全部来源 → 整理成规整 yaml →
**给你过目确认后**才开始搜索（红皮书：只写真实呈现，不编造）。完整流程 + 字段指南见
**[BRAND_INTAKE.md](BRAND_INTAKE.md)**。

## 快速开始

```bash
# 1. 装依赖
uv venv --python 3.11
uv pip install -e ".[dev]"

# 2. 配 key（至少 OPENROUTER_API_KEY + NEWSAPI_AI_KEY）
cp .env.example .env   # 然后填入你的 key

# 3. 复制示例品牌配置改成你的品牌
#    见 brands/example.yaml —— 最小只需 brand + positioning + themes

# 4. 跑完整 campaign
journofinder campaign brands/example.yaml
#    → reports/EverMind.html / .csv / .md
```

### 推荐：异步深挖（先出报告，后补联系方式）

Tier-A 的 MiroMind 深挖很慢（单个记者可能数分钟）。同步等 10 个会把报告阻塞很久。
推荐"主交付先行、细节后置"：

```bash
# ① 秒级出报告（跳过深挖，所有人先用 inferred 邮箱）
journofinder campaign brands/example.yaml --no-deepdive
#    → 记下输出里的 search #<id>

# ② 后台异步深挖 Tier-A（拿 verified 邮箱/twitter + sharp quotes）
journofinder enrich <id> brands/example.yaml

# ③ 深挖完重渲，报告升级成带 verified 联系方式
journofinder show <id> brands/example.yaml
```

### 输入：品牌配置（`brands/<brand>.yaml`）

```yaml
brand: EverMind
one_liner: "Memory is the defining line between a tool and an agent."
positioning: "..."
launch: "本次产品发布的具体信息（pitch angle 的素材）"
themes: [AI memory, AI agents, ...]        # 行业/赛道/vertical = 检索关键词
competitors: [Letta, mem0, Zep]            # 反查谁在报道竞品 + 中立第三方过滤
```

### 期待结果：A/B 分层报告

- 🟢 **Tier A** —— 高相关高置信，强烈推荐建联，含 **verified** 邮箱/twitter + 近期 sharp quotes
- 🟡 **Tier B** —— 中度相关，可建联，含 **inferred** 邮箱（规则推断）
- `drop`（竞品 owned media / 通稿 / 内容农场）不进报告，但留在 DB 可人工捞回

### 人工捞回 / 重渲

```bash
# 把被 drop 的记者 #42 手动捞回成 B（source=manual，重跑不会被覆盖）
journofinder tier <search_id> 42 B

# 用库内现有数据重渲报告（不重新抓取/打分）
journofinder show <search_id> brands/example.yaml
```

## API Key

| Key | 必填 | 用途 |
|---|---|---|
| `OPENROUTER_API_KEY` | ✅ | relevance 打分 + tier 分层 + pitch angle |
| `NEWSAPI_AI_KEY` | ✅ | 主源；免费档 2000 搜/月、近 30 天。申请：https://newsapi.ai/ |
| `MIROMIND_API_KEY` / `MIROTHINKER_API_KEY` | 可选 | 记者补召 + Tier-A 联系方式深挖 |
| `QUERIT_API_TOKEN` / `BRAVE_API_KEY` | 可选 | 网搜补召 |

缺可选 key 时对应能力自动跳过，不影响主流程。

## 成本

单次 campaign 约 **$0.5–5**：discover 走免费档；score 用便宜模型；深挖按 `budget.max_deepdive`（默认 10）硬上限受控。

## 设计参考

栈复用自 [sd-pitchfinder](https://github.com/SD-Launchpad/sd-pitchfinder)（创作者发现 + pitch angle）与 [sd-pulse](https://github.com/SD-Launchpad/sd-pulse)（NewsAPI.ai 媒体监控）。区别：pitchfinder 靠 RSS feed 发现「创作者」，journofinder 靠**文章署名反查记者**。

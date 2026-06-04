# 品牌 Intake —— 第 0 步（AI 负责）

> **谁来做**：AI。你（使用者）只管把**零散材料**丢过来，AI 负责整理成规整的
> `brands/<brand>.yaml`，**给你过目确认后**才开始搜索。你不需要自己写 yaml。

品牌输入天然是零散的（官网链接、一句话定位、一份 PDF、几句口头描述……）。
journofinder 的搜索引擎需要一个结构化配置才能跑，所以中间必须有一步「整理」。
这一步由 AI 完成，遵循**红皮书原则：只写官网/报告/材料里真实呈现的内容，不编造。**

## 你可以丢什么（有多少给多少）

- 官网 / landing page 链接（preview 也行）
- 一句话定位、slogan、tagline
- 产品发布 / launch 的具体信息
- technical report / 白皮书 / PDF / deck
- 新闻稿、competitor 名单、目标受众
- 甚至只是口头几句「我们做 X，对标 Y」

## AI 的处理步骤

1. **读全部来源** —— WebFetch 抓官网、Read 读 PDF/deck，把真实呈现的内容吃透
2. **按字段提炼** —— 填进 `brands/<brand>.yaml`（字段指南见下）
3. **红皮书过滤** —— 只写材料里有的；拿不准的标注、不脑补
4. **产出 yaml + 给你过目** —— 重点请你确认 `competitors` / `themes` / `launch` / `do_not`
5. **你确认/微调后** —— 才执行 `journofinder campaign brands/<brand>.yaml`

## 字段填写指南

| 字段 | 怎么填 |
|---|---|
| `brand` | 品牌名（也是首个检索关键词） |
| `one_liner` | 一句话定位，尽量用官网原话 |
| `positioning` | 一段定位，注入打分/分层/pitch 的上下文 |
| `launch` | 本次发布的**具体信息 + 关键数字/事实**——这是 pitch angle 的弹药，越具体越好 |
| `themes` | **检索关键词**，决定「去 NewsAPI 找谁在写这个空间」。诀窍：① 赛道/品类核心词（精准）② 加几个宽词（如 `AI model`/`artificial intelligence`）才反查得到 WSJ/Bloomberg 这类大刊的口线记者。太窄只能捞到垂直媒体 |
| `competitors` | 对标/同类**尽量全列**（材料点名的都写）。用途两个：反查报道竞品的记者 + 中立第三方过滤(drop 竞品 owned media) |
| `do_not` | 红皮书护栏：禁止编造的数字/客户、未定稿数据、内部批注；**若是 rebrand 要写明去关联化**（不提旧品牌血统） |
| `discovery.sort_by` | `sourceImportance` 把 Tier-1 大刊顶上来（默认 `date` 会被高产低质源淹没） |
| `discovery.pages` | 翻几页扩大池子（建议 3） |
| `discovery.date_window_days` | NewsAPI 免费档只索引近 ~30 天，设更大无效 |
| `discovery.languages` | `[eng]` 或 `[eng, zho]`（涉及中文媒体时加 zho） |
| `tiering.min_score` | recall 取舍：调低（如 40）= 把二级媒体也捞进来靠分层过滤；调高 = 只留高相关 |

字段的完整示例见 `brands/example.yaml`（带注释的模板）。

## 真实案例：Apodex

输入：① 官网 Framer 链接 ② `Apodex 1.0 Technical Report.pdf` ③ 口头「对标 OpenAI o3 / Kimi K2，这是 rebrand，旧版叫 MiroMind」

AI 做的：读官网（拿到 "Think. Act. Verify. Evolve." 定位）+ 读 38 页 PDF（拿到 SOTA benchmark 数字、agent-team 自验证架构、竞品全名单）→ 提炼成 `brands/apodex.yaml` → 用户确认时补全 competitors（报告点名的全加）、收窄 themes、加 rebrand 去关联化护栏 → 跑出 47 记者 / 37 媒体的两层报告。

> 注：含敏感定位/rebrand 信息的 live 品牌 yaml 不进版本控制（`.gitignore` 已排除 `brands/*.yaml`，只保留 `example.yaml`）。

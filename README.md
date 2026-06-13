# SD-JournoFinder

**媒体/记者搜索引擎** —— 给一个品牌 + 产品发布信息，自动做深度调研，产出一份**可直接拿去 pitch 的精准媒体/记者名单**：谁最近在报道你的赛道、怎么联系、用什么 angle、甚至连一封可直接发的 pitch 都帮你写好。

---

## 解决什么问题

PR 建联老大难的三件事，这个工具替你干掉：

1. **不知道去哪找靠谱的记者** —— 美国上千家媒体，谁真在写你这个赛道？
2. **pitch 千篇一律、发了没人回** —— 不针对记者近期报道，自然石沉大海
3. **联系方式难拿** —— 尤其 WSJ/NYT 这种大刊

## 谁用

你或同事。**每个品牌一个配置可复用**，换品牌只要换一份输入。

---

## 怎么用（4 步）

### 1. 装

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"
```

### 2. 配 key（一次性）

```bash
cp .env.example .env   # 用编辑器填入你的 key（别贴在对话里）
```

| Key | 必填 | 用途 |
|---|---|---|
| `OPENROUTER_API_KEY` | ✅ | 打分 / 分层 / 写 pitch |
| `NEWSAPI_AI_KEY` | ✅ | 主数据源（免费档 2000 搜/月，近 30 天）。注册：https://newsapi.ai/ |
| `APODEX_API_KEY` | 可选 | 深挖联系方式（慢，默认不跑） |
| `QUERIT_API_TOKEN` / `BRAVE_API_KEY` | 可选 | 网搜补召 |

缺可选 key 不影响主流程。

### 3. 把品牌材料丢给 AI 整理（**这一步 AI 帮你做**）

品牌信息天然零散（官网链接、一句话定位、一份 PDF、几句口头描述）。**你不用自己写配置文件** ——
把零散材料丢给 AI，AI 会读全部来源、整理成规整的 `brands/<brand>.yaml`、**给你过目确认后**才开搜（只写材料里真实有的，不编造）。

> 详细流程 + 字段说明见 **[BRAND_INTAKE.md](BRAND_INTAKE.md)**。

### 4. 跑

```bash
journofinder campaign brands/<brand>.yaml
#  → reports/<brand>.html / .csv / .md
```

慢的深挖默认走异步（见下方「进阶」），主报告几分钟出。

---

## 你得到什么

一份**两层结构**的报告（HTML 看着舒服 / CSV 直连 Google Sheet / Markdown）：

**📰 Layer 1 — 媒体清单**：哪些媒体相关 + 为什么相关
| 媒体 | 层级 | 记者数 | 含强推 | 为什么相关 |
|---|---|---|---|---|
| The Wall Street Journal | Tier-1 主流 | 1 | | 主流大刊，AI 报道权威…近期相关报道：… |
| TechCrunch | 科技媒体 | 1 | 🟢 | 科技垂直媒体，AI/agent 报道核心阵地… |

**👤 Layer 2 — 记者花名册 + 明细**：先一张花名册（记者 / 媒体 / 层级 / Tier / score / Email 一眼扫完），再每个记者的：
- **为什么是他**（分层理由）
- **1-3 个 pitch angle**（钩住他近期某篇具体报道）
- **一封可直接发的完整 pitch**（subject + body，"为什么他一定要报道你"）

分层规则：
- 🟢 **Tier A** —— 高相关高置信，**强烈推荐建联**
- 🟡 **Tier B** —— 中度相关，**可建联**（recall 优先，宁可多收不漏）
- ⛔ **drop** —— 内容农场 / PR 通稿 / 竞品自家媒体，自动剔除（不进报告，但可手动捞回）

联系方式：优先用文章署名里的真实邮箱（如 `sarah_perez@techcrunch.com`），拿不到的按媒体规则推断并标注。

---

## 进阶

```bash
# 先秒出名单（跳过慢的深挖），之后异步补 verified 联系方式
journofinder campaign brands/<brand>.yaml --no-deepdive
journofinder enrich <search_id> brands/<brand>.yaml   # 后台深挖 Tier-A（慢）
journofinder show <search_id> brands/<brand>.yaml     # 重渲报告

# 把被 drop 的记者手动捞回成 B（不会被重跑覆盖）
journofinder tier <search_id> <journalist_id> B
```

调参（在 `brands/<brand>.yaml` 的 `discovery` / `tiering` 段）：
- `min_score` 调低（如 40）= 多收二级媒体；调高 = 只留高相关
- `sort_by: sourceImportance` = 把 WSJ/NYT/Bloomberg 等大刊顶上来
- `pages` = 翻几页扩大池子

---

## 成本与限制

- **成本**：单次 campaign 约 **$0.5–3**（发现走免费档、打分用便宜模型、深挖按上限受控）
- **时间窗口**：NewsAPI 免费档只索引近 **~30 天**（要 60/90 天需升级付费档）
- **paywall**：不影响发现 —— WSJ/NYT 等大刊的记者署名照常拿得到（只有正文素材会少）

---

## 设计参考

栈复用自 [sd-pitchfinder](https://github.com/SD-Launchpad/sd-pitchfinder)（创作者发现 + pitch）与 [sd-pulse](https://github.com/SD-Launchpad/sd-pulse)（NewsAPI.ai 媒体监控）。
核心区别：pitchfinder 靠 RSS feed 发现「创作者」，**journofinder 靠文章署名反查「记者」** —— 用品牌关键词查近期文章，谁在密集报道这个领域，谁就是目标。

# 链接爬虫新闻源（Link Crawler News）

用户配置 URL 列表（如财联社电报页、华尔街见闻快讯页），每次个股分析时由 `LinkCrawlerService`
实时抓取页面中的新闻链接，去重、时效过滤后合并进 LLM 新闻上下文，增强模型对最新消息的感知。

## 功能说明

- 每个配置源按市场标签（`cn` / `hk` / `us` / `global`）过滤，`global` 源对所有市场生效。
- 每次个股分析实时抓取（不使用缓存池），不命中则静默跳过，不影响主分析流程。
- 抓取到的条目按 URL 去重（批次内 + 该股票历史入库记录），并持久化到 `crawl_news_items` 表。
- 支持 RSS/Atom feed 自动识别；普通 HTML 页面优先用 bs4 提取 `<a>` 新闻链接（无 bs4 时回退正则）。

## 配置格式与示例

配置项位于 `.env`，格式为 `标签|市场|URL`，多个源用英文逗号分隔：

```
LINK_CRAWL_SOURCES=财联社热闻|cn|https://www.cls.cn/telegraph,华尔街见闻快讯|cn|https://wallstreetcn.com/live/global
LINK_CRAWL_TIMEOUT_SEC=10
LINK_CRAWL_MAX_ITEMS_PER_SOURCE=20
LINK_CRAWL_MAX_AGE_HOURS=48
LINK_CRAWL_ENABLED=true
```

- `LINK_CRAWL_SOURCES`：源列表。支持三段（标签|市场|URL）、两段（标签|URL，市场视为
  `global`）、一段（裸 URL，标签取 hostname，市场视为 `global`）。市场取值非法时回退
  `global`；URL 校验不通过（见下方 SSRF 防护）或条目畸形时整条跳过。
- `LINK_CRAWL_TIMEOUT_SEC`：单个源拉取超时（秒，默认 10）。
- `LINK_CRAWL_MAX_ITEMS_PER_SOURCE`：单源最多提取条数（默认 20）。
- `LINK_CRAWL_MAX_AGE_HOURS`：新闻最大时效（小时，默认 48），条目带发布日期且超龄时丢弃。
- `LINK_CRAWL_ENABLED`：显式设为 `false` 可整体关闭（默认 `true`）。

## 行为细节

- **实时抓取**：每次个股分析（非 Agent 分支的 Step 4.6）对适用源发起 HTTP 请求，单页限制
  2MB、最多 5 次重定向，禁用代理。
- **URL 去重**：先查询该股票在 `crawl_news_items` 表的历史 URL，再叠加本次批次内去重，
  同一 URL 不会重复进入上下文。
- **时效过滤**：能从 HTML（锚点附近 `<time>` / 日期文本）或 feed（pubDate）解析出发布时间的
  条目才会做时效过滤；解析不到日期的条目不拦截。
- **持久化表 `crawl_news_items`**：字段含 `query_id`、`code`、`name`、`source_label`、
  `market`、`title`、`snippet`、`url`（唯一约束）、`published_date`、`fetched_at`。
  保存为幂等语义：URL 重复时跳过，保存失败仅记日志。

## fail-open 语义

整个模块是纯增量、fail-open 的：

- 服务初始化失败 → pipeline 以无爬虫模式运行，不影响搜索/资讯/主分析。
- 单个源抓取失败 → 其余源正常贡献，异常不外抛。
- 整体失败 → `fetch_context` 返回 `None`，等同于未启用。
- 持久化失败 → 不影响上下文返回（仅记录日志）。

## SSRF 防护说明

对配置 URL 与重定向后的最终 URL 做最小 SSRF 防护：

- 仅允许 `http` / `https` 完整 URL，禁止带账号密码。
- 拒绝回环（`127.0.0.1`、`localhost`、`*.local`）、内网/私有、链路本地、保留、组播地址。
- hostname 为域名时做一次 DNS 解析检查，任一解析结果命中内网即拒绝。

## 与现有新闻源的差异

| 模块 | 数据来源 | 触发方式 | 特点 |
| --- | --- | --- | --- |
| `search_service`（搜索引擎） | Bocha / Tavily / Brave 等搜索 API | 每次个股分析的多维度情报搜索 | 搜索引擎索引，广而泛，可跨源 |
| `intelligence_service`（本地资讯池） | 用户配置的资讯源（含 NewsNow / RSS） | 后台定时拉取入库，分析时读取 | 有调度、生命周期与清理策略，读本地池 |
| 本爬虫模块（link crawler） | 用户配置的页面 URL 列表 | 每次个股分析实时抓取 | 实时性最强，内容为页面可见新闻链接，数量受单源上限约束 |

# FlightWatch

国际机票价格监控。抓取 → 存历史 → 判断贵贱。

## 为什么这么设计

机票监控 95% 的工作是确定性的：定时抓、存历史、比价格。这部分**不该交给 LLM**——
同样的输入必须给出同样的结论，而且每小时跑一次模型既贵又不稳定。

所以本项目是纯确定性的抓取与分析管线。模型的位置在管线**之外**（见「下一步」）。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 编辑 config.yaml 填入你的航线
.venv/bin/python -m flightwatch watch                    # 抓一轮入库
.venv/bin/python -m flightwatch report --html report.html # 看结论
```

## 配置

```yaml
defaults:
  currency: USD     # 必须显式指定，否则 Google 会按地区推断，价格单位会漂移
  seat: economy
  passengers: 1
  keep_top: 8       # 每条航线每天最多存几档报价
  # 刻意不设 max_stops：实测限制中转反而会错过更便宜的票
  # （STL→PVG 限 1 转要贵 375 刀，限直飞则一班都没有）

watches:
  - name: LAS-SGN           # 唯一标识。改名 = 新开一条历史线，别随便改
    from: LAS
    to: SGN
    trip: one-way
    dates: [2026-12-16, 2026-12-17]
```

当前配置是 **4 个出发地 × 5 个目的地 = 20 条航线**：

| | |
|---|---|
| 出发 | LAS 拉斯维加斯 · SFO 旧金山 · LAX 洛杉矶 · ORD 芝加哥 |
| 到达 | SGN 胡志明 · HAN 河内 · DAD 岘港 · CGK 雅加达 · BKK 曼谷 |
| 日期 | 2026-12-16、2026-12-17（不限时段、不限中转） |

每轮 40 次查询，约 4 分钟。查询之间有 3 秒以上间隔，避免被 Google 限流。

## 数据从哪来

[fast-flights](https://github.com/AWeirdDev/flights)，即 Google Flights 的逆向接口。
选它是因为 2026 年 7 月 Amadeus 关停了免费自助开发者门户，Kiwi Tequila 也取消了自助注册——
个人项目已经拿不到免费的官方航班 API 了。

**代价是它随时会坏。** Google 一改版，解析就失败。因此：

- `requirements.txt` 里 **钉死了版本** `fast-flights==3.1.0`。升级前先手动验证。
- 抓取失败绝不静默吞掉：记进 `fetch_log` 表，退出码返回 1，报告顶部显示告警。
- 拿到响应但一条都解析不出来，会被当作**失败**而非「今天没航班」——
  这两者的区别，正是「监控正常」和「你以为在监控、其实已经死了三周」的区别。

## 关于「历史价格」

有两种历史，必须分清：

1. **我们自己攒的** —— 从你第一次跑 `watch` 开始记账，之前的查不到。
   攒够 3 次以上，报告才会给分位数结论。
2. **Google 的** —— Google 基于它自己的全量历史，直接告诉你当前价格
   `low` / `typical` / `high`。这份判断**从第一次跑就能拿到**，免费。

第 2 点解决了冷启动：刚跑起来没有自有历史时，报告会退回显示 Google 的判断，
而不是干巴巴一句「样本不足」。

> 实现细节：Google 页面上的 `Prices are currently <b>typical</b>` 是直接内嵌在
> HTML 里的，抓取时顺带解析，不额外发请求。但**精确的历史价格曲线**（每天多少钱）
> 是懒加载的，免费路径拿不到 —— 那份数据要走 SearchApi 付费接口
> （免费额度仅 100 次搜索）。目前没接。
>
> 代价：解析靠英文文案定位，所以查询语言被锁死为 `en-US`。Google 改文案就会失效，
> 届时 `price_level` 变成空值，但不影响价格抓取本身。

## 微信提醒

每 30 分钟扫一次全部 20 条航线，但**不是每轮都打扰你**。两条独立路径：

| 触发 | 条件 | 频率 |
|---|---|---|
| ✈️ **定时报告** | 到了 `report_times` 配置的时段 | 一天 5 次 |
| 静默 | 其余所有轮次 | —— |

**不做紧急推送**（`urgent_price` 留空即关闭）。跌破 `target_price` 的航线
只在报告里打 🎯 标记，不会单独打断你。

每 30 分钟仍照常抓取：一是攒历史，二是万一某个报告时段的触发被 GitHub 漏掉，
下一轮（30 分钟后）会自动补发，不会整段丢失。

**去重**：同一航线同样的价格不重复发；只有更便宜了才再发；
价格涨回阈值之上会清除记录，下次再跌破可以重新提醒。
定时报告按「本地日期 + 时段」去重，某轮被延迟或跳过，下一轮会自动补发。

**配置**（一次性）：
1. [sct.ftqq.com](https://sct.ftqq.com/) 微信扫码登录，拿到 SendKey
2. `gh secret set SERVERCHAN_SEND_KEY -R <owner>/<repo> --body "你的key"`
3. 本地预览：`python -m flightwatch notify --dry-run`

额度：免费 5 条/天，年付 39 元升到 1000 条/天，新用户送 7 天全功能试用。
一天 5 条定时报告正好卡满免费额度，紧急推送会超出 —— 需要会员。

## 24/7 运行

`.github/workflows/watch.yml` 每 30 分钟跑一轮。

### 为什么不用 GitHub 自带的 cron

**实测 `*/30` 在 11.5 小时内只触发了 1 次。** 结果是中部时间 09:00 的
定时报告整个错过 —— 那个时段根本没有运行去触发它。GitHub 官方只承认
"高峰期可能延迟"，实际上高频 schedule 会被大量丢弃。

所以主触发源改为**外部定时服务**，POST 到 `repository_dispatch`：

```
POST https://api.github.com/repos/<owner>/<repo>/dispatches

Headers:
  Accept: application/vnd.github+json
  Authorization: Bearer <PAT>
  X-GitHub-Api-Version: 2022-11-28

Body:
  {"event_type":"scan"}
```

PAT 需要该仓库的 **Contents: write** 权限
（[fine-grained token](https://github.com/settings/personal-access-tokens/new)）。

原 `*/30` cron 保留作兜底：外部服务万一挂了，还能偶尔跑一跑。

> 实测 GitHub runner 上抓 40 条只需约 3 分钟、零失败，并未被 Google 限速。
> 30 分钟间隔在性能上完全够用，瓶颈只在触发可靠性。

### 为什么报告时刻不写死成 cron

GitHub cron 只认 UTC、不处理夏令时。若把 5 个报告时刻硬编码成 UTC，
11 月换冬令时后本地时间会整体偏一小时。

改为：每 30 分钟触发一次，**「何时该发报告」交给代码判断**
（`notify.due_report_slot` 用 `zoneinfo` 按 `America/Chicago` 本地时间算）。
夏令时切换完全不用改配置，顺带还支持了 30 分钟粒度的紧急扫描。

### 历史数据存在哪

**`history/YYYY-MM-DD.csv` 是永久价格历史，每轮都提交进 git。**
每轮每条航线每个出发日记一行最低价（40 行 / 约 3 KB）。

为什么是 CSV 而不是直接提交 SQLite：git 存的是整份快照，而 SQLite 每次
写入都会重排页面，在 git 眼里整个文件都变了。实测每 30 分钟提交一次
数据库，**一周就让仓库涨到 4GB**。

CSV 是纯追加文本，git 增量压缩对它很有效；按天分文件后，昨天的文件
写完就永不改动。实测 336 次提交（7 天 × 48 轮）后 `.git` 经 gc 只有
**428 KB**，推算 90 天约 5 MB。

判断涨跌只需对比上一轮，所以这里只记最低价；完整报价明细留在 SQLite
里供报告使用，那部分超过 14 天自动清理（`--keep-days`）——
**但 CSV 历史永久保留，不受清理影响**。

### 数据库为什么不每轮提交

每 30 分钟提交一次二进制数据库，实测**一周就能让仓库涨到 4GB**
（git 存整份快照而非差异），一个月 77GB —— 远超 GitHub 5GB 硬上限。

所以：
- 数据库在轮次之间靠 **Actions 缓存**传递，不进 git
- 只有**真发了消息的轮次**才提交（一天约 5 次），由 `flightwatch watch`
  通过 `$GITHUB_OUTPUT` 的 `persist` 信号告诉工作流
- 缓存万一被 LRU 清掉，能从最近一次提交恢复
- 超过 14 天的原始报价自动清理（`--keep-days`），并 `VACUUM` 真正缩小文件

> 仓库是 public 的：私有仓库 Actions 免费额度 2000 分钟/月，
> 而每 30 分钟一轮需要约 7900 分钟/月。公开仓库 Actions 不限分钟。

不建议用本地 cron —— 笔记本合盖就断了。

## 数据表

- `offers` — 每次抓取的每条报价（价格/日期/航司/中转/路径）
- `fetch_log` — 每次抓取成败，用于判断监控本身是否还活着

历史攒够后，`report` 会给出分位数结论（如「便宜过 85% 的历史观测」）。
**少于 3 次观测时它会明说样本不足**，而不是硬给一个「史低」的假结论。

## 下一步（尚未实现）

1. **推送**：Telegram Bot 或 Server酱。当前只出本地报告。
2. **LLM 层**：自然语言配置行程、降价时结合历史给出「现在该不该买」的解释、
   不定目的地的灵活搜索。这些才是模型真正值钱的地方。
3. **国内航线**：Google Flights 看不到携程/飞猪的特价与会员价，
   需要 Selenium 对抗验证码和 IP 封禁，工作量比国际线大得多。

## 已知限制

- `duration_min` 是各航段飞行时间之和，**不含中转等待**（跨时区无法从本地时间可靠推算）。
- 到达时刻跨天会标 `+1d` / `+2d`。
- Google 会把同一行程同时放进 "best" 和 "cheapest" 分组，入库前已去重。
- `price_level` 只有 low/typical/high 三档，没有具体数值；想要精确历史曲线需接付费 API。

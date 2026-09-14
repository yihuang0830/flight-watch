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

跌破 `target_price`（当前 800 USD）时推送到微信，走 [Server酱](https://sct.ftqq.com/)。

**配置**（一次性）：
1. 访问 [sct.ftqq.com](https://sct.ftqq.com/) 微信扫码登录，拿到 SendKey
2. 仓库 Settings → Secrets and variables → Actions → New repository secret
   名称 `SERVERCHAN_SEND_KEY`，值粘贴 SendKey
3. 本地测试：`SERVERCHAN_SEND_KEY=xxx python -m flightwatch notify`

**免费额度只有 5 条/天**，所以做了两层保护：
- 一轮只发 **一条** 消息，把所有达标航线聚合进去（而不是 20 条航线发 20 条）
- 同一航线同样的价格不重复发；只有「更便宜了」才再发一次；
  价格涨回目标价之上会清除记录，下次再跌破可以重新提醒

不想被打扰：`python -m flightwatch watch --no-notify`
想看会发什么但不真发：`python -m flightwatch notify --dry-run`

## 24/7 运行

`.github/workflows/watch.yml` 已配好：每天两轮，把 `flights.db` 提交回仓库保存历史，
报告输出到 `docs/index.html`（开 GitHub Pages 即可网页查看）。免费、无需服务器。

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

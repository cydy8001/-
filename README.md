# Daily US Stock Email Push

每天早上自动筛选美股中总市值大于等于 400 亿美元的股票，并发送到邮箱。

## 1. 安装依赖

```bash
cd /workspaces/-
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置邮箱

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 编辑 `.env`：

- `EMAIL_SENDER`: 你的 Gmail 地址
- `EMAIL_APP_PASSWORD`: Gmail 应用专用密码（16 位）
- `EMAIL_RECEIVER`: 收件邮箱（默认是 `cydy8001@gmail.com`）
- `MARKET_CAP_MIN_BILLION`: 市值门槛（默认 40）

> 注意：Gmail 需要开启两步验证后，生成 App Password。

## 3. 手动测试

先 dry run（不发邮件）：

```bash
python3 us_stock_email_push.py --dry-run
```

发送一次真实邮件：

```bash
python3 us_stock_email_push.py
```

## 4. 设置每天早上自动发送（cron）

示例：每天早上 08:00 执行。

```bash
crontab -e
```

加入以下一行（按你的实际路径调整）：

```cron
0 8 * * * cd /workspaces/- && /workspaces/-/.venv/bin/python us_stock_email_push.py >> /workspaces/-/stock_push.log 2>&1
```

查看当前任务：

```bash
crontab -l
```

## 5. 说明

- 股票范围：基于 TradingView Scanner（NASDAQ/NYSE + Common Stock）。
- 策略条件：
	1. `Market Cap >= 40B USD`
	2. `Average Volume 10D >= 130% * Average Volume 30D`
	3. `Exchange in {NASDAQ, NYSE}`
	4. `Common stock only`
	5. `1-day High <= 90% of 52-week High`（即距 52W High 至少低 10%）
- 技术数据来源：TradingView Scanner 字段（用于 10/30 日均量、1 日最高价、52 周最高价）。
- 邮件内容：按市值从高到低排序，包含均量和 52 周高点距离等指标。
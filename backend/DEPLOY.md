# Applai 后端部署决策 — Fly.io vs Render

> 状态:草稿,等你拍板。LLM 匹配已上线等 key,这个是下一步。

## 先回答你上次的问题:Fly.io 和 Render 是什么

都是"把代码推上去就有一个公网 URL"的云托管平台,不用自己买服务器装系统。

- **Fly.io**:把你的应用跑在一个轻量虚拟机里,按秒计费。特点是**有真正的持久硬盘**(volume),SQLite 文件放上去重启不丢。
- **Render**:操作更傻瓜,git push 自动部署。免费档很慷慨,但**免费档没有持久硬盘**——机器休眠或重启,磁盘上的文件全部清空。

## 决定性的一条(为什么之前我说 Fly.io)

Applai 后端是 **FastAPI + SQLite**,数据库就是一个 `applai.db` 文件
(`db.py` 第 8 行:`APPLAI_DB` 环境变量可配,默认 `backend/applai.db`)。

| | Fly.io | Render 免费档 | Render 付费档 |
|---|---|---|---|
| 机器 | shared-cpu-1x / 256MB | 512MB / 0.1 CPU | Starter 及以上 |
| SQLite 数据 | ✅ volume 持久化,重启不丢 | ❌ 15 分钟无请求就休眠,磁盘清空,**数据全丢** | ✅ 需另加 disk |
| 费用(2026-10 最新价) | **约 $2.34/月**(机器 $2.19 + 1GB volume $0.15) | $0 | $7/月 + disk $0.25/GB/月 |
| 地区 | 有多伦多(yyz),延迟最低 | — | — |

Render 免费档不是"慢一点",是**每次休眠/重新部署都会把 applai.db 清零**——
职位队列、申请记录、匹配分数全没了。所以 Render 要么不用,要么 $7 起。
Fly.io 约 $2.34/月搞定,多伦多机房,离你最近。

**结论:推荐 Fly.io。** 除非你想为省 $2.34/月接受 Render $7/月(没理由),
或者以后量大了再迁 Postgres(代码里 SQL 已写成可零改动迁移,`db.py` 头注释有写)。

## 部署步骤(选 Fly.io 的话,10 分钟)

前提:装 `flyctl`,注册账号(要绑卡,无长期免费档,试用只有 2 小时 VM 或 7 天)。

```bash
cd backend

# 1. 建应用(会生成 fly.toml,region 选 yyz 多伦多)
fly launch --no-deploy

# 2. 建 1GB 持久盘(SQLite 住这儿)
fly volumes create applai_data --region yyz --size 1

# 3. 设 secret——DashScope key 放这里,不进 git、不在聊天里出现
fly secrets set \
  APPLAI_LLM_API_KEY="你自己的key" \
  APPLAI_LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1" \
  APPLAI_LLM_MODEL="qwen-turbo"
# 国际版 key 的话 BASE_URL 换 https://dashscope-intl.aliyuncs.com/compatible-mode/v1

# 4. 部署
fly deploy
```

### 需要新建的两个文件(内容已写好,部署时加上)

**Dockerfile**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py db.py ingest.py llm.py matcher.py ./
ENV APPLAI_DB=/data/applai.db
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

**fly.toml**
```toml
app = "applai-backend"          # fly launch 会按你的账号生成,改成实际名字
primary_region = "yyz"

[build]

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false    # 保持常开,extension 每天 8 点来取队列
  min_machines_running = 1

[[mounts]]
  source = "applai_data"
  destination = "/data"

[[vm]]
  size = "shared-cpu-1x"
  memory = "256mb"              # 如果 OOM,改 512mb(约 $3.19/月)
```

### 验证

```bash
curl https://applai-backend.fly.dev/health
# 期望: {..., "llm": true}  —— llm:true 说明 DashScope key 生效,语义匹配已激活
# 若为 false:检查 secret 是否设对、BASE_URL 与 key 版本(国内/国际)是否匹配
```

验证通过后,把 extension 设置里的 backend URL 改成 `https://你的应用名.fly.dev` 即可。

## 价格来源(2026-10-05 查)

- Fly.io 2026-10-01 起新价:shared-cpu-1x·256MB $2.19/月,volume $0.15/GB/月,
  北美 egress $0.02/GB。https://fly.io/pricing-update/
- Render:免费档 512MB 睡眠 15 分钟无请求、文件系统 ephemeral;
  disk 仅付费档可用,$0.25/GB/月;Starter $7/月起。

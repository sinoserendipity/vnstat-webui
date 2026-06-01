# vnstat Dashboard — OpenWrt 网络流量监控

从 OpenWrt 路由器的 vnstat 数据库读取流量数据，以漂亮的暗黑毛玻璃 Web 界面展示。

![UI](https://img.shields.io/badge/UI-Dark%20Glassmorphism-0ea5e9)
![Python](https://img.shields.io/badge/Python-3.11-3776AB)
![Source](https://img.shields.io/badge/Source-Local%20%7C%20Samba%20%7C%20WebDAV-22c55e)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 截图

![Dashboard](https://img.shields.io/badge/demo-network%20dashboard-blue)

---

## 快速开始

### 方式一：本地文件（最简单，推荐）

把 OpenWrt 上的 vnstat.db 复制到本地，直接映射进容器，免配 Samba/WebDAV：

```bash
# 1. 从 OpenWrt 复制数据库
# Windows: copy \\192.168.1.1\vnstat\vnstat.db C:\vnstat-dashboard\vnstat.db
# Linux:   scp root@192.168.1.1:/var/lib/vnstat/vnstat.db ./vnstat.db

# 2. 启动容器
docker run -d \
  --name vnstat-dashboard \
  -p 5050:5050 \
  --restart unless-stopped \
  -v vnstat-data:/app/data \
  -v /path/to/vnstat.db:/app/data/vnstat.db:ro \
  -e VNSTAT_SOURCE_MODE=true \
  ghcr.io/YOUR_GITHUB_USER/vnstat-dashboard:latest
```

打开 http://localhost:5050，勾选「使用本地文件」，保存即可。

### 方式二：Docker 远程获取

持久化设置，首次启动即配置好连接参数：

```bash
docker run -d \
  --name vnstat-dashboard \
  -p 5050:5050 \
  --restart unless-stopped \
  -v vnstat-data:/app/data \
  -e VNSTAT_HOST=192.168.1.1 \
  -e VNSTAT_USER=vnstat \
  -e VNSTAT_PASS=123456 \
  -e VNSTAT_SHARE=vnstat \
  ghcr.io/YOUR_GITHUB_USER/vnstat-dashboard:latest
```

### 方式三：直接运行

```bash
pip install -r requirements.txt
python app.py
```

打开 http://localhost:5050，点击右上角齿轮图标配置数据源。

---

## Docker 环境变量

| 变量 | 对应设置 | 示例 |
|------|---------|------|
| `VNSTAT_SOURCE_MODE` | 数据源模式 | `true`（本地）或 `false`（远程） |
| `VNSTAT_LOCAL_DB` | 本地数据库路径 | `/app/data/vnstat.db` |
| `VNSTAT_PROTOCOL` | 远程协议 | `samba` 或 `webdav` |
| `VNSTAT_HOST` | 主机地址 | `192.168.1.1` |
| `VNSTAT_PORT` | Samba 端口 | `445` |
| `VNSTAT_USER` | 用户名 | `vnstat` |
| `VNSTAT_PASS` | 密码 | `123456` |
| `VNSTAT_SHARE` | Samba 共享名 | `vnstat` |
| `VNSTAT_FILE` | 数据库文件名 | `vnstat.db` |
| `VNSTAT_WEBDAV_URL` | WebDAV 完整 URL | `http://192.168.1.1/vnstat.db` |
| `VNSTAT_WEBDAV_PORT` | WebDAV 端口 | `80` |

环境变量仅在首次启动或 `settings.json` 不存在时生效。

### 时区设置

今日流量数据基于服务器本地时间判断，请在 Docker 启动时设置正确的时区：

```bash
docker run -d ... \
  -e TZ=Asia/Shanghai \
  ghcr.io/YOUR_GITHUB_USER/vnstat-dashboard:latest
```

常用时区：
| 时区 | 地区 | 设置值 |
|------|------|--------|
| 中国标准时间 | 北京/上海/香港 | `Asia/Shanghai` |
| 美国东部时间 | 纽约 | `America/New_York` |
| 美国太平洋时间 | 洛杉矶 | `America/Los_Angeles` |
| 日本标准时间 | 东京 | `Asia/Tokyo` |
| 英国时间 | 伦敦 | `Europe/London` |
| 欧洲中部时间 | 柏林/巴黎 | `Europe/Berlin` |

> 不设置 `TZ` 环境变量时，默认使用 UTC 时间。如果今日 RX/TX 显示 `--`，大概率是时区不匹配导致的。

---

## 配置

打开仪表盘后，点击右上角 **齿轮图标** 进入设置弹窗：

**数据源模式：**
- **本地文件** — 勾选复选框，直接读取映射到容器内的 vnstat.db，无需 Samba/WebDAV
- **远程获取** — 取消勾选，通过 Samba 或 WebDAV 拉取数据库

远程模式下可配置：协议、主机地址、端口、用户名/密码、共享名、WebDAV URL、数据库文件名。

> 密码不会明文暴露到前端（API 仅返回 `has_password: true`）。

---

## 功能

| 功能 | 说明 |
|------|------|
| 实时总览 | 今日/昨日/本月/历史总流量 |
| 实时流量（5分钟粒度） | 近 12 小时 5 分钟级精细折线图 |
| 24小时流量分布 | 跨天拼接的最近 24 小时折线图 |
| 日流量 | 近 60 天每天 RX/TX 全宽柱状图 |
| 月流量 | 近 24 个月趋势全宽柱状图 |
| TOP 20 排行 | 流量最高的日期排名 |
| 自动刷新 | 每 5 分钟自动更新 |
| 数据源切换 | 本地文件 / Samba / WebDAV 可选 |
| 接口切换 | 多网卡切换（eth0, wlan0 等） |

---

## OpenWrt 端配置

### Samba

```bash
opkg update
opkg install samba36-server
smbpasswd -a vnstat
```

编辑 `/etc/config/samba`，添加共享：
```
config 'sambashare'
    option 'name' 'vnstat'
    option 'path' '/var/lib/vnstat'
    option 'read_only' 'yes'
    option 'guest_ok' 'no'
```

### WebDAV

使用 uhttpd 或 lighttpd 提供 vnstat.db 的 HTTP 访问：
```
ln -s /var/lib/vnstat/vnstat.db /www/vnstat.db
```

然后在仪表盘设置中填入 `http://192.168.1.1/vnstat.db`。

---

## Docker 构建

```bash
# 本地构建
docker build -t vnstat-dashboard .

# 多平台构建（x86_64 + ARM64）
docker buildx build --platform linux/amd64,linux/arm64 -t vnstat-dashboard .
```

### GitHub Actions 自动构建

推送到 `main` 分支自动触发：

1. Push 到 GitHub 仓库
2. GitHub Actions 自动编译 Docker 镜像（x86_64 + ARM64 双架构）
3. 镜像推送到 `ghcr.io/YOUR_USER/vnstat-dashboard:latest`
4. 在任何支持 Docker 的设备上运行

---

## 技术栈

- **后端**: Python 3.11 + Flask + pysmb + gunicorn
- **前端**: Chart.js 4.5 + 纯 CSS 暗黑毛玻璃风格（backdrop-filter）
- **数据源**: 本地文件 / Samba (SMB/CIFS) / WebDAV (HTTP)，Web UI 可配置
- **部署**: Docker（多平台）/ Docker Compose / 裸机

---

## 协议

MIT License

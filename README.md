# ArchiveMate 📦

> 智能自动解压服务 — 监控文件夹，自动解压，Web 管理界面

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green)
![Vue3](https://img.shields.io/badge/Vue-3-brightgreen)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ✨ 功能特性

### 核心功能
- 📁 **多目录监控** — 同时监控多个文件夹，实时检测新压缩包
- 🔄 **自动解压** — 检测到压缩包后自动解压，支持后台持续运行
- 📂 **自定义输出路径** — 每个监控目录可配置独立解压路径
- ⏳ **下载感知** — 自动检测文件是否仍在下载中，等待完成后再解压
- ✅ **增量解压** — 已解压文件自动跳过，不重复处理
- 🗂️ **嵌套解压** — 支持最多 N 层嵌套压缩包自动递归解压
- 📦 **分卷识别** — 自动识别 `.z01/.part1.rar/.001` 等分卷格式，只处理主卷

### 密码支持
- 🔑 **密码遍历** — 自动尝试密码字典解压加密压缩包
- 📋 **密码管理** — Web 端管理密码库，支持批量导入
- 🏆 **命中率统计** — 记录每个密码的命中次数，高频密码优先尝试
- 🛡️ **密码耗尽保护** — 遍历所有密码均失败后，自动标记为"密码耗尽"状态，永久跳过该文件不再重复解压，节省资源和时间；如需重试可在历史记录中手动重试

### 格式支持
通过 [7-Zip-zstd](https://github.com/mcmilk/7-Zip-zstd) 引擎支持：
`.zip` `.7z` `.rar` `.tar` `.gz` `.bz2` `.xz` `.tgz` `.tar.gz` `.tar.bz2` 以及分卷格式

### Web 界面
- 📊 **仪表盘** — 实时统计、成功率环形图、最近任务列表
- 📜 **解压历史** — 分页查询、状态筛选、展开详情、一键重试
- 🔑 **密码管理** — 悬停查看明文、命中率进度条
- 📂 **文件浏览器** — 路径面包屑导航，支持快速跳转监控目录
- 📝 **实时日志** — WebSocket 推送，级别筛选，自动滚动
- ⚙️ **设置中心** — 所有参数 Web 端可配置

## 🚀 快速开始

### 前置要求
- Python 3.11+
- [7-Zip-zstd](https://github.com/mcmilk/7-Zip-zstd) 二进制（`7zzs`）

### 直接运行

```bash
git clone https://github.com/shuichang/auto-extractor.git
cd auto-extractor

# 安装依赖
pip install -r backend/requirements.txt

# 启动服务（默认端口 3088）
APP_BASE_DIR=$(pwd) SEVENZIP_PATH=./backend/bin/7zzs python3 backend/main.py
```

访问 http://localhost:3088

### Docker Compose

```bash
git clone https://github.com/shuichang/auto-extractor.git
cd auto-extractor
docker-compose up -d
```

### systemd 后台服务

```bash
# 复制服务文件
sudo cp deploy/archivemate.service /etc/systemd/system/

# 修改 User 和路径后启用
sudo systemctl daemon-reload
sudo systemctl enable --now archivemate

# 查看状态
sudo systemctl status archivemate
```

## ⚙️ 配置

所有配置均可在 Web 界面 **设置** 页面修改，也可通过环境变量覆盖：

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `APP_BASE_DIR` | 数据目录根路径 | `/home/shuichang/auto-extractor` |
| `SEVENZIP_PATH` | 7zzs 可执行文件路径 | 自动检测 |

| 设置项 | 说明 | 默认值 |
|--------|------|--------|
| `check_interval` | 定时扫描间隔（秒） | 60 |
| `max_depth` | 最大嵌套解压深度 | 3 |
| `concurrent_tasks` | 并发解压任务数 | 2 |
| `stability_wait` | 文件稳定性检测等待秒数 | 3 |
| `sevenzip_path` | 7zip 路径 | `/usr/local/bin/7zzs` |

## 📁 目录结构

```
auto-extractor/
├── backend/
│   ├── main.py          # FastAPI 后端主程序
│   ├── bin/7zzs         # 7-Zip-zstd 二进制
│   ├── frontend/        # 构建产物（Docker 用）
│   └── requirements.txt
├── frontend/
│   └── index.html       # Vue3 单页应用
├── data/
│   └── archivemate.db   # SQLite 数据库
├── logs/
│   ├── archivemate.log  # 应用日志
│   └── host.log         # systemd 日志
├── docker-compose.yml
└── README.md
```

## 🔌 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/dashboard` | 统计概览 |
| GET/POST | `/api/watch-dirs` | 监控目录管理 |
| GET | `/api/watch-dirs/{id}/scan` | 手动触发扫描 |
| GET | `/api/history` | 解压历史（分页） |
| POST | `/api/history/{id}/retry` | 重试失败任务 |
| GET/POST/DELETE | `/api/passwords` | 密码管理 |
| GET/PUT | `/api/settings` | 设置读写 |
| GET | `/api/logs` | 日志查询 |
| GET | `/api/files` | 文件浏览 |
| WS | `/ws/logs` | 实时日志推送 |

## 🛠️ 技术栈

- **后端**: Python 3.11 + FastAPI + SQLite + watchdog
- **前端**: Vue3 (CDN) + TailwindCSS + Axios
- **解压引擎**: [7-Zip-zstd 23.01](https://github.com/mcmilk/7-Zip-zstd)
- **部署**: systemd / Docker Compose

## 📄 License

MIT

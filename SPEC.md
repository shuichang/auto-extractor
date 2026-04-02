# ArchiveMate - 智能自动解压服务

## 项目概述
监控本地文件夹，自动解压压缩包，支持嵌套、分卷、密码遍历、增量解压，提供精美 Web UI 和详细日志。

## 功能列表

### 核心功能
1. **多目录监控** - 同时监控多个文件夹的新增压缩包
2. **自动解压** - 检测到新压缩包后自动解压
3. **自定义解压路径** - 每个监控目录可配置独立输出路径
4. **增量解压** - 已解压文件跳过，未完成下载文件等待完成后解压
5. **嵌套解压** - 支持二级甚至三级嵌套压缩
6. **分卷解压** - 自动识别并合并 .z01/.z02 或 .part1.rar 等分卷
7. **密码遍历** - 内置密码字典 + 自定义密码列表，遍历尝试解压
8. **压缩码管理** - Web 端管理常用密码，保存密码历史

### 文件识别
- 支持格式：`.zip`, `.7z`, `.rar`, `.tar`, `.gz`, `.bz2`, `.xz`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.tgz`, `.tbz2`, `.txz`, `.001`
- 增量识别：已解压同名文件 + 同名 .done 标记跳过
- 分卷识别：.z01/.z02, .part1.rar, .rar.001 等

### 后台运行
- 无需守护进程，docker-compose 后台运行
- 文件监控 + 定时扫描双保险
- 任务队列，支持并发解压

### Web UI
- 仪表盘：实时任务状态、统计信息
- 监控目录管理：增删改监控路径
- 解压历史：按时间/状态筛选
- 密码管理：增删密码、导入密码本
- 设置中心：全局参数配置
- 日志查看：实时日志流、失败详情

### 日志功能
- 成功日志：文件名、路径、大小、耗时
- 失败日志：文件名、错误原因、堆栈
- 实时日志流 WebSocket 推送
- 日志持久化，可按日期筛选

## 技术栈
- **后端**：Python 3.11 + FastAPI (异步)
- **前端**：Vue3 + Vite + TailwindCSS
- **数据库**：SQLite（通过 SQLModel）
- **文件监控**：watchdog
- **压缩处理**：7z（命令行）+ Python zipfile/rarfile/py7zr
- **任务队列**：FastAPI BackgroundTasks + APScheduler

## 数据模型
- **WatchDir**：监控目录路径、输出路径、启用状态、创建时间
- **ArchiveHistory**：原始路径、输出路径、状态(success/failed/pending)、文件大小、密码、错误信息、创建时间
- **Password**：密码明文、用途标签、命中次数、创建时间
- **TaskLog**：任务ID、级别(info/warning/error)、消息、时间

## API 设计
- `GET /api/dashboard` - 统计数据
- `GET/POST /api/watch-dirs` - 监控目录管理
- `GET/POST /api/history` - 解压历史
- `GET/POST /api/passwords` - 密码管理
- `GET /api/logs` - 日志查询
- `POST /api/tasks/scan` - 手动触发扫描
- `WebSocket /ws/logs` - 实时日志流

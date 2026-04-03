"""
ArchiveMate v2 - 智能自动解压服务（重构版）
"""
import os
import sys
import re
import asyncio
import logging
import json
import time
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import sqlite3

# ============== 配置 ==============
BASE_DIR = Path(os.environ.get("APP_BASE_DIR", "/home/shuichang/auto-extractor"))
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
DB_PATH = DATA_DIR / "archivemate.db"

# 7zip路径优先级：环境变量 > 本地bin > 系统
_SEVENZIP_CANDIDATES = [
    os.environ.get("SEVENZIP_PATH", ""),
    str(BASE_DIR / "backend" / "bin" / "7zzs"),
    str(BASE_DIR / "bin" / "7zzs"),
    "/usr/local/bin/7zzs",
    "/usr/bin/7zz",
    "/usr/bin/7z",
]

def _find_sevenzip() -> str:
    for p in _SEVENZIP_CANDIDATES:
        if p and Path(p).exists():
            return p
    return "7z"

SEVENZZ_PATH = _find_sevenzip()

# ============== 日志 ==============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "archivemate.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("archivemate")

# ============== 数据库 ==============
db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # WAL 模式允许读写并发
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")  # 10秒等待
    return conn

def init_db():
    with db_lock:
        conn = get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS watch_dirs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_path TEXT NOT NULL UNIQUE,
                    output_path TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    auto_delete INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS archive_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_path TEXT NOT NULL,
                    output_path TEXT,
                    status TEXT DEFAULT 'pending',
                    file_size INTEGER DEFAULT 0,
                    password_used TEXT,
                    error_message TEXT,
                    extracted_size INTEGER DEFAULT 0,
                    file_count INTEGER DEFAULT 0,
                    duration_seconds REAL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS passwords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    password TEXT NOT NULL UNIQUE,
                    tag TEXT DEFAULT '',
                    hit_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT DEFAULT 'info',
                    message TEXT NOT NULL,
                    task_id TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS done_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL UNIQUE,
                    archive_id INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS pending_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL UNIQUE,
                    watch_dir_id INTEGER NOT NULL,
                    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_size INTEGER DEFAULT 0,
                    last_mtime REAL DEFAULT 0,
                    retry_count INTEGER DEFAULT 0
                );
            """)
            # 迁移旧库，添加新字段
            for col_sql in [
                "ALTER TABLE archive_history ADD COLUMN extracted_size INTEGER DEFAULT 0",
                "ALTER TABLE archive_history ADD COLUMN file_count INTEGER DEFAULT 0",
                "ALTER TABLE archive_history ADD COLUMN duration_seconds REAL DEFAULT 0",
            ]:
                try:
                    conn.execute(col_sql)
                except:
                    pass
            conn.execute("""
                INSERT OR IGNORE INTO settings (key, value) VALUES
                ('max_depth', '3'),
                ('check_interval', '60'),
                ('stability_wait', '5'),
                ('concurrent_tasks', '2'),
                ('sevenzip_path', '/usr/local/bin/7zzs'),
                ('password_dict', 'password\n123456\n12345678\nqwerty\nadmin\n000000\n111111\n123123\n123456789\n1234567890\nabc123\n')
            """)
            # 服务重启：将遗留的 processing 记录重置为 failed
            conn.execute(
                "UPDATE archive_history SET status='failed', error_message='服务重启，任务中断', completed_at=CURRENT_TIMESTAMP WHERE status='processing'"
            )
            conn.commit()
        finally:
            conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()

def get_sevenzip_path() -> str:
    env_path = os.environ.get("SEVENZIP_PATH", "")
    if env_path and Path(env_path).exists():
        return env_path
    db_path = get_setting("sevenzip_path", "")
    if db_path and Path(db_path).exists():
        return db_path
    return SEVENZZ_PATH


# ============== 压缩包识别 ==============
ARCHIVE_EXTENSIONS = {
    '.zip', '.7z', '.rar', '.tar', '.gz', '.bz2', '.xz',
    '.tgz', '.tbz2', '.txz',
    '.z01', '.001',
}

def is_archive(path: str) -> bool:
    p = Path(path)
    name = p.name
    name_lower = name.lower()
    if name.startswith('.') or name_lower.endswith('.tmp') or name_lower.endswith('.crdownload') or name_lower.endswith('.download') or name_lower.endswith('.part'):
        return False
    ext = p.suffix.lower()
    if ext in ARCHIVE_EXTENSIONS:
        return True
    # tar.gz, tar.bz2, tar.xz
    if name_lower.endswith('.tar.gz') or name_lower.endswith('.tar.bz2') or name_lower.endswith('.tar.xz'):
        return True
    # .rar (包括 .part1.rar)
    if name_lower.endswith('.rar'):
        return True
    return False

def is_volume_secondary(path: str) -> bool:
    """判断是否是非首个分卷文件（需跳过）"""
    name = Path(path).name.lower()
    # .z02, .z03... (not .z01)
    if re.match(r'.*\.z\d{2}$', name) and not name.endswith('.z01'):
        return True
    # .part2.rar, .part02.rar (number > 1)
    m = re.search(r'\.part0*(\d+)\.rar$', name)
    if m and int(m.group(1)) > 1:
        return True
    # .002, .003... (not .001)
    if re.match(r'.*\.\d{3,}$', name) and not re.search(r'\.0+1$', name):
        return True
    # .rar.002, .rar.003...
    if re.match(r'.*\.rar\.\d{3,}$', name) and not name.endswith('.rar.001'):
        return True
    return False

def get_sibling_volumes(path: str) -> List[str]:
    """返回与主卷同属一套分卷的所有文件路径（包括主卷本身），找不到规律返回空列表"""
    p = Path(path)
    name_lower = p.name.lower()
    parent = p.parent
    volumes = []

    # .7z.001 / .zip.001 / .rar.001 / .001 系列
    m = re.match(r'^(.*?)\.(\w+)\.0+1$', name_lower)
    if m:
        base_name = p.name[:m.end(1)]  # 保持原始大小写
        ext = m.group(2)
        i = 1
        while True:
            cand = parent / f"{base_name}.{ext}.{i:03d}"
            if cand.exists():
                volumes.append(str(cand))
                i += 1
            else:
                break
        return volumes

    # .part01.rar / .part1.rar 系列
    m2 = re.search(r'(\.part0*1\.rar)$', name_lower)
    if m2:
        prefix = p.name[:len(p.name) - len(m2.group(1))]
        i = 1
        while True:
            # try both .part1.rar and .part01.rar style
            found = False
            for fmt in [f'.part{i}.rar', f'.part{i:02d}.rar', f'.part{i:03d}.rar']:
                cand = parent / (prefix + fmt)
                if cand.exists():
                    volumes.append(str(cand))
                    found = True
                    break
            if not found:
                break
            i += 1
        return volumes

    # .z01 系列
    if name_lower.endswith('.z01'):
        base_name = p.name[:-4]  # 去掉 .z01
        volumes.append(str(p))
        i = 2
        while True:
            cand = parent / f"{base_name}.z{i:02d}"
            if cand.exists():
                volumes.append(str(cand))
                i += 1
            else:
                break
        return volumes

    return []

def all_volumes_stable(archive_path: str, stability_wait: int = 3) -> bool:
    """检查分卷压缩的所有分卷文件是否都已下载完成（稳定）"""
    volumes = get_sibling_volumes(archive_path)
    if not volumes:
        return True  # 非分卷，不检查
    for vol in volumes:
        vp = Path(vol)
        if not vp.exists():
            logger.info(f"分卷尚未就绪: {vp.name}")
            return False
        try:
            s1 = vp.stat()
            time.sleep(stability_wait)
            s2 = vp.stat()
            if s1.st_size != s2.st_size or s1.st_mtime != s2.st_mtime or s2.st_size == 0:
                logger.info(f"分卷仍在写入: {vp.name}")
                return False
        except Exception:
            return False
    return True

def is_file_stable(path: str, wait_seconds: int = 5) -> bool:
    """检测文件是否稳定（未在下载中）"""
    p = Path(path)
    try:
        if p.stat().st_size == 0:
            return False
        stat1 = p.stat()
        time.sleep(wait_seconds)
        stat2 = p.stat()
        return stat1.st_size == stat2.st_size and stat1.st_mtime == stat2.st_mtime
    except:
        return False

def is_quick_stable(path: str) -> bool:
    """快速检测（不等待），基于文件大小"""
    p = Path(path)
    try:
        return p.stat().st_size > 0
    except:
        return False

def get_all_passwords() -> List[str]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT password FROM passwords ORDER BY hit_count DESC").fetchall()
        custom = [r[0] for r in rows]
        dict_val = conn.execute("SELECT value FROM settings WHERE key='password_dict'").fetchone()
        dict_passwords = []
        if dict_val:
            dict_passwords = [p.strip() for p in dict_val[0].split('\n') if p.strip()]
        # custom passwords first (higher hit rate), then dict
        seen = set()
        result = []
        for p in custom + dict_passwords:
            if p not in seen:
                seen.add(p)
                result.append(p)
        return result
    finally:
        conn.close()

# ============== 解压引擎 ==============
class ExtractionResult:
    def __init__(self, success: bool, output_path: str = "", error: str = "",
                 password_used: str = "", file_count: int = 0, extracted_size: int = 0):
        self.success = success
        self.output_path = output_path
        self.error = error
        self.password_used = password_used
        self.file_count = file_count
        self.extracted_size = extracted_size

class ArchiveExtractor:
    def __init__(self, archive_path: str, output_dir: str, passwords: List[str] = None):
        self.archive_path = Path(archive_path)
        self.output_dir = Path(output_dir)
        self.passwords = passwords if passwords is not None else get_all_passwords()

    def _get_primary_archive(self) -> Path:
        """对于分卷压缩，返回主卷文件路径"""
        name_lower = self.archive_path.name.lower()
        parent = self.archive_path.parent
        # .z01 -> look for .zip
        if name_lower.endswith('.z01'):
            base = self.archive_path.stem
            for ext in ['.zip', '.ZIP']:
                cand = parent / (base + ext)
                if cand.exists():
                    return cand
            return self.archive_path
        # .part01.rar / .part1.rar -> use as-is (7z handles it)
        if re.search(r'\.part0*1\.rar$', name_lower):
            return self.archive_path
        # .rar.001 -> use as-is
        if name_lower.endswith('.rar.001'):
            return self.archive_path
        # .001 -> use as-is (7z handles multi-volume)
        if name_lower.endswith('.001'):
            return self.archive_path
        return self.archive_path

    def _try_extract(self, password: str = "") -> ExtractionResult:
        primary = self._get_primary_archive()
        stem = primary.stem
        # Strip common suffixes from stem for output dir name
        for suffix in ['.part01', '.part001', '.part1', '.rar']:
            if stem.lower().endswith(suffix):
                stem = stem[:len(stem)-len(suffix)]
                break
        output_subdir = self.output_dir / stem
        output_subdir.mkdir(parents=True, exist_ok=True)

        sz_path = get_sevenzip_path()
        cmd = [sz_path, 'x', str(primary), f'-o{output_subdir}', '-y', '-sccUTF-8']
        # 始终传入 -p 参数（即使为空），避免 Rar5 加密文件等待交互式密码输入卡死
        cmd.append(f'-p{password}')

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3600,
                stdin=subprocess.DEVNULL
            )
            stdout = result.stdout + result.stderr
            stdout_lower = stdout.lower()

            # 密码错误的各种表现
            if 'wrong password' in stdout_lower:
                return ExtractionResult(False, error="wrong password")
            if 'enter password' in stdout_lower:
                return ExtractionResult(False, error="wrong password")
            if result.returncode == 2 and ('encrypted' in stdout_lower or 'password' in stdout_lower):
                return ExtractionResult(False, error="wrong password")

            extracted_files = [f for f in output_subdir.rglob('*') if f.is_file()]
            total_size = sum(f.stat().st_size for f in extracted_files)

            if result.returncode == 0 or 'everything is ok' in stdout_lower:
                if total_size > 0 or len(extracted_files) > 0:
                    return ExtractionResult(
                        True, str(output_subdir),
                        password_used=password,
                        file_count=len(extracted_files),
                        extracted_size=total_size
                    )
                else:
                    return ExtractionResult(False, error="wrong password (empty output)")

            err = stdout.strip()[:500] if stdout.strip() else f"returncode={result.returncode}"
            return ExtractionResult(False, error=err)
        except subprocess.TimeoutExpired:
            return ExtractionResult(False, error="解压超时（1小时）")
        except Exception as e:
            return ExtractionResult(False, error=str(e))

    def extract(self) -> ExtractionResult:
        # Try without password first
        result = self._try_extract("")
        if result.success:
            return result
        if "wrong password" not in result.error.lower():
            return result

        # Try password list
        for pwd in self.passwords:
            r = self._try_extract(pwd)
            if r.success:
                self._bump_password_hit(pwd)
                return r
            if "wrong password" not in r.error.lower():
                return r

        return ExtractionResult(False, error=f"所有密码均失败（{len(self.passwords)}个）")

    def _bump_password_hit(self, pwd: str):
        try:
            with db_lock:
                conn = get_db()
                try:
                    conn.execute("UPDATE passwords SET hit_count=hit_count+1 WHERE password=?", (pwd,))
                    conn.commit()
                finally:
                    conn.close()
        except:
            pass


# ============== 日志广播 ==============
# 每个 WS 客户端独立的 Queue，保存弱引用集合
_ws_subscribers: list = []  # list of asyncio.Queue
_ws_subscribers_lock = threading.Lock()

def log_task(message: str, level: str = "info", task_id: str = ""):
    logger.info(f"[{level.upper()}] {message}")
    try:
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    "INSERT INTO task_logs (level, message, task_id) VALUES (?,?,?)",
                    (level, message, task_id)
                )
                conn.commit()
            finally:
                conn.close()
    except:
        pass
    # Broadcast to all WS clients (each has own queue)
    payload = {"level": level, "message": message, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with _ws_subscribers_lock:
        for q in list(_ws_subscribers):
            try:
                q.put_nowait(payload)
            except:
                pass

# ============== 任务处理 ==============
processing_files: set = set()
processing_lock = threading.Lock()
_semaphore: threading.Semaphore = None

def get_semaphore() -> threading.Semaphore:
    global _semaphore
    if _semaphore is None:
        n = int(get_setting("concurrent_tasks", "2"))
        _semaphore = threading.Semaphore(n)
    return _semaphore

def is_done(file_path: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM done_files WHERE file_path=?", (file_path,)).fetchone()
        return row is not None
    finally:
        conn.close()

def queue_archive(archive_path: str, watch_dir_id: int, output_path: str):
    """入队处理一个压缩包，跳过已完成的和次分卷"""
    if is_volume_secondary(archive_path):
        return
    if is_done(archive_path):
        logger.debug(f"跳过已处理: {archive_path}")
        return
    with processing_lock:
        if archive_path in processing_files:
            return
        processing_files.add(archive_path)
    threading.Thread(target=_worker, args=(archive_path, watch_dir_id, output_path), daemon=True).start()

def _worker(archive_path: str, watch_dir_id: int, output_path: str):
    sem = get_semaphore()
    sem.acquire()
    history_id = None
    try:
        p = Path(archive_path)
        # 分卷完整性检查：等所有分卷都下载完毕才开始解压
        volumes = get_sibling_volumes(archive_path)
        if volumes:
            all_ready = False
            for attempt in range(30):  # 最多等 30 * 10s = 5分钟
                missing = [v for v in volumes if not Path(v).exists()]
                if missing:
                    names = ', '.join(Path(v).name for v in missing)
                    log_task(f"等待分卷下载: {names}", "info")
                    sem.release()
                    time.sleep(10)
                    sem.acquire()
                    continue
                # 所有分卷存在，检查是否稳定（3秒内无变化）
                unstable = []
                for vol in volumes:
                    vp = Path(vol)
                    try:
                        s1 = vp.stat()
                        time.sleep(3)
                        s2 = vp.stat()
                        if s1.st_size != s2.st_size or s1.st_mtime != s2.st_mtime or s2.st_size == 0:
                            unstable.append(vp.name)
                    except Exception:
                        unstable.append(vp.name)
                if unstable:
                    names = ', '.join(unstable)
                    log_task(f"分卷仍在写入，等待: {names}", "info")
                    sem.release()
                    time.sleep(10)
                    sem.acquire()
                    continue
                all_ready = True
                break
            if not all_ready:
                log_task(f"分卷等待超时，放弃: {Path(archive_path).name}", "warning")
                _add_pending(archive_path, watch_dir_id)
                return
        else:
            # 单文件稳定性检测
            try:
                s1 = p.stat()
                time.sleep(3)
                s2 = p.stat()
                if s1.st_size != s2.st_size or s1.st_mtime != s2.st_mtime or s2.st_size == 0:
                    log_task(f"文件仍在写入，延迟处理: {p.name}", "warning")
                    _add_pending(archive_path, watch_dir_id)
                    return
            except Exception:
                return

        log_task(f"开始解压: {Path(archive_path).name}", "info")
        t_start = time.time()

        # 创建历史记录
        file_size = Path(archive_path).stat().st_size if Path(archive_path).exists() else 0
        with db_lock:
            conn = get_db()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO archive_history (original_path, status, file_size) VALUES (?,?,?)",
                    (archive_path, 'processing', file_size)
                )
                conn.commit()
                if cur.lastrowid == 0:
                    # Already exists (INSERT OR IGNORE skipped), reset status to processing
                    conn.execute(
                        "UPDATE archive_history SET status='processing', completed_at=NULL, error_message=NULL WHERE original_path=?",
                        (archive_path,)
                    )
                    conn.commit()
                    row = conn.execute("SELECT id FROM archive_history WHERE original_path=? ORDER BY id DESC LIMIT 1", (archive_path,)).fetchone()
                    history_id = row[0] if row else None
                else:
                    history_id = cur.lastrowid
            finally:
                conn.close()

        if history_id is None:
            return

        extractor = ArchiveExtractor(archive_path, output_path)
        result = extractor.extract()
        duration = time.time() - t_start

        with db_lock:
            conn = get_db()
            try:
                if result.success:
                    conn.execute(
                        """UPDATE archive_history SET status='success', output_path=?, password_used=?,
                           extracted_size=?, file_count=?, duration_seconds=?, completed_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (result.output_path, result.password_used, result.extracted_size,
                         result.file_count, duration, history_id)
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO done_files (file_path, archive_id) VALUES (?,?)",
                        (archive_path, history_id)
                    )
                    conn.commit()
                    log_task(f"解压成功: {Path(archive_path).name} → {result.output_path} ({result.file_count}个文件, {round(duration,1)}s)", "info")
                    # 深层解压
                    threading.Thread(
                        target=deep_extract,
                        args=(result.output_path, output_path, 1),
                        daemon=True
                    ).start()
                    # 自动删除
                    if watch_dir_id > 0:
                        _maybe_auto_delete(archive_path, watch_dir_id)
                else:
                    conn.execute(
                        """UPDATE archive_history SET status='failed', error_message=?,
                           duration_seconds=?, completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (result.error, duration, history_id)
                    )
                    conn.commit()
                    log_task(f"解压失败: {Path(archive_path).name} — {result.error}", "error")
            finally:
                conn.close()
    except Exception as e:
        logger.exception(f"处理异常: {archive_path}")
        log_task(f"处理异常: {Path(archive_path).name} — {str(e)}", "error")
        if history_id:
            with db_lock:
                conn = get_db()
                try:
                    conn.execute("UPDATE archive_history SET status='failed', error_message=?, completed_at=CURRENT_TIMESTAMP WHERE id=?", (str(e), history_id))
                    conn.commit()
                finally:
                    conn.close()
    finally:
        sem.release()
        with processing_lock:
            processing_files.discard(archive_path)

def _add_pending(file_path: str, watch_dir_id: int):
    p = Path(file_path)
    try:
        stat = p.stat()
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO pending_files (file_path, watch_dir_id, last_size, last_mtime)
                       VALUES (?,?,?,?)""",
                    (file_path, watch_dir_id, stat.st_size, stat.st_mtime)
                )
                conn.commit()
            finally:
                conn.close()
    except:
        pass

def _maybe_auto_delete(archive_path: str, watch_dir_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT auto_delete, output_path FROM watch_dirs WHERE id=?", (watch_dir_id,)).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        try:
            Path(archive_path).unlink()
            log_task(f"已删除原压缩包: {Path(archive_path).name}", "info")
        except Exception as e:
            log_task(f"删除原文件失败: {e}", "warning")

def deep_extract(directory: str, root_output: str, depth: int = 1):
    """递归处理解压出来的压缩包 — 只扫本次解压的子目录，不递归扫全部输出目录"""
    max_depth = int(get_setting("max_depth", "3"))
    if depth >= max_depth:
        return
    dir_path = Path(directory)
    if not dir_path.exists():
        return
    # 只遍历当前目录一层，对压缩包文件排队，对子目录再递归
    for item in dir_path.iterdir():
        if item.is_file() and is_archive(str(item)) and not is_volume_secondary(str(item)):
            if not is_done(str(item)):
                # 子压缩包解压到同目录下
                queue_archive(str(item), 0, str(dir_path))
        elif item.is_dir():
            deep_extract(str(item), root_output, depth + 1)


# ============== 文件监控 ==============
class ArchiveWatcher:
    def __init__(self):
        self.observers: Dict[int, Any] = {}

    def add_path(self, watch_path: str, watch_dir_id: int, output_path: str):
        if watch_dir_id in self.observers:
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class Handler(FileSystemEventHandler):
                def __init__(self, wid, opath):
                    self.wid = wid
                    self.opath = opath

                def on_created(self, event):
                    if not event.is_directory:
                        self._check(event.src_path)

                def on_modified(self, event):
                    if not event.is_directory:
                        self._check(event.src_path)

                def on_moved(self, event):
                    if not event.is_directory:
                        self._check(event.dest_path)

                def _check(self, path):
                    if is_archive(path) and is_quick_stable(path):
                        queue_archive(path, self.wid, self.opath)

            obs = Observer()
            obs.schedule(Handler(watch_dir_id, output_path), watch_path, recursive=True)
            obs.start()
            self.observers[watch_dir_id] = obs
            logger.info(f"开始监控: {watch_path}")
        except Exception as e:
            logger.error(f"监控失败 {watch_path}: {e}")

    def remove_path(self, watch_dir_id: int):
        obs = self.observers.pop(watch_dir_id, None)
        if obs:
            try:
                obs.stop()
                obs.join(timeout=5)
            except:
                pass
            logger.info(f"停止监控 id={watch_dir_id}")

watcher = ArchiveWatcher()

# ============== 定时扫描 ==============
def periodic_scan():
    while True:
        interval = int(get_setting("check_interval", "60"))
        time.sleep(interval)
        try:
            with db_lock:
                conn = get_db()
                try:
                    rows = conn.execute("SELECT id, watch_path, output_path FROM watch_dirs WHERE enabled=1").fetchall()
                finally:
                    conn.close()
            for row in rows:
                wid, watch_path, output_path = row[0], row[1], row[2]
                _scan_dir(wid, watch_path, output_path)
            # 检查 pending 队列
            _check_pending()
        except Exception as e:
            logger.error(f"定时扫描异常: {e}")

def _scan_dir(wid: int, watch_path: str, output_path: str):
    p = Path(watch_path)
    if not p.exists():
        return
    output_p = Path(output_path)
    for item in p.rglob("*"):
        # 跳过输出目录本身（避免把已解压内容里的压缩包重复处理）
        try:
            item.relative_to(output_p)
            continue  # item is inside output_path, skip
        except ValueError:
            pass
        if item.is_file() and is_archive(str(item)) and not is_volume_secondary(str(item)):
            if not is_done(str(item)):
                queue_archive(str(item), wid, output_path)

def _check_pending():
    """检查等待队列中的文件是否稳定了"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, file_path, watch_dir_id, last_size, last_mtime FROM pending_files").fetchall()
    finally:
        conn.close()

    for row in rows:
        pid, file_path, wid, last_size, last_mtime = row
        p = Path(file_path)
        if not p.exists():
            with db_lock:
                conn = get_db()
                try:
                    conn.execute("DELETE FROM pending_files WHERE id=?", (pid,))
                    conn.commit()
                finally:
                    conn.close()
            continue
        try:
            stat = p.stat()
            if stat.st_size == last_size and stat.st_mtime == last_mtime and stat.st_size > 0:
                # File is stable now
                with db_lock:
                    conn = get_db()
                    try:
                        wd = conn.execute("SELECT output_path FROM watch_dirs WHERE id=?", (wid,)).fetchone()
                    finally:
                        conn.close()
                output_path = wd[0] if wd else str(BASE_DIR / "extracted")
                with db_lock:
                    conn = get_db()
                    try:
                        conn.execute("DELETE FROM pending_files WHERE id=?", (pid,))
                        conn.commit()
                    finally:
                        conn.close()
                queue_archive(file_path, wid, output_path)
            else:
                # Update snapshot
                with db_lock:
                    conn = get_db()
                    try:
                        conn.execute("UPDATE pending_files SET last_size=?, last_mtime=? WHERE id=?",
                                     (stat.st_size, stat.st_mtime, pid))
                        conn.commit()
                    finally:
                        conn.close()
        except:
            pass

def restore_watchers():
    time.sleep(2)
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, watch_path, output_path FROM watch_dirs WHERE enabled=1").fetchall()
    finally:
        conn.close()
    for row in rows:
        watcher.add_path(row[1], row[0], row[2])


# ============== FastAPI App ==============
app = FastAPI(title="ArchiveMate", version="2.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=periodic_scan, daemon=True).start()
    threading.Thread(target=restore_watchers, daemon=True).start()

# ============== Pydantic 模型 ==============
class WatchDirCreate(BaseModel):
    watch_path: str
    output_path: str
    enabled: bool = True
    auto_delete: bool = False

class WatchDirOut(BaseModel):
    id: int
    watch_path: str
    output_path: str
    enabled: bool
    auto_delete: bool
    created_at: str

class PasswordCreate(BaseModel):
    password: str
    tag: str = ""

class PasswordOut(BaseModel):
    id: int
    password: str
    tag: str
    hit_count: int
    created_at: str

class LogOut(BaseModel):
    id: int
    level: str
    message: str
    task_id: Optional[str] = ""
    created_at: str

class HistoryItem(BaseModel):
    id: int
    original_path: str
    output_path: Optional[str] = None
    status: str
    file_size: int
    password_used: Optional[str] = None
    error_message: Optional[str] = None
    extracted_size: int = 0
    file_count: int = 0
    duration_seconds: float = 0
    created_at: str
    completed_at: Optional[str] = None

class HistoryPage(BaseModel):
    items: List[HistoryItem]
    total: int
    page: int
    page_size: int

class DashboardStats(BaseModel):
    total_archives: int
    success_count: int
    failed_count: int
    pending_count: int
    watch_dirs_count: int
    passwords_count: int
    today_success: int
    today_failed: int
    processing_count: int

# ============== API: Dashboard ==============
@app.get("/api/dashboard", response_model=DashboardStats)
def get_dashboard():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM archive_history").fetchone()[0]
        success = conn.execute("SELECT COUNT(*) FROM archive_history WHERE status='success'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM archive_history WHERE status='failed'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM archive_history WHERE status='pending'").fetchone()[0]
        processing = conn.execute("SELECT COUNT(*) FROM archive_history WHERE status='processing'").fetchone()[0]
        dirs_count = conn.execute("SELECT COUNT(*) FROM watch_dirs").fetchone()[0]
        pwd_count = conn.execute("SELECT COUNT(*) FROM passwords").fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        today_success = conn.execute(
            "SELECT COUNT(*) FROM archive_history WHERE status='success' AND completed_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]
        today_failed = conn.execute(
            "SELECT COUNT(*) FROM archive_history WHERE status='failed' AND completed_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]
    finally:
        conn.close()
    return DashboardStats(
        total_archives=total, success_count=success, failed_count=failed,
        pending_count=pending, watch_dirs_count=dirs_count, passwords_count=pwd_count,
        today_success=today_success, today_failed=today_failed, processing_count=processing
    )

# ============== API: Watch Dirs ==============
@app.get("/api/watch-dirs", response_model=List[WatchDirOut])
def list_watch_dirs():
    conn = get_db()
    try:
        rows = conn.execute("SELECT id,watch_path,output_path,enabled,auto_delete,created_at FROM watch_dirs ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return [WatchDirOut(id=r[0], watch_path=r[1], output_path=r[2], enabled=bool(r[3]), auto_delete=bool(r[4]), created_at=r[5]) for r in rows]

@app.post("/api/watch-dirs", response_model=WatchDirOut)
def create_watch_dir(data: WatchDirCreate):
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO watch_dirs (watch_path,output_path,enabled,auto_delete) VALUES (?,?,?,?)",
                (data.watch_path, data.output_path, 1 if data.enabled else 0, 1 if data.auto_delete else 0)
            )
            conn.commit()
            row = conn.execute("SELECT id,watch_path,output_path,enabled,auto_delete,created_at FROM watch_dirs WHERE watch_path=?", (data.watch_path,)).fetchone()
        except sqlite3.IntegrityError:
            conn.close()
            raise HTTPException(400, "监控目录已存在")
        finally:
            conn.close()
    result = WatchDirOut(id=row[0], watch_path=row[1], output_path=row[2], enabled=bool(row[3]), auto_delete=bool(row[4]), created_at=row[5])
    if result.enabled:
        watcher.add_path(result.watch_path, result.id, result.output_path)
    return result

@app.delete("/api/watch-dirs/{wid}")
def delete_watch_dir(wid: int):
    watcher.remove_path(wid)
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM watch_dirs WHERE id=?", (wid,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

@app.put("/api/watch-dirs/{wid}/toggle")
def toggle_watch_dir(wid: int, enabled: bool):
    with db_lock:
        conn = get_db()
        try:
            conn.execute("UPDATE watch_dirs SET enabled=? WHERE id=?", (1 if enabled else 0, wid))
            conn.commit()
            row = conn.execute("SELECT watch_path, output_path FROM watch_dirs WHERE id=?", (wid,)).fetchone()
        finally:
            conn.close()
    if row:
        if enabled:
            watcher.add_path(row[0], wid, row[1])
        else:
            watcher.remove_path(wid)
    return {"ok": True}

@app.get("/api/watch-dirs/{wid}/scan")
def scan_watch_dir(wid: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT watch_path, output_path FROM watch_dirs WHERE id=?", (wid,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "目录不存在")
    threading.Thread(target=_scan_dir, args=(wid, row[0], row[1]), daemon=True).start()
    return {"ok": True, "message": f"开始扫描: {row[0]}"}


# ============== API: History ==============
@app.get("/api/history", response_model=HistoryPage)
def list_history(status: str = "", search: str = "", page: int = 1, page_size: int = 20):
    offset = (page - 1) * page_size
    conn = get_db()
    try:
        where = "WHERE 1=1"
        params = []
        if status:
            where += " AND status=?"
            params.append(status)
        if search:
            where += " AND original_path LIKE ?"
            params.append(f"%{search}%")
        total = conn.execute(f"SELECT COUNT(*) FROM archive_history {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id,original_path,output_path,status,file_size,password_used,error_message,extracted_size,file_count,duration_seconds,created_at,completed_at FROM archive_history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()
    finally:
        conn.close()
    cols = ['id','original_path','output_path','status','file_size','password_used','error_message','extracted_size','file_count','duration_seconds','created_at','completed_at']
    items = [HistoryItem(**dict(zip(cols, r))) for r in rows]
    return HistoryPage(items=items, total=total, page=page, page_size=page_size)

@app.delete("/api/history/clear")
def clear_history():
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM archive_history")
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

@app.delete("/api/history/{hid}")
def delete_history(hid: int):
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM archive_history WHERE id=?", (hid,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

@app.post("/api/history/{hid}/retry")
def retry_history(hid: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT original_path FROM archive_history WHERE id=?", (hid,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "记录不存在")
    archive_path = row[0]
    # Remove from done_files so it can be re-processed
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM done_files WHERE file_path=?", (archive_path,))
            conn.execute("UPDATE archive_history SET status='pending' WHERE id=?", (hid,))
            conn.commit()
            wd = conn.execute(
                "SELECT wd.id, wd.output_path FROM watch_dirs wd WHERE wd.enabled=1 AND ? LIKE wd.watch_path || '%' LIMIT 1",
                (archive_path,)
            ).fetchone()
        finally:
            conn.close()
    wid = wd[0] if wd else 0
    output_path = wd[1] if wd else str(BASE_DIR / "extracted")
    with processing_lock:
        processing_files.discard(archive_path)
    queue_archive(archive_path, wid, output_path)
    return {"ok": True}

# ============== API: Passwords ==============
@app.get("/api/passwords", response_model=List[PasswordOut])
def list_passwords():
    conn = get_db()
    try:
        rows = conn.execute("SELECT id,password,tag,hit_count,created_at FROM passwords ORDER BY hit_count DESC, id DESC").fetchall()
    finally:
        conn.close()
    return [PasswordOut(id=r[0], password=r[1], tag=r[2], hit_count=r[3], created_at=r[4]) for r in rows]

@app.post("/api/passwords", response_model=PasswordOut)
def create_password(data: PasswordCreate):
    with db_lock:
        conn = get_db()
        try:
            try:
                conn.execute("INSERT INTO passwords (password,tag) VALUES (?,?)", (data.password, data.tag))
                conn.commit()
            except sqlite3.IntegrityError:
                raise HTTPException(400, "密码已存在")
            row = conn.execute("SELECT id,password,tag,hit_count,created_at FROM passwords WHERE password=?", (data.password,)).fetchone()
        finally:
            conn.close()
    return PasswordOut(id=row[0], password=row[1], tag=row[2], hit_count=row[3], created_at=row[4])

@app.delete("/api/passwords/{pid}")
def delete_password(pid: int):
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM passwords WHERE id=?", (pid,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

# ============== API: Settings ==============
@app.get("/api/settings")
def get_settings():
    conn = get_db()
    try:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}

@app.put("/api/settings")
def update_settings(data: dict):
    with db_lock:
        conn = get_db()
        try:
            for k, v in data.items():
                conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, str(v)))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

# ============== API: Logs ==============
@app.get("/api/logs", response_model=List[LogOut])
def get_logs(limit: int = 200, level: str = ""):
    conn = get_db()
    try:
        if level:
            rows = conn.execute("SELECT id,level,message,task_id,created_at FROM task_logs WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit)).fetchall()
        else:
            rows = conn.execute("SELECT id,level,message,task_id,created_at FROM task_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [LogOut(id=r[0], level=r[1], message=r[2], task_id=r[3] or "", created_at=r[4]) for r in rows]

@app.delete("/api/logs")
def clear_logs():
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM task_logs")
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

# ============== API: Files ==============
@app.get("/api/files")
def list_files(path: str = "/tmp"):
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"目录不存在: {path}")
    items = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                stat = item.stat()
                items.append({
                    "name": item.name,
                    "path": str(item),
                    "size": stat.st_size if item.is_file() else 0,
                    "is_dir": item.is_dir(),
                    "is_archive": item.is_file() and is_archive(str(item)),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                })
            except:
                pass
    except PermissionError:
        raise HTTPException(403, "没有权限访问该目录")
    return {"path": str(p), "parent": str(p.parent) if str(p) != "/" else None, "items": items}

# ============== WebSocket: Logs ==============
@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    # Each client gets its own queue
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    with _ws_subscribers_lock:
        _ws_subscribers.append(q)
    # Send last 50 logs on connect
    conn = get_db()
    try:
        rows = conn.execute("SELECT level,message,created_at FROM task_logs ORDER BY id DESC LIMIT 50").fetchall()
    finally:
        conn.close()
    try:
        await websocket.send_json([{"level": r[0], "message": r[1], "created_at": r[2]} for r in reversed(rows)])
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_json([item])
            except asyncio.TimeoutError:
                # heartbeat ping
                await websocket.send_json([])
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        with _ws_subscribers_lock:
            try:
                _ws_subscribers.remove(q)
            except ValueError:
                pass

# ============== Static Files ==============
@app.get("/")
def root():
    for candidate in [BASE_DIR / "frontend" / "index.html", Path("/app/frontend/index.html")]:
        if candidate.exists():
            return FileResponse(str(candidate))
    return {"message": "ArchiveMate v2 API running"}

for assets_dir in [BASE_DIR / "frontend" / "assets", Path("/app/frontend/assets")]:
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
        break

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3088, log_level="info")

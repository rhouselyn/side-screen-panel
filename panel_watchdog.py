"""看门狗：确保副屏看板（dashboard.py）始终在运行。

- 启动时若面板未运行则拉起；
- 之后每 10 秒检查一次，发现退出就重新拉起；
- 拉起使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，使面板进程
  脱离父进程树，即使本看门狗被回收，面板也能继续存活。

启动脚本和 Startup 快捷方式共用此入口；面板明确退出时停止守护。
"""

import subprocess
import os
import sys
import time
from pathlib import Path
from panel_paths import ROOT, STOP_FILE, prepare_data

DASHBOARD = str(ROOT / "dashboard.py")
PYTHONW = str(Path(sys.executable).with_name("pythonw.exe"))

DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB，脱离任务计划程序的作业对象


def dashboard_running():
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["cmdline"]):
        args = p.info.get("cmdline") or []
        if len(args) < 2 or Path(args[0]).name.lower() not in ("python", "python.exe", "pythonw.exe"):
            continue
        if Path(args[1]).name.lower() != "dashboard.py":
            continue
        try:
            script = Path(args[1])
            if not script.is_absolute():
                script = Path(p.cwd()) / script
            if os.path.normcase(os.path.normpath(script)) == os.path.normcase(DASHBOARD):
                return True
        except (psutil.Error, OSError):
            continue
    return False


def startup_tasks():
    """只识别本项目旧版在任务计划程序根目录注册的启动任务。"""
    import win32com.client
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    targets = {os.path.normcase(str(ROOT / name)) for name in ("dashboard.py", "panel_watchdog.py")}
    result = []
    for task in service.GetFolder("\\").GetTasks(1):
        for action in task.Definition.Actions:
            if action.Type == 0 and Path(action.Path).name.lower() in ("python.exe", "pythonw.exe"):
                argument = os.path.normcase(os.path.normpath(action.Arguments.strip().strip('"')))
                if argument in targets:
                    result.append(task)
                    break
    return result


def launch():
    try:
        subprocess.Popen(
            [PYTHONW, DASHBOARD],
            creationflags=DETACHED | NEW_GROUP | BREAKAWAY,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        # 若作业不允许 breakaway，退一步用常规分离方式
        try:
            subprocess.Popen(
                [PYTHONW, DASHBOARD],
                creationflags=DETACHED | NEW_GROUP,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


def main():
    from PyQt5.QtCore import QCoreApplication, QSharedMemory
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    singleton = QSharedMemory("local-monitor-watchdog-v1", app)
    if not singleton.create(1):
        if not STOP_FILE.exists():
            return
        # 明确退出后马上重开时，等旧守护释放锁再接管，不能吞掉启动请求。
        for _ in range(12):
            time.sleep(1)
            if singleton.create(1):
                break
        else:
            return
    prepare_data()
    STOP_FILE.unlink(missing_ok=True)
    while not STOP_FILE.exists():
        if not dashboard_running():
            launch()
        for _ in range(10):
            time.sleep(1)
            if STOP_FILE.exists():
                return


if __name__ == "__main__":
    main()

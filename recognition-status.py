import ctypes
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parent
PID_PATH = ROOT / 'data/recognition.pid'
DB_PATH = ROOT / 'data/recognition.sqlite3'


def process_is_alive(pid: int) -> bool:
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def file_summary(path: Path) -> str:
    if not path.exists():
        return f'{path.relative_to(ROOT)}: файла пока нет'
    modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime('%d.%m.%Y %H:%M:%S')
    return f'{path.relative_to(ROOT)}: {path.stat().st_size} байт, обновлён {modified}'


def main() -> int:
    pid = None
    try:
        pid = int(PID_PATH.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError, OSError):
        pass

    running = bool(pid and process_is_alive(pid))
    print(f'Процесс: {"РАБОТАЕТ" if running else "НЕ ЗАПУЩЕН"}' + (f' (PID {pid})' if pid else ''))

    if DB_PATH.exists():
        try:
            db = sqlite3.connect(f'file:{DB_PATH.as_posix()}?mode=ro', uri=True, timeout=2)
            counts = dict(db.execute('SELECT status, COUNT(*) FROM events GROUP BY status').fetchall())
            pending_notifications = db.execute(
                "SELECT COUNT(*) FROM events WHERE status IN ('done','failed') AND notified=0"
            ).fetchone()[0]
            last = db.execute('SELECT id,message,status FROM events ORDER BY id DESC LIMIT 1').fetchone()
            month = datetime.now(timezone.utc).strftime('%Y-%m')
            spent = db.execute('SELECT COALESCE(SUM(cost),0) FROM usage WHERE month=?', (month,)).fetchone()[0]
            paused = db.execute(
                "SELECT value FROM app_settings WHERE key='recognition_paused'").fetchone()
            channels = db.execute(
                'SELECT COUNT(*),SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END),'
                "SUM(CASE WHEN connection_status='ready' THEN 1 ELSE 0 END) FROM channels"
            ).fetchone()
            commands = db.execute(
                "SELECT COUNT(*) FROM control_commands WHERE status='pending'").fetchone()[0]
            plans = dict(db.execute('''
                SELECT status,COUNT(*) FROM trade_plans
                WHERE status!='superseded' GROUP BY status''').fetchall())
            positions = dict(db.execute(
                'SELECT status,COUNT(*) FROM positions GROUP BY status').fetchall())
            db.close()
            print(f'Разборы: {counts or {}}; уведомлений в очереди: {pending_notifications}')
            print(f'Пауза: {bool(paused and paused[0] == "true")}; LLM за месяц: ${spent:.4f}')
            print(f'Каналы: всего {channels[0]}, включено {channels[1] or 0}, подключено {channels[2] or 0}; '
                  f'команд ожидает: {commands}')
            print(f'Планы: {plans or {}}; позиции: {positions or {}}')
            if last:
                print(f'Последнее событие: #{last[0]}, пост #{last[1]}, статус {last[2]}')
        except sqlite3.Error as error:
            print(f'База: не удалось прочитать ({type(error).__name__})')
    else:
        print('База: ещё не создана')

    print(file_summary(ROOT / 'logs/recognition.log'))
    print(file_summary(ROOT / 'logs/recognition-error.log'))
    print('Живой журнал: watch-recognition-log.cmd')
    print('Telegram: /status или /stats')
    return 0 if running else 1


if __name__ == '__main__':
    sys.exit(main())

import os
import re
import socket
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

ROOT = Path(__file__).resolve().parent
PORT = os.environ.get("PORT", "10000")
URL_RE = re.compile(r"https://[a-z0-9-]+\.lhr\.life", re.I)
_MUTEX_HANDLE = None

# Windows consoles often use cp1251, which cannot print some punctuation or
# emoji below. Keep logging from crashing after the tunnel has been created.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def read_bot_token():
    token = os.environ.get("BOT_TOKEN", "")
    if token or winreg is None:
        return token
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            token, _ = winreg.QueryValueEx(key, "BOT_TOKEN")
        return str(token)
    except OSError:
        return ""


def claim_single_instance():
    """Prevent overlapping local launchers from polling Telegram twice."""
    global _MUTEX_HANDLE
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, 1, "Local\\KochBratstvoCasinoBot")
        if not _MUTEX_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(_MUTEX_HANDLE)
            _MUTEX_HANDLE = None
            print("Бот уже запущен. Второй экземпляр не стартует.", flush=True)
            return False
    # Also catch the currently running instance, which predates this guard.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", int(PORT))) == 0:
            print(f"На порту {PORT} уже работает сервер казино. Второй экземпляр не стартует.", flush=True)
            return False
    return True


def start_tunnel(ssh, creationflags):
    command = [
        ssh, "-T", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-R", "80:127.0.0.1:" + PORT, "nokey@localhost.run",
    ]
    tunnel = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=creationflags,
    )
    found = {"url": None}
    ready = threading.Event()

    def read_output():
        if tunnel.stdout is None:
            return
        for line in tunnel.stdout:
            for match in URL_RE.finditer(line):
                found["url"] = match.group(0).rstrip(".,;)")
                ready.set()
                break
            if line.lower().startswith("ssh:") or "permission denied (" in line.lower() or "connection refused" in line.lower():
                print("[туннель] " + line.rstrip(), flush=True)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + 45
    while not ready.wait(0.25):
        if tunnel.poll() is not None:
            raise RuntimeError(f"SSH‑туннель завершился с кодом {tunnel.returncode}.")
        if time.monotonic() >= deadline:
            raise RuntimeError("SSH‑туннель не выдал HTTPS‑адрес за 45 секунд.")
    return tunnel, found["url"]


def endpoint_ok(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=6) as response:
            return response.status == 200 and response.read(32).strip() == b"ok"
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    if not claim_single_instance():
        return 0
    token = read_bot_token()
    if not token:
        print("В Windows не найдена переменная BOT_TOKEN.")
        print("Проверь её в пользовательских переменных среды и запусти снова.")
        return 1
    os.environ["BOT_TOKEN"] = token

    ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not ssh:
        print("Не найден ssh.exe. В Windows включи компонент OpenSSH Client.")
        return 1

    env = os.environ.copy()
    env["PORT"] = PORT
    env["DB_PATH"] = str(ROOT / "leaderboard.db")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    tunnel = None
    web = None
    try:
        while True:
            old_web = web
            try:
                tunnel, public_url = start_tunnel(ssh, creationflags)
                env["BASE_WEB_APP_URL"] = public_url
                env["ALLOWED_ORIGIN"] = public_url
                (Path(os.environ.get("TEMP", str(ROOT))) / "koch-casino-url.txt").write_text(public_url, encoding="utf-8")
                # On the first start, launch the local server so the tunnel has
                # something to reach. On reconnect, keep the old bot alive until
                # the candidate tunnel has passed this health check.
                if old_web is None:
                    web = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env)
                    print("Запускаю локальный сервер; проверяю новый туннель...", flush=True)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and tunnel.poll() is None:
                    if endpoint_ok(public_url):
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError("Новый туннель не прошёл проверку доступности.")

                if old_web is not None:
                    stop_process(old_web)
                    web = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env)
                print(f"HTTPS‑адрес казино: {public_url}", flush=True)
                if old_web is None:
                    print("Бот запущен. Туннель проверяется автоматически.", flush=True)
                    print("База данных: leaderboard.db", flush=True)
                else:
                    print("Туннель восстановлен; адрес кнопки Telegram обновлён.", flush=True)

                failed_checks = 0
                while web.poll() is None and tunnel.poll() is None:
                    time.sleep(12)
                    if endpoint_ok("http://127.0.0.1:" + PORT):
                        if endpoint_ok(public_url):
                            failed_checks = 0
                        else:
                            failed_checks += 1
                            print(f"Проверка туннеля не прошла ({failed_checks}/3).", flush=True)
                    else:
                        print("Локальный сервер временно не отвечает; проверю ещё раз.", flush=True)
                        failed_checks += 1
                    if failed_checks >= 3:
                        print("Туннель недоступен. Переподключаюсь; база данных не меняется.", flush=True)
                        break

                if web.poll() is not None:
                    print(f"Веб-сервер завершился с кодом {web.returncode}; запускаю заново.", flush=True)
                elif tunnel.poll() is not None:
                    print("SSH‑туннель завершился; подключаю заново.", flush=True)
                stop_process(web)
                web = None
                stop_process(tunnel)
                tunnel = None
                time.sleep(3)
            except KeyboardInterrupt:
                print("Останавливаю бота и туннель...", flush=True)
                return 130
            except Exception as exc:
                print(f"[перезапуск] {exc}", flush=True)
                if old_web is None:
                    stop_process(web)
                    web = None
                else:
                    web = old_web
                stop_process(tunnel)
                tunnel = None
                time.sleep(5)
    except KeyboardInterrupt:
        print("Останавливаю бота и туннель...", flush=True)
        return 130
    finally:
        stop_process(web)
        stop_process(tunnel)


if __name__ == "__main__":
    raise SystemExit(main())

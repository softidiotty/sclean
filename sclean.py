import os
import sys
import time
import json
import shutil
import subprocess
import datetime
import threading
import queue
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox


# ============================================================
# Метаданные приложения
# ============================================================

APP_NAME = "sclean"
APP_VERSION = "1.15.2"
APP_AUTHOR = "softidiotty"
APP_FONT = "Segoe UI"

# Репозиторий GitHub, из которого программа проверяет и скачивает новые
# версии (см. раздел "Автообновление" ниже). Формат: "аккаунт/репозиторий".
# Замените на свой репозиторий перед публикацией релизов — без этого
# проверка обновлений просто ничего не найдёт (GitHub вернёт 404).
GITHUB_REPO = "softidiotty/sclean"


# ============================================================
# Проверка / запрос прав администратора
# ============================================================

def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """
    Перезапускает текущий исполняемый файл (или python-скрипт) с правами
    администратора через UAC-диалог и завершает текущий процесс.
    """
    import ctypes

    if getattr(sys, "frozen", False):
        # Собранный exe (PyInstaller) — перезапускаем сам себя
        executable = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        # Запуск как .py — перезапускаем через тот же интерпретатор
        executable = sys.executable
        params = " ".join(f'"{a}"' for a in ([os.path.abspath(__file__)] + sys.argv[1:]))

    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    except Exception:
        pass
    sys.exit(0)


# ============================================================
# Утилиты (та же логика, что и в тихой версии)
# ============================================================

# Рабочая директория для всех дочерних процессов. Собранный exe (PyInstaller
# onefile) запускается из временной папки %TEMP%\_MEIxxxxx, которая может
# исчезнуть, если сама программа чистит TEMP (или Windows чистит его сама) —
# тогда cmd.exe/powershell не могут получить текущую директорию и падают
# с ошибкой вида "No such file or directory: ...\_MEIxxxxx\base_library.zip".
# Поэтому всегда явно указываем безопасный системный каталог как cwd.
SAFE_CWD = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"


def _hidden_startupinfo():
    """STARTUPINFO, заставляющее любое дочернее GUI-приложение (в т.ч.
    cleanmgr, которое иначе показывает своё окно прогресса) запускаться
    свёрнутым/скрытым."""
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    except Exception:
        return None


def _background_minimized_startupinfo():
    """
    STARTUPINFO для GUI-процессов вроде cleanmgr: окно создаётся и
    реально существует (получает и обрабатывает сообщения Windows), но
    показывается свёрнутым и НЕ становится активным/в фокусе
    (SW_SHOWMINNOACTIVE). Это отличается от полностью скрытого
    (SW_HIDE) режима, при котором GUI-процесс может зависнуть, если
    Windows троттлит/замораживает окно, которое никогда не получает
    цикл сообщений — переключение пользователя на другие окна в это
    время приводило к зависанию cleanmgr до принудительного завершения.
    SW_SHOWMINNOACTIVE не даёт окну красть фокус, но и не позволяет
    Windows считать процесс полностью неактивным/фоновым.
    """
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        return si
    except Exception:
        return None


CREATE_NO_WINDOW = 0x08000000


def run_cmd(cmd, timeout=None, hide_window=True, window_mode=None):
    """
    Выполняет команду, возвращает (returncode, stdout, stderr). Не бросает
    исключений наружу.

    window_mode управляет тем, как показывается окно дочернего процесса:
      - "hide" (или hide_window=True, по умолчанию) — полностью скрытое
        окно (SW_HIDE) + CREATE_NO_WINDOW. Безопасно для обычных
        консольных команд (powercfg, netsh, defrag и т.д.).
      - "minimize" — окно реально создаётся и получает сообщения Windows,
        но показывается свёрнутым и не крадёт фокус (SW_SHOWMINNOACTIVE).
        Нужен для полноценных GUI-приложений вроде cleanmgr: полностью
        скрытое окно (SW_HIDE) у таких процессов может зависнуть — Windows
        троттлит/замораживает GUI-окно, которое никогда не получает цикл
        сообщений, особенно если пользователь в это время переключается
        на другие окна. "minimize" не даёт этому произойти, оставаясь
        при этом невидимым и не мешающим пользователю.
      - "show" — не подавлять окно вообще (старое поведение hide_window=False).
    """
    if window_mode is None:
        window_mode = "hide" if hide_window else "show"

    if window_mode == "hide":
        startupinfo = _hidden_startupinfo()
        creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    elif window_mode == "minimize":
        startupinfo = _background_minimized_startupinfo()
        creationflags = 0
    else:  # "show"
        startupinfo = None
        creationflags = 0

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=SAFE_CWD,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def run_ps(ps_command, timeout=None):
    """
    Выполняет команду PowerShell, принудительно переключая вывод консоли
    в UTF-8 (chcp 65001 + $OutputEncoding), чтобы кириллица в результатах
    (модели дисков, название ОС и т.д.) не превращалась в иероглифы.
    """
    wrapped = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; " + ps_command
    )
    # base64 (UTF-16LE) через -EncodedCommand полностью убирает проблемы
    # с экранированием кавычек и с кодировкой при передаче команды в процесс.
    import base64
    encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    cmd = f'powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}'

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=SAFE_CWD,
            startupinfo=_hidden_startupinfo(),
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""

    if result.returncode != 0:
        return ""

    raw = result.stdout
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _count_processes_by_name(exe_name):
    """
    Возвращает количество запущенных процессов с данным именем exe
    (например "cleanmgr.exe") через tasklist /FI. Используется вместо
    проверки одного PID/proc.wait(), потому что cleanmgr иногда
    порождает дополнительные копии себя (или пересоздаётся) в процессе
    работы — процесс, который мы изначально запустили через Popen, может
    формально завершиться, пока реальная работа продолжается в другом
    процессе с тем же именем. Возвращает -1 при ошибке (tasklist
    недоступен) — вызывающий код должен трактовать это консервативно.
    """
    try:
        result = subprocess.run(
            f'tasklist /FI "IMAGENAME eq {exe_name}" /FO CSV /NH',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            cwd=SAFE_CWD,
            startupinfo=_hidden_startupinfo(),
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        return -1

    if result.returncode != 0:
        return -1

    try:
        out = result.stdout.decode("cp866", errors="replace")
    except Exception:
        out = result.stdout.decode("utf-8", errors="replace")

    # tasklist без совпадений печатает локализованное сообщение
    # ("INFO: No tasks..." / "ИНФОРМАЦИЯ: Задачи..."), а не CSV-строку —
    # достаточно посчитать строки, начинающиеся с кавычки (валидный CSV).
    return sum(1 for line in out.splitlines() if line.strip().startswith('"'))


def _kill_processes_by_name(exe_name):
    """Принудительно завершает все процессы с данным именем exe."""
    try:
        subprocess.run(
            f'taskkill /IM "{exe_name}" /F',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            cwd=SAFE_CWD,
            startupinfo=_hidden_startupinfo(),
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        pass


def get_free_space_gb(drive="C:\\"):
    try:
        total, used, free = shutil.disk_usage(drive)
        return round(free / (1024 ** 3), 2)
    except Exception:
        return None


def get_disk_usage_info(drive="C:\\"):
    """
    Возвращает (used_gb, total_gb, percent_used) для индикатора заполнения
    диска в шапке, или None при ошибке.
    """
    try:
        total, used, free = shutil.disk_usage(drive)
        total_gb = total / (1024 ** 3)
        used_gb = used / (1024 ** 3)
        percent = round(used / total * 100) if total else 0
        return (round(used_gb, 1), round(total_gb, 1), percent)
    except Exception:
        return None


PROTECTED_NAME_HINTS = ("desktop.ini", ".lnk")


def _is_own_runtime_folder(name):
    """
    True для временной папки распаковки самого запущенного PyInstaller
    onefile-exe (%TEMP%\\_MEIxxxxx) — её нельзя удалять во время работы
    программы, иначе все последующие вызовы subprocess ломаются.
    """
    if not name.lower().startswith("_mei"):
        return False
    mei_path = getattr(sys, "_MEIPASS", None)
    if mei_path and os.path.basename(mei_path.rstrip("\\/")).lower() == name.lower():
        return True
    # Если _MEIPASS недоступен (запуск как .py) — на всякий случай не трогаем
    # вообще никакие _MEI* папки, это всегда чья-то временная распаковка PyInstaller.
    return True


def safe_delete_files_in(folder, min_age_minutes=0):
    deleted = 0
    errors = 0
    freed_bytes = 0

    if not folder or not os.path.isdir(folder):
        return deleted, errors, freed_bytes

    now = time.time()
    min_age_sec = min_age_minutes * 60

    try:
        entries = os.listdir(folder)
    except (PermissionError, FileNotFoundError, OSError):
        return deleted, errors, freed_bytes

    for name in entries:
        if name.lower() in PROTECTED_NAME_HINTS:
            continue
        if _is_own_runtime_folder(name):
            continue

        path = os.path.join(folder, name)
        try:
            if os.path.islink(path):
                os.unlink(path)
                deleted += 1
                continue

            if os.path.isfile(path):
                if min_age_sec:
                    try:
                        if now - os.path.getmtime(path) < min_age_sec:
                            continue
                    except OSError:
                        pass
                try:
                    freed_bytes += os.path.getsize(path)
                except OSError:
                    pass
                os.remove(path)
                deleted += 1

            elif os.path.isdir(path):
                try:
                    for dirpath, _dirnames, filenames in os.walk(path):
                        for fn in filenames:
                            try:
                                freed_bytes += os.path.getsize(os.path.join(dirpath, fn))
                            except OSError:
                                pass
                except OSError:
                    pass
                shutil.rmtree(path, ignore_errors=True)
                deleted += 1

        except (PermissionError, OSError):
            errors += 1
            continue

    return deleted, errors, freed_bytes


def format_bytes_gb(num_bytes):
    return round(num_bytes / (1024 ** 3), 2)


def report_freed_bytes(logf, num_bytes):
    """
    Способ для шага сообщить, сколько байт он РЕАЛЬНО освободил.

    Раньше итог "Освобождено места" считался как разница свободного
    места на C: до и после всего запуска. Это регулярно врало: за
    десятки секунд работы система и другие программы успевают записать
    на диск больше, чем очистка освободила, и в отчёт попадали значения
    вроде "-0.01 ГБ" — при том что очистка временных файлов честно
    освободила 0.04 ГБ и написала об этом строкой выше.

    Теперь шаги, которые знают свой объём точно (они удаляют файлы сами
    и складывают размеры), сообщают его сюда, а итог считается суммой
    этих значений. Величина не может стать отрицательной и не зависит от
    посторонней записи на диск. Разница свободного места по-прежнему
    показывается в отчёте, но как справочная, с оговоркой.

    Значение копится прямо на объекте функции logf — у каждого шага она
    своя (см. CleanerApp._worker._make_logf), так что счётчики шагов не
    смешиваются. Для logf без поддержки атрибутов вызов просто ничего не
    делает.
    """
    try:
        logf.freed_bytes = getattr(logf, "freed_bytes", 0) + int(num_bytes)
    except Exception:
        pass


def get_disk_media_type(drive_letter="C"):
    ps = (
        f"$p = Get-Partition -DriveLetter {drive_letter} -ErrorAction Stop; "
        f"$d = Get-Disk -Number $p.DiskNumber -ErrorAction Stop; "
        f"$pd = Get-PhysicalDisk -DeviceNumber $d.Number -ErrorAction Stop; "
        f"Write-Output $pd.MediaType"
    )
    out = run_ps(ps, timeout=30)
    if "SSD" in out:
        return "SSD"
    if "HDD" in out:
        return "HDD"
    return "Unknown"


def get_desktop_dir():
    userprofile = os.getenv("USERPROFILE")
    if not userprofile:
        return None
    return os.path.join(userprofile, "Desktop")


def get_app_data_dir():
    """
    Папка sclean на рабочем столе для журналов и бэкапа настроек —
    вместо того, чтобы разбрасывать файлы прямо по рабочему столу.
    Создаётся при первом обращении, если ещё не существует.
    """
    desktop = get_desktop_dir()
    base = desktop if (desktop and os.path.isdir(desktop)) else os.getenv("TEMP", ".")
    folder = os.path.join(base, "sclean")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        return base
    return folder


MAX_REPORTS_KEPT = 30


def rotate_old_reports(logf=None, keep=MAX_REPORTS_KEPT):
    """
    Оставляет только keep последних файлов отчётов sclean_*.txt в папке
    sclean, более старые удаляются — чтобы отчёты не копились бесконечно.
    """
    folder = get_app_data_dir()
    try:
        files = [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if name.startswith("sclean_") and name.endswith(".txt")
        ]
    except Exception:
        return
    if len(files) <= keep:
        return
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    removed = 0
    for path in files[keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed and logf:
        logf(f"Ротация отчётов: удалено {removed} старых файлов (оставлено последних {keep}).")


# ============================================================
# Бэкап и восстановление изменяемых настроек системы
# ============================================================

def get_backup_path():
    return os.path.join(get_app_data_dir(), "sclean_backup.json")


def load_backup():
    path = get_backup_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_backup(data):
    path = get_backup_path()
    existing = load_backup() or {}
    existing.update(data)
    existing["saved_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return path


def backup_power_plan(logf):
    """
    Сохраняет GUID активной схемы электропитания.

    Раньше GUID вырезался через -replace по шаблону '.*GUID: ...' —
    но это работает только на английской Windows. На русской powercfg
    печатает "GUID схемы питания: 8c5e7fda-... (Высокая
    производительность)", шаблон не совпадал, и в бэкап уходила ВСЯ
    строка целиком. При восстановлении powercfg /setactive получал эту
    строку вместо GUID и молча падал — то есть откат электропитания
    фактически не работал ни разу на русской системе.

    Теперь GUID ищется регулярным выражением по своей форме
    (8-4-4-4-12 hex), без привязки к языку подписи вокруг него.
    """
    out = run_ps(
        "$t = (powercfg /getactivescheme | Out-String); "
        "[regex]::Match($t, '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}').Value"
    )
    guid = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""

    # Тайм-ауты сна/экрана/дисков и USB selective suspend сохраняем
    # отдельно: step_power_plan обнуляет их, и без этих значений откат
    # вернул бы прежнюю СХЕМУ, но с уже затёртыми тайм-аутами.
    # powercfg /query печатает их в шестнадцатеричном виде — забираем
    # оба индекса (от сети и от батареи) по каждому параметру.
    timeouts = {}
    for label, subgroup, setting in (
        ("standby", "SUB_SLEEP", "STANDBYIDLE"),
        ("monitor", "SUB_VIDEO", "VIDEOIDLE"),
        ("disk", "SUB_DISK", "DISKIDLE"),
        ("usb_suspend", "2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"),
    ):
        q = run_ps(
            f"$t = (powercfg /query SCHEME_CURRENT {subgroup} {setting} | Out-String); "
            "$ac = [regex]::Match($t, '(?i)Current AC Power Setting Index:\\s*0x([0-9a-f]+)').Groups[1].Value; "
            "if (-not $ac) { $ac = [regex]::Match($t, '0x([0-9a-f]+)').Groups[1].Value }; "
            "$dc = [regex]::Match($t, '(?i)Current DC Power Setting Index:\\s*0x([0-9a-f]+)').Groups[1].Value; "
            "Write-Output ($ac + '|' + $dc)",
            timeout=30,
        )
        parts = (q or "").strip().splitlines()
        if parts:
            ac, _, dc = parts[-1].strip().partition("|")
            if ac or dc:
                timeouts[label] = {"ac": ac.strip(), "dc": dc.strip()}

    payload = {"power_timeouts": timeouts}
    if guid:
        payload["power_plan_guid"] = guid
    save_backup(payload)

    if guid:
        logf(f"  Бэкап: текущая схема электропитания сохранена ({guid}).")
    else:
        logf("  Бэкап: не удалось определить текущую схему электропитания.")
    if timeouts:
        logf(f"  Бэкап: сохранены прежние тайм-ауты сна/экрана/дисков и USB ({len(timeouts)} параметра).")


def backup_firewall(logf):
    """
    Сохраняет состояние (вкл/выкл) трёх профилей брандмауэра.

    Раньше состояние определялось разбором текста `netsh advfirewall
    show <profile> state` на подстроки "ON"/"OFF" — но netsh печатает
    локализованный вывод, и на русской Windows там "ВКЛ"/"ОТКЛ". Ни одна
    из подстрок не находилась, и в бэкап для всех профилей уходило
    "UNKNOWN". Восстановление же применяет состояние только если оно
    равно "ON" или "OFF" — то есть откат брандмауэра тихо не делал
    ничего, хотя в отчёте выглядел успешным.

    Get-NetFirewallProfile возвращает булево поле Enabled, не зависящее
    от языка системы. netsh оставлен как резерв на случай, если модуль
    NetSecurity недоступен (урезанные сборки Windows).
    """
    states = {}
    out = run_ps(
        "try { Get-NetFirewallProfile -ErrorAction Stop | "
        "ForEach-Object { Write-Output ($_.Name + '=' + $_.Enabled) } } catch { Write-Output 'FAIL' }"
    )
    parsed = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if "=" in line:
            name, _, value = line.partition("=")
            name = name.strip().capitalize()
            value = value.strip().lower()
            if value in ("true", "1"):
                parsed[name] = "ON"
            elif value in ("false", "0"):
                parsed[name] = "OFF"

    for profile in ("Domain", "Private", "Public"):
        if profile in parsed:
            states[profile] = parsed[profile]
            continue
        # Резервный путь: netsh. Проверяем и английские, и русские
        # варианты подписи состояния.
        code, netsh_out, err = run_cmd(
            f"netsh advfirewall show {profile.lower()}profile state", timeout=15
        )
        text = (netsh_out or "").upper()
        if "ВКЛ" in text or " ON" in text:
            states[profile] = "ON"
        elif "ОТКЛ" in text or "ВЫКЛ" in text or " OFF" in text:
            states[profile] = "OFF"
        else:
            states[profile] = "UNKNOWN"

    save_backup({"firewall_state": states})
    unknown = [p for p, s in states.items() if s == "UNKNOWN"]
    if unknown:
        logf(f"  Бэкап: состояние брандмауэра сохранено ({states}); не определено: {', '.join(unknown)}.")
    else:
        logf(f"  Бэкап: состояние брандмауэра сохранено ({states}).")


def backup_visual_effects(logf):
    visualfx = run_ps(
        "try { (Get-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VisualEffects' "
        "-Name VisualFXSetting -ErrorAction Stop).VisualFXSetting } catch { '' }"
    )
    min_animate = run_ps(
        "try { (Get-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' "
        "-Name MinAnimate -ErrorAction Stop).MinAnimate } catch { '' }"
    )
    drag_full = run_ps(
        "try { (Get-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' "
        "-Name DragFullWindows -ErrorAction Stop).DragFullWindows } catch { '' }"
    )
    font_smoothing = run_ps(
        "try { (Get-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' "
        "-Name FontSmoothing -ErrorAction Stop).FontSmoothing } catch { '' }"
    )
    font_smoothing_type = run_ps(
        "try { (Get-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' "
        "-Name FontSmoothingType -ErrorAction Stop).FontSmoothingType } catch { '' }"
    )
    save_backup({
        "visual_fx_setting": visualfx.strip(),
        "min_animate": min_animate.strip(),
        "drag_full_windows": drag_full.strip(),
        "font_smoothing": font_smoothing.strip(),
        "font_smoothing_type": font_smoothing_type.strip(),
    })
    logf("  Бэкап: текущие визуальные эффекты сохранены.")


def backup_prefetch_snapshot(logf):
    """
    Сохраняет список файлов Prefetch (имена + время изменения) перед их
    удалением. Сами файлы восстановить нельзя (они пересоздаются Windows
    автоматически), но список позволяет увидеть, что именно было удалено.
    """
    try:
        names = []
        folder = r"C:\Windows\Prefetch"
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    names.append(name)
        save_backup({"prefetch_snapshot": names[:500]})
        logf(f"  Бэкап: список Prefetch сохранён ({len(names)} файлов, для справки — файлы не восстанавливаются).")
    except Exception:
        pass


def backup_usb_power(logf):
    """
    Сохраняет состояние галочек "Разрешить отключение этого устройства
    для экономии энергии" по каждому USB-устройству плюс машинный
    переключатель Services\\USB\\DisableSelectiveSuspend.

    Без этого бэкапа кнопка "Бэкап" обещала вернуть систему в прежнее
    состояние, но пункт отключения энергосбережения USB откатить было
    нечем — он менял настройки безвозвратно.

    Ключ словаря — имя экземпляра WMI (InstanceName), значение — было ли
    разрешено отключение (True/False). Восстановление возвращает ровно
    те значения, что были у каждого устройства, а не "включить всем".
    """
    ps = (
        "$out = @(); "
        "try { "
        "  foreach ($it in @(Get-CimInstance -Namespace root/WMI -ClassName MSPower_DeviceEnable -ErrorAction Stop)) { "
        "    if ($it.InstanceName) { $out += ($it.InstanceName + '=' + $it.Enable) } "
        "  } "
        "} catch {}; "
        "$out -join [Environment]::NewLine"
    )
    out = run_ps(ps, timeout=90)
    states = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        name, _, value = line.rpartition("=")
        value = value.strip().lower()
        if name and value in ("true", "false"):
            states[name.strip()] = (value == "true")

    global_out = run_ps(
        "try { (Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\USB' "
        "-Name 'DisableSelectiveSuspend' -ErrorAction Stop).DisableSelectiveSuspend } catch { 'NONE' }",
        timeout=30,
    )
    global_val = (global_out or "").strip().splitlines()[-1].strip() if (global_out or "").strip() else "NONE"

    save_backup({
        "usb_power_states": states,
        "usb_disable_selective_suspend": global_val,
    })
    logf(f"  Бэкап: состояние энергосбережения сохранено для {len(states)} USB-устройств.")


def backup_services(logf, service_names):
    """
    Сохраняет текущий тип запуска (StartType) перечисленных служб перед
    их отключением — чтобы восстановление возвращало ИМЕННО прежнее
    значение (Automatic/Manual/Disabled), а не всегда одно и то же.
    """
    states = {}
    for name in service_names:
        out = run_ps(
            f"try {{ (Get-Service -Name '{name}' -ErrorAction Stop).StartType.ToString() }} catch {{ '' }}"
        )
        val = out.strip()
        if val:
            states[name] = val
    save_backup({"services_state": states})
    logf(f"  Бэкап: тип запуска {len(states)} служб сохранён.")


def backup_startup_apps(logf, disabled_entries):
    """
    Сохраняет список записей автозапуска, которые были отключены —
    disabled_entries: список словарей {"kind": "registry"/"folder",
    "hive": ..., "path": ..., "name": ..., "value": ...} — достаточно
    информации, чтобы восстановить именно то, что было отключено.
    """
    save_backup({"startup_apps_disabled": disabled_entries})
    logf(f"  Бэкап: {len(disabled_entries)} записей автозапуска сохранено для восстановления.")


# Категории бэкапа, доступные для точечного восстановления (используется
# и диалогом "Бэкап", и полным restore_from_backup).
BACKUP_CATEGORIES = ("power_plan", "firewall", "visual_effects", "services", "startup_apps", "usb_power")


def restore_from_backup(logf, categories=None):
    """
    Восстанавливает настройки, сохранённые перед изменениями (схема
    электропитания, брандмауэр, визуальные эффекты). Не трогает
    удалённые файлы (это необратимо) — только переключаемые настройки.

    categories — необязательный набор из BACKUP_CATEGORIES: если указан,
    восстанавливаются только выбранные категории (точечное восстановление),
    иначе — всё, что есть в бэкапе.
    """
    data = load_backup()
    if not data:
        logf("Бэкап не найден — восстанавливать нечего.")
        return

    if categories is None:
        categories = set(BACKUP_CATEGORIES)

    logf(f"Восстановление из бэкапа (сохранён: {data.get('saved_at', '?')})...")

    if "power_plan" in categories:
        guid = data.get("power_plan_guid")
        if guid:
            code, out, err = run_cmd(f"powercfg /setactive {guid}", timeout=30)
            logf(f"  Электропитание: восстановлена схема {guid} (код {code}).")
        else:
            logf("  Электропитание: в бэкапе нет сохранённой схемы.")

        # Тайм-ауты возвращаем отдельно: смена схемы не восстанавливает
        # значения, которые step_power_plan обнулил внутри самой схемы.
        timeouts = data.get("power_timeouts") or {}
        restored_timeouts = 0
        setting_map = {
            "standby": ("SUB_SLEEP", "STANDBYIDLE"),
            "monitor": ("SUB_VIDEO", "VIDEOIDLE"),
            "disk": ("SUB_DISK", "DISKIDLE"),
            "usb_suspend": (
                "2a737441-1930-4402-8d77-b2bebba308a3",
                "48e6b7a6-50f5-4782-a5d4-53bb8f07e226",
            ),
        }
        for label, values in timeouts.items():
            if label not in setting_map or not isinstance(values, dict):
                continue
            subgroup, setting = setting_map[label]
            for kind, flag in (("ac", "/setacvalueindex"), ("dc", "/setdcvalueindex")):
                raw = (values.get(kind) or "").strip()
                if not raw:
                    continue
                try:
                    value = int(raw, 16)
                except ValueError:
                    continue
                code, _, _ = run_cmd(
                    f"powercfg {flag} SCHEME_CURRENT {subgroup} {setting} {value}", timeout=15
                )
                if code == 0:
                    restored_timeouts += 1
        if restored_timeouts:
            run_cmd("powercfg /setactive SCHEME_CURRENT", timeout=15)
            logf(f"  Электропитание: восстановлено прежних тайм-аутов: {restored_timeouts}.")
        elif timeouts:
            logf("  Электропитание: тайм-ауты в бэкапе есть, но применить их не удалось.")

    if "usb_power" in categories:
        usb_states = data.get("usb_power_states") or {}
        if usb_states:
            # Возвращаем ровно то значение, что было у каждого
            # устройства: у части галочка могла быть снята и до запуска
            # программы, и "включить всем" исказило бы исходное состояние.
            pairs = "; ".join(
                "@{{N='{0}';V=${1}}}".format(name.replace("'", "''"), "true" if enabled else "false")
                for name, enabled in usb_states.items()
            )
            ps = (
                f"$want = @({pairs}); "
                "$map = @{}; foreach ($p in $want) { $map[$p.N] = $p.V }; "
                "$done = 0; "
                "try { "
                "  foreach ($it in @(Get-CimInstance -Namespace root/WMI -ClassName MSPower_DeviceEnable -ErrorAction Stop)) { "
                "    if ($map.ContainsKey($it.InstanceName)) { "
                "      try { $it.Enable = $map[$it.InstanceName]; Set-CimInstance -InputObject $it -ErrorAction Stop; $done++ } catch {} "
                "    } "
                "  } "
                "} catch {}; "
                "Write-Output $done"
            )
            out = run_ps(ps, timeout=120)
            done = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else "0"
            logf(f"  USB: восстановлено состояние энергосбережения для {done} из {len(usb_states)} устройств.")
        else:
            logf("  USB: в бэкапе нет сохранённого состояния устройств.")

        global_val = (data.get("usb_disable_selective_suspend") or "").strip()
        if global_val and global_val != "NONE":
            run_ps(
                "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\USB' "
                f"-Name 'DisableSelectiveSuspend' -Value {global_val} -Type DWord -ErrorAction SilentlyContinue",
                timeout=30,
            )
            logf(f"  USB: DisableSelectiveSuspend возвращён в прежнее значение ({global_val}).")
        elif global_val == "NONE":
            run_ps(
                "Remove-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\USB' "
                "-Name 'DisableSelectiveSuspend' -ErrorAction SilentlyContinue",
                timeout=30,
            )
            logf("  USB: DisableSelectiveSuspend удалён (до запуска его не было).")

    if "firewall" in categories:
        fw_states = data.get("firewall_state")
        if fw_states:
            for profile, state in fw_states.items():
                if state in ("ON", "OFF"):
                    run_cmd(f"netsh advfirewall set {profile.lower()}profile state {state.lower()}", timeout=15)
            logf(f"  Брандмауэр: состояние профилей восстановлено ({fw_states}).")
        else:
            logf("  Брандмауэр: в бэкапе нет сохранённого состояния.")

    if "visual_effects" in categories:
        visualfx = data.get("visual_fx_setting")
        if visualfx not in (None, ""):
            run_ps(
                "Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VisualEffects' "
                f"-Name VisualFXSetting -Value {visualfx} -Type DWord",
                timeout=15,
            )
            logf(f"  Визуальные эффекты: VisualFXSetting восстановлен ({visualfx}).")

        min_animate = data.get("min_animate")
        if min_animate not in (None, ""):
            run_ps(
                f"Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' -Name MinAnimate -Value '{min_animate}'",
                timeout=15,
            )

        drag_full = data.get("drag_full_windows")
        if drag_full not in (None, ""):
            run_ps(
                f"Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' -Name DragFullWindows -Value '{drag_full}'",
                timeout=15,
            )

        font_smoothing = data.get("font_smoothing")
        if font_smoothing not in (None, ""):
            run_ps(
                f"Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' -Name FontSmoothing -Value '{font_smoothing}'",
                timeout=15,
            )

        font_smoothing_type = data.get("font_smoothing_type")
        if font_smoothing_type not in (None, ""):
            run_ps(
                "Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' -Name FontSmoothingType "
                f"-Value {font_smoothing_type} -Type DWord",
                timeout=15,
            )

        if any(data.get(k) not in (None, "") for k in
               ("visual_fx_setting", "min_animate", "drag_full_windows", "font_smoothing", "font_smoothing_type")):
            logf("  Визуальные эффекты восстановлены (применятся после перезахода/перезапуска explorer.exe).")
        else:
            logf("  Визуальные эффекты: в бэкапе нет сохранённых значений.")

    if "services" in categories:
        services_state = data.get("services_state")
        if services_state:
            restored = 0
            for name, start_type in services_state.items():
                # Map .NET ServiceStartMode обратно на значение Set-Service
                # -StartupType (те же строки для Automatic/Manual/Disabled).
                out = run_ps(
                    f"try {{ Set-Service -Name '{name}' -StartupType {start_type} -ErrorAction Stop; "
                    "Write-Output OK } catch { Write-Output ('ERR:' + $_.Exception.Message) }"
                )
                if out.strip() == "OK":
                    restored += 1
            logf(f"  Службы: восстановлен тип запуска для {restored} из {len(services_state)}.")
        else:
            logf("  Службы: в бэкапе нет сохранённого состояния.")

    if "startup_apps" in categories:
        disabled_entries = data.get("startup_apps_disabled")
        if disabled_entries:
            restored = 0
            for entry in disabled_entries:
                try:
                    if entry.get("kind") == "registry":
                        hive = entry["hive"]
                        reg_path = entry["path"]
                        name = entry["name"]
                        value = entry["value"]
                        escaped_value = value.replace("'", "''")
                        out = run_ps(
                            f"try {{ if (-not (Test-Path '{hive}:\\{reg_path}')) "
                            f"{{ New-Item -Path '{hive}:\\{reg_path}' -Force | Out-Null }}; "
                            f"Set-ItemProperty -Path '{hive}:\\{reg_path}' -Name '{name}' -Value '{escaped_value}'; "
                            "Write-Output OK } catch { Write-Output ('ERR:' + $_.Exception.Message) }"
                        )
                        if out.strip() == "OK":
                            restored += 1
                    elif entry.get("kind") == "folder":
                        # Файлы папки автозагрузки перемещались в бэкап-подпапку
                        # при отключении (см. step_startup_apps) — возвращаем обратно.
                        src = entry.get("backup_path")
                        dst = entry.get("original_path")
                        if src and dst and os.path.isfile(src):
                            shutil.move(src, dst)
                            restored += 1
                except Exception:
                    continue
            logf(f"  Автозапуск приложений: восстановлено {restored} из {len(disabled_entries)} записей.")
        else:
            logf("  Автозапуск приложений: в бэкапе нет сохранённых записей.")

    logf("Восстановление завершено.")


# ============================================================
# Описание шагов: каждый шаг — функция, принимающая callback log(msg)
# и возвращающая ничего (либо строку для накопления в system_info)
# ============================================================

def step_clean_temp(logf):
    logf("Очистка временных файлов и логов...")
    backup_prefetch_snapshot(logf)
    user = os.getenv("USERNAME")
    targets = []

    temp = os.getenv("TEMP")
    if temp:
        targets.append(("TEMP пользователя", temp, 0))
    if user:
        targets.append(("Local Temp", fr"C:\Users\{user}\AppData\Local\Temp", 0))
    targets.append(("Windows Temp", r"C:\Windows\Temp", 0))
    targets.append(("Логи CBS", r"C:\Windows\Logs\CBS", 0))
    targets.append(("Логи DISM", r"C:\Windows\Logs\DISM", 0))
    targets.append(("Prefetch (>24ч)", r"C:\Windows\Prefetch", 24 * 60))
    targets.append(("WER ReportQueue", r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue", 0))
    targets.append(("WER ReportArchive", r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive", 0))
    if user:
        targets.append(("Кэш миниатюр Explorer", fr"C:\Users\{user}\AppData\Local\Microsoft\Windows\Explorer", 0))

    # В журнал попадают только папки, где реально что-то удалено.
    # Раньше печаталась строка на каждую из девяти папок, включая
    # "удалено 0, пропущено 0" — это давало десяток строк шума на
    # пункт, из которых полезны были одна-две.
    total_freed = 0
    total_deleted = 0
    total_skipped = 0
    cleaned_labels = []
    for label, folder, min_age in targets:
        deleted, errors, freed = safe_delete_files_in(folder, min_age_minutes=min_age)
        total_freed += freed
        total_deleted += deleted
        total_skipped += errors
        if deleted:
            cleaned_labels.append(f"{label} ({deleted})")

    run_cmd("net stop wuauserv", timeout=30)
    deleted, errors, freed = safe_delete_files_in(r"C:\Windows\SoftwareDistribution\Download")
    run_cmd("net start wuauserv", timeout=30)
    total_freed += freed
    total_deleted += deleted
    total_skipped += errors
    if deleted:
        cleaned_labels.append(f"кэш обновлений Windows ({deleted})")

    # Объём здесь намеренно НЕ печатается: он уже выводится в заголовке
    # пункта отчёта ("— освобождено X ГБ") и в общем итоге сверху.
    # Раньше одно и то же число встречалось в отчёте трижды.
    if cleaned_labels:
        logf(f"  Очищено: {', '.join(cleaned_labels)}.")
    logf(
        f"  Удалено файлов: {total_deleted}"
        + (f", занято другими процессами и пропущено: {total_skipped}." if total_skipped else ".")
    )
    report_freed_bytes(logf, total_freed)


def step_recycle_bin(logf):
    logf("Очистка корзины...")

    # Объём корзины замеряем ДО очистки: Clear-RecycleBin не сообщает,
    # сколько освободила, а без этого пункт не попадал бы в итог
    # "Освобождено места" вообще (см. report_freed_bytes).
    size_out = run_ps(
        "$s = 0; "
        "Get-ChildItem -LiteralPath 'C:\\$Recycle.Bin' -Recurse -Force -ErrorAction SilentlyContinue | "
        "ForEach-Object { if (-not $_.PSIsContainer) { $s += $_.Length } }; "
        "Write-Output $s",
        timeout=60,
    )
    try:
        size_before = int((size_out or "0").strip().splitlines()[-1].strip())
    except (ValueError, IndexError):
        size_before = 0

    out = run_ps(
        "try { Clear-RecycleBin -Confirm:$false -ErrorAction Stop; Write-Output OK } "
        "catch { Write-Output $_.Exception.Message }",
        timeout=60,
    )
    result = (out or "нет ответа").strip()
    if result != "OK":
        logf(f"  Не удалось очистить корзину: {result}")
    elif size_before > 0:
        # Объём не дублируем — он в заголовке пункта отчёта.
        logf("  Корзина очищена.")
        report_freed_bytes(logf, size_before)
    else:
        logf("  Корзина была пуста.")


# Два независимых профиля очистки диска (номера произвольные, использует
# только эта программа). Раньше был один профиль, в который помечались
# ВСЕ категории VolumeCaches разом — включая тяжёлые, связанные с
# обслуживанием хранилища компонентов Windows. Эти категории cleanmgr не
# обрабатывает сам, а перекладывает на TiWorker.exe ("Установщик модулей
# Windows"), который на машине с накопленными обновлениями работает
# 20-60 минут. Снаружи это выглядело как зависание пункта "Очистка
# диска": окно давно закрылось, а процессы cleanmgr.exe всё ещё висят.
# Теперь лёгкие категории идут в быстрый основной пункт, а тяжёлые — в
# отдельный пункт "Глубокая очистка обновлений Windows", который
# отмечается вручную, когда есть время его дождаться.
CLEANMGR_SAGESET_ID = "65432"        # лёгкие категории (обычная очистка диска)
CLEANMGR_DEEP_SAGESET_ID = "65433"   # тяжёлые категории (глубокая очистка обновлений)

# Категории VolumeCaches, которые запускают обслуживание хранилища
# компонентов через TiWorker.exe и потому непредсказуемо долгие.
# Имена ключей реестра стабильны между версиями Windows 10/11.
CLEANMGR_HEAVY_CATEGORIES = (
    "Update Cleanup",                     # Очистка обновлений Windows
    "Previous Installations",             # Предыдущие установки Windows
    "Windows ESD installation files",     # Файлы установки ESD
    "Delivery Optimization Files",        # Файлы оптимизации доставки
    "Windows Upgrade Log Files",          # Журналы обновления Windows
    "Setup Log Files",                    # Журналы установки
    # Ниже — категории, тяжёлые не удалением, а ЭТАПОМ ОЦЕНКИ: cleanmgr
    # обходит по ним огромные деревья файлов, показывая окно "Программа
    # очистки оценивает объём места...". Именно на "Диагностических
    # данных" очистка визуально замирала на минуты, хотя удалять там
    # почти нечего — держим их в глубоком профиле.
    "Diagnostic Data Viewer Database Files",  # Диагностические данные
    "Feedback Hub Archive log files",         # Архив Центра отзывов
    "Windows Defender",                       # Файлы Защитника Windows
    "System error memory dump files",         # Дампы памяти
    "System error minidump files",            # Малые дампы памяти
)

# Жёсткий предел на весь пункт очистки диска. Достигается редко —
# обычно раньше срабатывает детектор простоя (см. _cleanmgr_cpu_seconds),
# но без этого предела цикл опроса мог ждать вечно.
CLEANMGR_MAX_SECONDS = 15 * 60

# Если суммарное процессорное время cleanmgr.exe + TiWorker.exe не растёт
# столько секунд подряд — считаем, что процесс завис (а не работает
# медленно), и прерываем, не дожидаясь общего таймаута.
CLEANMGR_STALL_SECONDS = 4 * 60


def _cleanmgr_cpu_seconds():
    """
    Суммарное процессорное время (в секундах) всех процессов, участвующих
    в очистке диска: самого cleanmgr.exe и TiWorker.exe, которому он
    делегирует тяжёлые категории. Растущее значение = работа реально
    идёт, пусть и медленно; замершее = процесс завис и ждать бесполезно.

    Это надёжнее, чем следить за свободным местом на диске (место может
    не меняться подолгу в середине легитимной работы) или за наличием
    окна (окно cleanmgr закрывается задолго до конца работы TiWorker).
    Возвращает None, если опросить не удалось — вызывающий код тогда
    просто не обновляет детектор простоя.
    """
    ps = (
        "$total = 0; "
        "foreach ($n in @('cleanmgr','TiWorker')) { "
        "  Get-Process -Name $n -ErrorAction SilentlyContinue | "
        "  ForEach-Object { try { $total += $_.CPU } catch {} } "
        "}; "
        "Write-Output $total"
    )
    out = run_ps(ps, timeout=15)
    try:
        return float((out or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _run_cleanmgr_profile(logf, sageset_id, heavy, on_pid=None, should_stop=None):
    """
    Общая реализация запуска cleanmgr для обоих пунктов очистки диска.

    heavy=False — в профиль попадают все категории VolumeCaches, КРОМЕ
    перечисленных в CLEANMGR_HEAVY_CATEGORIES; heavy=True — наоборот,
    только они. Разделение сделано потому, что тяжёлые категории
    делегируют работу TiWorker.exe и идут непредсказуемо долго; смешивать
    их с быстрыми в одном пункте означало, что обычная очистка диска
    случайным образом то занимает минуту, то висит час.

    Ожидание завершения не может опираться на proc.wait() исходного PID:
    cleanmgr порождает дополнительные процессы с тем же именем, из-за
    чего исходный PID завершается, пока реальная очистка продолжается.
    Поэтому опрашиваем систему по имени процесса. Чтобы это ожидание не
    стало бесконечным, есть два предохранителя:
      * жёсткий общий таймаут CLEANMGR_MAX_SECONDS;
      * детектор простоя: если суммарное процессорное время cleanmgr.exe
        и TiWorker.exe не растёт CLEANMGR_STALL_SECONDS подряд — работа
        не идёт, ждать дальше бессмысленно.
    В обоих случаях процессы cleanmgr.exe принудительно завершаются, а
    пункт помечается выполненным с пояснением в отчёте — вместо
    бесконечного "выполняется" в интерфейсе.

    on_pid вызывается с PID запущенного процесса (для кнопки "Завершить
    очистку диска" в GUI). should_stop — функция без аргументов,
    возвращающая True, если пользователь запросил остановку.
    """
    kind = "тяжёлые (обслуживание хранилища компонентов)" if heavy else "быстрые"
    logf(f"Настройка профиля очистки диска — категории: {kind}...")

    # Сначала сбрасываем StateFlags этого профиля у ВСЕХ категорий, потом
    # выставляем 2 только нужным. Без сброса категория, попавшая в профиль
    # в прошлой версии программы, осталась бы отмеченной навсегда — именно
    # так тяжёлые категории могли бы продолжать тормозить быстрый пункт
    # после обновления.
    heavy_list = ", ".join(f"'{c}'" for c in CLEANMGR_HEAVY_CATEGORIES)
    want_value = "2" if heavy else "0"
    other_value = "0" if heavy else "2"
    ps = (
        "$base = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VolumeCaches'; "
        f"$heavy = @({heavy_list}); "
        "$selected = 0; "
        "Get-ChildItem $base | ForEach-Object { "
        "  $isHeavy = $heavy -contains $_.PSChildName; "
        f"  $val = if ($isHeavy) {{ {want_value} }} else {{ {other_value} }}; "
        f"  try {{ Set-ItemProperty -Path $_.PsPath -Name 'StateFlags{sageset_id}' -Value $val -Type DWord -ErrorAction Stop; "
        "         if ($val -eq 2) { $selected++ } } catch {} "
        "}; "
        "Write-Output $selected"
    )
    out = run_ps(ps, timeout=60)
    selected = (out or "").strip()
    if selected.isdigit() and int(selected) > 0:
        logf(f"  В профиль включено категорий: {selected}.")
    elif selected.isdigit():
        logf("  Ни одна категория этого типа не доступна в системе — пропущено.")
        return
    else:
        logf("  Не удалось настроить профиль через реестр — пункт пропущен.")
        return

    logf(f"Запуск cleanmgr /sagerun:{sageset_id}...")
    if heavy:
        logf("  Тяжёлые категории обрабатывает TiWorker.exe («Установщик модулей")
        logf("  Windows») — окно cleanmgr может закрыться задолго до конца работы.")

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    cleanmgr_path = os.path.join(system_root, "System32", "cleanmgr.exe")
    try:
        proc = subprocess.Popen(
            [cleanmgr_path, f"/sagerun:{sageset_id}"],
            cwd=SAFE_CWD,
        )
    except Exception as e:
        logf(f"  Не удалось запустить cleanmgr: {e}")
        return

    if on_pid:
        try:
            on_pid(proc.pid)
        except Exception:
            pass

    # Даём процессу мгновение стартовать, прежде чем начинать опрос по
    # имени — иначе первая проверка может ещё не увидеть только что
    # созданный процесс.
    time.sleep(1.5)

    poll_interval = 2
    consecutive_errors = 0
    started_at = time.time()
    last_cpu = _cleanmgr_cpu_seconds()
    last_progress_at = time.time()

    while True:
        if should_stop and should_stop():
            _kill_processes_by_name("cleanmgr.exe")
            logf("  Очистка диска остановлена принудительно пользователем.")
            return

        elapsed = time.time() - started_at
        if elapsed >= CLEANMGR_MAX_SECONDS:
            _kill_processes_by_name("cleanmgr.exe")
            logf(
                f"  Достигнут общий предел времени ({format_eta(int(CLEANMGR_MAX_SECONDS))}) — "
                "очистка прервана принудительно."
            )
            logf("  Часть категорий могла не успеть обработаться; повторный запуск продолжит с места остановки.")
            return

        count = _count_processes_by_name("cleanmgr.exe")
        if count == 0:
            break
        if count == -1:
            # tasklist недоступен (редкость) — не можем надёжно
            # определить статус; после нескольких подряд ошибок
            # откатываемся на ожидание исходного процесса, чтобы не
            # зависнуть в цикле навечно.
            consecutive_errors += 1
            if consecutive_errors >= 5:
                logf("  Не удалось опросить список процессов, ожидаем исходный процесс напрямую...")
                try:
                    proc.wait(timeout=max(60, CLEANMGR_MAX_SECONDS - elapsed))
                except Exception:
                    _kill_processes_by_name("cleanmgr.exe")
                    logf("  Исходный процесс не завершился в отведённое время — прервано.")
                break
        else:
            consecutive_errors = 0

        # Детектор простоя: растёт ли процессорное время участников очистки.
        cpu_now = _cleanmgr_cpu_seconds()
        if cpu_now is not None:
            if last_cpu is None or cpu_now > last_cpu + 0.5:
                last_cpu = cpu_now
                last_progress_at = time.time()
            elif time.time() - last_progress_at >= CLEANMGR_STALL_SECONDS:
                _kill_processes_by_name("cleanmgr.exe")
                logf(
                    f"  Очистка не подаёт признаков работы {format_eta(int(CLEANMGR_STALL_SECONDS))} "
                    "(процессорное время не растёт) — считаем зависанием, прервано."
                )
                return

        time.sleep(poll_interval)

    logf(f"  cleanmgr завершён за {format_eta(int(time.time() - started_at))} — все процессы закрыты.")


def step_cleanmgr(logf, on_pid=None, should_stop=None):
    """
    Быстрая очистка диска: все категории cleanmgr, кроме тяжёлых
    (обслуживание хранилища компонентов Windows) — они вынесены в
    отдельный пункт step_cleanmgr_deep.
    """
    return _run_cleanmgr_profile(
        logf, CLEANMGR_SAGESET_ID, heavy=False, on_pid=on_pid, should_stop=should_stop,
    )


def step_cleanmgr_deep(logf, on_pid=None, should_stop=None):
    """
    Глубокая очистка обновлений Windows: тяжёлые категории cleanmgr плюс
    DISM /StartComponentCleanup, который сжимает хранилище компонентов
    (WinSxS) — вместе они освобождают заметно больше места, чем обычная
    очистка диска, но требуют времени и обслуживают одно и то же
    хранилище, поэтому логично идут одним пунктом.
    """
    _run_cleanmgr_profile(
        logf, CLEANMGR_DEEP_SAGESET_ID, heavy=True, on_pid=on_pid, should_stop=should_stop,
    )

    if should_stop and should_stop():
        logf("  DISM пропущен — выполнение остановлено пользователем.")
        return

    logf("Сжатие хранилища компонентов: DISM /StartComponentCleanup...")
    code, out, err = run_cmd(
        "DISM /Online /Cleanup-Image /StartComponentCleanup", timeout=CLEANMGR_MAX_SECONDS,
    )
    if code == 0:
        logf("  Хранилище компонентов очищено (устаревшие версии обновлений удалены).")
    else:
        logf(f"  DISM завершился с кодом {code} — часть обновлений могла остаться: {(err or out or '').strip()[:200]}")


def step_sfc_dism(logf):
    logf("Проверка целостности системы: sfc /scannow (может занять 10-30 минут)...")
    code, out, err = run_cmd("sfc /scannow", timeout=1800)
    logf(f"  sfc /scannow завершён, код: {code}")

    logf("DISM /Online /Cleanup-Image /RestoreHealth (может занять время)...")
    code, out, err = run_cmd("DISM /Online /Cleanup-Image /RestoreHealth", timeout=1800)
    logf(f"  DISM завершён, код: {code}")


def step_defrag(logf):
    logf("Определение типа диска C:...")
    media_type = get_disk_media_type("C")
    logf(f"  Тип диска: {media_type}")

    if media_type == "SSD":
        logf("  SSD — выполняется TRIM (без полной дефрагментации)...")
        code, out, err = run_cmd("defrag C: /L /V", timeout=600)
        logf(f"  TRIM завершён, код: {code}")
    elif media_type == "HDD":
        logf("  HDD — выполняется полная дефрагментация...")
        code, out, err = run_cmd("defrag C: /U /V", timeout=1800)
        logf(f"  Дефрагментация завершена, код: {code}")
    else:
        logf("  Тип не определён — безопасный режим defrag C: /O...")
        code, out, err = run_cmd("defrag C: /O /V", timeout=1800)
        logf(f"  Оптимизация завершена, код: {code}")


def step_power_plan(logf):
    """
    Активирует схему "Высокая производительность" и дополнительно
    настраивает её под моноблоки/планшеты с iikoFront: система не должна
    засыпать или гасить экран при простое (POS-терминал может стоять без
    касаний подолгу между заказами, но должен быть готов сразу), а порты
    USB не должны отключаться для экономии энергии (иначе периодически
    отваливаются сканер штрихкодов, чековый принтер, фискальный
    регистратор и подобная периферия, подключенная через USB).
    """
    logf("Настройка электропитания: высокая производительность...")
    backup_power_plan(logf)
    HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
    code, out, err = run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=30)
    if code == 0:
        logf("  Схема активирована.")
    else:
        logf("  Схема не найдена, создаём из шаблона...")
        run_cmd(f"powercfg -duplicatescheme {HIGH_PERF_GUID}", timeout=30)
        code2, out2, err2 = run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=30)
        if code2 == 0:
            logf("  Схема создана и активирована.")
        else:
            logf(f"  Не удалось активировать (код {code2}): {err2 or err}")

    # Сон/гашение экрана при простое — отключаем и от сети, и от
    # батареи (на планшетах может быть аккумулятор), чтобы моноблок
    # никогда не засыпал сам по себе во время работы кассы.
    idle_settings = [
        ("standby-timeout-ac", "Сон (от сети)"),
        ("standby-timeout-dc", "Сон (от батареи)"),
        ("monitor-timeout-ac", "Отключение экрана (от сети)"),
        ("monitor-timeout-dc", "Отключение экрана (от батареи)"),
        ("disk-timeout-ac", "Отключение дисков (от сети)"),
        ("disk-timeout-dc", "Отключение дисков (от батареи)"),
    ]
    idle_ok = True
    for setting, label in idle_settings:
        c, _, e = run_cmd(f"powercfg /change {setting} 0", timeout=15)
        if c != 0:
            idle_ok = False
            logf(f"  {label}: не удалось отключить ({e})")
    if idle_ok:
        logf("  Сон и отключение экрана/дисков при простое отключены (всегда активен).")

    # USB selective suspend — та самая галочка "Разрешить отключение
    # этого устройства для экономии энергии" в диспетчере устройств,
    # но выключенная централизованно через политику электропитания
    # (действует для всех USB-концентраторов сразу). GUID подраздела
    # USB settings / USB selective suspend setting — стандартные для
    # всех схем Windows.
    USB_SUBGROUP = "2a737441-1930-4402-8d77-b2bebba308a3"
    USB_SETTING = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"
    c1, _, e1 = run_cmd(f"powercfg /setacvalueindex {HIGH_PERF_GUID} {USB_SUBGROUP} {USB_SETTING} 0", timeout=15)
    c2, _, e2 = run_cmd(f"powercfg /setdcvalueindex {HIGH_PERF_GUID} {USB_SUBGROUP} {USB_SETTING} 0", timeout=15)
    run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=15)
    if c1 == 0 and c2 == 0:
        logf("  Автоматическое отключение USB-портов для экономии энергии выключено.")
    else:
        logf(f"  Не удалось отключить USB selective suspend (коды {c1}/{c2}): {e1 or e2}")


def step_firewall(logf):
    logf("Отключение брандмауэра во всех профилях...")
    backup_firewall(logf)
    code, out, err = run_cmd("netsh advfirewall set allprofiles state off", timeout=30)
    if code == 0:
        logf("  Брандмауэр отключён (Domain/Private/Public).")
    else:
        logf(f"  Не удалось отключить (код {code}): {err}")


def step_visual_effects(logf):
    """
    Переключает визуальные эффекты Windows в режим "Обеспечить наилучшее
    быстродействие" (VisualFXSetting=2), но отдельно возвращает/оставляет
    включённым сглаживание экранных шрифтов (FontSmoothing), которое этот
    режим по умолчанию отключает вместе со всем остальным.
    """
    logf("Настройка визуальных эффектов: быстродействие...")
    backup_visual_effects(logf)

    ps = (
        "$path = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VisualEffects'; "
        "if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }; "
        "Set-ItemProperty -Path $path -Name VisualFXSetting -Value 2 -Type DWord; "
        "Write-Output OK"
    )
    out = run_ps(ps, timeout=30)
    if out.strip() == "OK":
        logf("  Режим 'Наилучшее быстродействие' установлен (VisualFXSetting=2).")
    else:
        logf("  Не удалось установить VisualFXSetting через реестр.")

    # Быстродействие также применяется через SystemParametersInfo — используем
    # SPI_SETUIEFFECTS/анимации по отдельности, но проще и надёжнее продублировать
    # непосредственно нужные разделы реестра, которые правит панель "Быстродействие":
    reg_perf = (
        # Отключаем анимации, тени, прозрачность и т.д.
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop" -Name UserPreferencesMask '
        '-Value ([byte[]](0x90,0x12,0x01,0x80,0x10,0x00,0x00,0x00)) -Type Binary; '
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop\\WindowMetrics" -Name MinAnimate -Value "0"; '
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop" -Name DragFullWindows -Value "0"; '
        'Write-Output OK'
    )
    run_ps(reg_perf, timeout=30)

    # Принудительно оставляем/включаем сглаживание шрифтов (ClearType),
    # которое маска UserPreferencesMask выше могла выключить.
    ps_fonts = (
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop" -Name FontSmoothing -Value "2"; '
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop" -Name FontSmoothingType -Value 2 -Type DWord; '
        'Write-Output OK'
    )
    out_fonts = run_ps(ps_fonts, timeout=30)
    if out_fonts.strip() == "OK":
        logf("  Сглаживание экранных шрифтов (ClearType) оставлено включённым.")
    else:
        logf("  Не удалось подтвердить настройку сглаживания шрифтов.")

    logf("  Изменения вступят в силу после перезахода в систему или перезапуска explorer.exe.")


# Службы Windows, безопасные для отключения на большинстве компьютеров и
# конкретно на моноблоках/планшетах с iikoFront: намеренно НЕ включает
# ничего, что может задеть POS-специфику, сеть, печать, звук, USB-периферию
# (сканеры штрихкодов/чекпринтеры часто USB) или сам Windows Update.
# Отключается только тип автозапуска (Disabled), а не остановка "навсегда" —
# служба не запустится при следующей загрузке, но её можно запустить
# вручную при необходимости, и восстановление возвращает исходный тип.
SAFE_DISABLE_SERVICES = {
    "Fax": "Факс",
    "MapsBroker": "Загрузчик плиток карт Windows",
    "RemoteRegistry": "Удалённый реестр",
    "RetailDemo": "Демонстрационный режим витрины",
    "WerSvc": "Служба регистрации ошибок Windows",
    "diagnosticshub.standardcollector.service": "Служба сбора стандартной диагностики Microsoft (R)",
    "DiagTrack": "Функциональные возможности подключённых пользователей и телеметрия",
    "dmwappushservice": "Служба push-сообщений WAP",
    "lfsvc": "Служба обнаружения местоположения",
    "SharedAccess": "Общий доступ к подключению интернета (ICS)",
    "TabletInputService": "Служба ввода планшетного ПК",
    "WMPNetworkSvc": "Служба общего доступа к сети проигрывателя Windows Media",
    "WSearch": "Windows Search (индексирование файлов)",
}


def get_service_startup_type(name):
    """
    Возвращает текущий StartupType службы ("Automatic"/"Manual"/
    "Disabled"/...) или None, если служба не найдена в системе.
    """
    out = run_ps(f"try {{ (Get-Service -Name '{name}' -ErrorAction Stop).StartType.ToString() }} catch {{ '' }}")
    val = out.strip()
    return val if val else None


def set_service_disabled(name, disabled):
    """
    Мгновенно переключает одну службу: disabled=True — останавливает и
    ставит StartupType=Disabled; disabled=False — возвращает Manual
    (безопасный дефолт для большинства служб из SAFE_DISABLE_SERVICES,
    не запускает её принудительно, просто разрешает запуск по требованию).
    Возвращает True при успехе, False при ошибке/отсутствии службы.
    """
    if disabled:
        ps = (
            f"try {{ Stop-Service -Name '{name}' -Force -ErrorAction SilentlyContinue; "
            f"Set-Service -Name '{name}' -StartupType Disabled -ErrorAction Stop; "
            "Write-Output OK } catch { Write-Output 'ERR' }"
        )
    else:
        ps = (
            f"try {{ Set-Service -Name '{name}' -StartupType Manual -ErrorAction Stop; "
            "Write-Output OK } catch { Write-Output 'ERR' }"
        )
    out = run_ps(ps)
    return out.strip() == "OK"


def step_disable_services(logf, selected_names=None):
    """
    Отключает автозапуск выбранного набора служб — по умолчанию (если
    selected_names не передан) используется весь безопасный список
    SAFE_DISABLE_SERVICES, но GUI может передать конкретное подмножество,
    если пользователь развернул пункт и снял отметки с части служб.
    Каждая служба обрабатывается независимо — если одной из них нет в
    системе (уже не установлена/зависит от редакции Windows), остальные
    всё равно отключаются.
    """
    logf("Отключение автозапуска неиспользуемых служб Windows...")
    names = list(selected_names) if selected_names is not None else list(SAFE_DISABLE_SERVICES.keys())
    if not names:
        logf("  Ни одна служба не выбрана — пропущено.")
        return
    backup_services(logf, names)

    disabled_count = 0
    skipped_count = 0
    for name in names:
        if set_service_disabled(name, True):
            disabled_count += 1
        else:
            skipped_count += 1
    logf(f"  Отключено служб: {disabled_count}, пропущено (не найдены в системе): {skipped_count}.")


def step_restore_points(logf):
    """
    Удаляет старые точки восстановления системы (System Restore), оставляя
    только самую свежую. На тесных SSD моноблоков (часто 64-128 ГБ) старые
    точки восстановления могут занимать заметную часть места. Точки
    восстановления не входят в бэкап sclean и не восстанавливаются им —
    это отдельный, независимый механизм самой Windows.
    """
    logf("Очистка старых точек восстановления...")
    out = run_ps(
        "try { "
        "$points = Get-ComputerRestorePoint -ErrorAction Stop | Sort-Object CreationTime; "
        "if ($points.Count -le 1) { Write-Output 'NOTHING' } else { Write-Output $points.Count } "
        "} catch { Write-Output 'UNSUPPORTED' }"
    )
    result = out.strip()
    if result == "UNSUPPORTED":
        logf("  Защита системы не включена или точки восстановления недоступны — пропущено.")
        return
    if result == "NOTHING" or not result:
        logf("  Старых точек восстановления не найдено (0-1 точка) — нечего удалять.")
        return

    # vssadmin позволяет удалить все теневые копии диска C кроме самой
    # свежей одной командой — проще и надёжнее, чем перебирать
    # Get-ComputerRestorePoint по одной точке (нет прямого cmdlet для
    # удаления конкретной точки без стороннего модуля).
    code, cmd_out, err = run_cmd(
        'vssadmin delete shadows /for=C: /oldest /quiet', timeout=120
    )
    logf(f"  Удаление старых теневых копий (было точек: {result}), код: {code}.")


def step_usb_power_management(logf):
    """
    Отключает галочку "Разрешить отключение этого устройства для
    экономии энергии" (свойства устройства -> вкладка "Управление
    электропитанием") для всех USB-концентраторов.

    История вопроса — почему тут именно WMI, а не реестр. Первые две
    версии этого пункта писали значения в
    HKLM\\SYSTEM\\CurrentControlSet\\Enum\\<устройство>\\Device Parameters
    (сначала EnhancedPowerManagementEnabled, потом ещё и
    SelectiveSuspendEnabled). Запись проходила успешно и подтверждалась
    обратным чтением — но галочка в диспетчере устройств оставалась
    отмеченной даже после перезагрузки. Значит, галочка читает НЕ эти
    значения: они влияют на поведение драйвера, а не на состояние,
    которое показывает и меняет вкладка "Управление электропитанием".

    За саму галочку отвечает WMI-класс MSPower_DeviceEnable в
    пространстве имён root\\WMI: у каждого устройства, у которого эта
    вкладка есть, там ровно один экземпляр с булевым свойством Enable.
    Именно его переключает диспетчер устройств, поэтому запись туда —
    единственный способ снять галочку программно. Применяется сразу,
    без перезагрузки.

    Реестровые значения и машинный переключатель
    Services\\USB\\DisableSelectiveSuspend=1 оставлены дополнительно:
    они не двигают галочку, но запрещают выборочную приостановку USB на
    уровне драйвера и системы. Для POS-периферии (сканеры штрихкодов,
    чековые принтеры), которая отваливается при засыпании порта, важен
    именно суммарный эффект.
    """
    logf("Отключение энергосбережения USB-портов...")
    backup_usb_power(logf)

    # 1. Галочка "Разрешить отключение этого устройства для экономии
    # энергии" — через WMI. Это основной способ; всё остальное ниже
    # лишь подстраховка на уровне драйвера.
    # Отбор устройств идёт по ДВУМ признакам, а не только по префиксу
    # "USB\" в имени экземпляра. Хост-контроллеры ("Расширяемый
    # хост-контроллер AMD USB 3.10") перечислены системой не под USB\,
    # а под PCI\VEN_..., поэтому фильтр по одному префиксу их пропускал:
    # отчёт показывал 14 из 14 снятых, а галочка у контроллера
    # оставалась. Теперь дополнительно берутся все устройства класса USB
    # (это ровно раздел "Контроллеры USB" в диспетчере устройств),
    # независимо от того, какой шиной они перечислены.
    wmi_ps = (
        "$want = @{}; "
        "foreach ($d in (Get-PnpDevice -Class 'USB' -ErrorAction SilentlyContinue)) { "
        "  if ($d.InstanceId) { $want[$d.InstanceId.ToUpper()] = $true } "
        "}; "
        "function Test-Match($name) { "
        "  if (-not $name) { return $false }; "
        "  $n = $name.ToUpper(); "
        "  if ($n.EndsWith('_0')) { $n = $n.Substring(0, $n.Length - 2) }; "
        "  if ($n.StartsWith('USB\\')) { return $true }; "
        "  return $want.ContainsKey($n) "
        "}; "
        "$total = 0; $changed = 0; $failed = 0; "
        "try { $items = @(Get-CimInstance -Namespace root/WMI -ClassName MSPower_DeviceEnable -ErrorAction Stop) } "
        "catch { $items = @() }; "
        "foreach ($it in $items) { "
        "  if (-not (Test-Match $it.InstanceName)) { continue }; "
        "  $total++; "
        "  if ($it.Enable -eq $false) { continue }; "
        "  try { "
        "    $it.Enable = $false; "
        "    Set-CimInstance -InputObject $it -ErrorAction Stop; "
        "    $changed++; "
        "  } catch { $failed++; continue } "
        "}; "
        "$after = -1; "
        "try { "
        "  $after = 0; "
        "  foreach ($it in @(Get-CimInstance -Namespace root/WMI -ClassName MSPower_DeviceEnable -ErrorAction Stop)) { "
        "    if ((Test-Match $it.InstanceName) -and ($it.Enable -eq $false)) { $after++ } "
        "  } "
        "} catch { $after = -1 }; "
        "Write-Output ($total.ToString() + '|' + $changed.ToString() + '|' + $failed.ToString() + '|' + $after.ToString())"
    )
    out = run_ps(wmi_ps, timeout=120)
    parts = (out or "").strip().split("|")
    wmi_ok = False
    if len(parts) == 4 and all(p.lstrip("-").isdigit() for p in parts):
        total, changed, failed, after = (int(p) for p in parts)
        if total == 0:
            logf("  WMI не сообщил ни об одном USB-устройстве с вкладкой управления электропитанием.")
        else:
            wmi_ok = True
            logf(f"  Галочка энергосбережения: устройств с этой настройкой {total}, снято сейчас {changed}, "
                 f"уже было снято {total - changed - failed}.")
            if after >= 0:
                logf(f"  Проверка после записи: галочка снята у {after} из {total}.")
            if failed:
                logf(f"  Не удалось изменить {failed} устройств (драйвер запретил изменение).")
    else:
        logf(f"  Не удалось обратиться к WMI MSPower_DeviceEnable (ответ: {(out or '').strip()[:120] or 'нет'}).")

    # 2. Глобальный запрет выборочной приостановки USB (на уровне
    # системы, поверх настроек отдельных устройств).
    global_ps = (
        "$k = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\USB'; "
        "try { "
        "  if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }; "
        "  Set-ItemProperty -Path $k -Name 'DisableSelectiveSuspend' -Value 1 -Type DWord -ErrorAction Stop; "
        "  $v = (Get-ItemProperty -Path $k -Name 'DisableSelectiveSuspend').DisableSelectiveSuspend; "
        "  Write-Output $v "
        "} catch { Write-Output 'ERR' }"
    )
    gout = (run_ps(global_ps, timeout=30) or "").strip()
    if gout == "1":
        logf("  Глобальный запрет выборочной приостановки USB включён (Services\\USB\\DisableSelectiveSuspend=1).")
    else:
        logf(f"  Не удалось включить глобальный запрет выборочной приостановки USB (ответ: {gout or 'нет'}).")

    # 3. Значения уровня драйвера по каждому устройству.
    ps = (
        "$total = 0; $set = 0; "
        "$devs = Get-PnpDevice -Class 'USB' -ErrorAction SilentlyContinue; "
        "foreach ($d in $devs) { "
        "  if (-not $d.InstanceId) { continue }; "
        "  $total++; "
        "  $key = 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\' + $d.InstanceId + '\\Device Parameters'; "
        "  if (-not (Test-Path $key)) { continue }; "
        "  try { "
        "    Set-ItemProperty -Path $key -Name 'SelectiveSuspendEnabled' -Value 0 -Type DWord -ErrorAction Stop; "
        "    Set-ItemProperty -Path $key -Name 'EnhancedPowerManagementEnabled' -Value 0 -Type DWord -ErrorAction SilentlyContinue; "
        "    $set++; "
        "  } catch {} "
        "}; "
        "Write-Output ($total.ToString() + '|' + $set.ToString())"
    )
    out2 = run_ps(ps, timeout=120)
    parts2 = (out2 or "").strip().split("|")
    if len(parts2) == 2 and all(p.isdigit() for p in parts2):
        total2, set2 = (int(p) for p in parts2)
        logf(f"  Настройки уровня драйвера записаны для {set2} из {total2} USB-устройств.")
    else:
        logf("  Настройки уровня драйвера записать не удалось.")

    if not wmi_ok:
        logf("  Внимание: основной способ (WMI) не сработал — галочка в диспетчере")
        logf("  устройств могла остаться отмеченной, хотя запрет на уровне системы записан.")


def collect_hardware_diagnostics(logf=None):
    """
    Диагностика железа и периферии: ничего не меняет, только собирает
    состояние и подсвечивает проблемы. Смысл — на удалённом моноблоке
    сразу увидеть то, ради чего обычно лезут в три разных окна: не
    сыплется ли диск, не кончается ли место, все ли устройства
    поднялись без ошибок и вся ли периферия на месте.

    Раньше это был пункт списка задач, который нужно было отметить и
    запустить. Теперь диагностика выполняется сама при старте программы
    и показывается в шапке под сведениями о системе, поэтому функция
    возвращает не только текст, но и список проблем отдельно —
    интерфейсу нужно знать, сколько их, не разбирая текст обратно.

    Возвращает (text, problems): готовый текст блока и список строк с
    проблемами. logf необязателен — при вызове из фонового потока GUI
    построчный журнал никому не нужен.
    """
    if logf is None:
        def logf(_msg):
            pass

    logf("Диагностика железа и периферии...")
    lines = ["[Диагностика железа и периферии]"]
    problems = []

    # --- Процессор: модель, ядра, загрузка, температура ---
    # LoadPercentage берём как мгновенный срез: на кассе фоновая
    # загрузка под 100% при простое — верный признак, что что-то
    # зациклилось (антивирус, обновления, зависшая задача).
    cpu_ps = (
        "$c = Get-CimInstance Win32_Processor | Select-Object -First 1; "
        "$t = ''; "
        "try { "
        "  $z = Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
        "       Select-Object -First 1; "
        "  if ($z) { $t = [math]::Round(($z.CurrentTemperature / 10) - 273.15, 1) } "
        "} catch {}; "
        "Write-Output \"$($c.Name)|$($c.NumberOfCores)|$($c.NumberOfLogicalProcessors)|"
        "$($c.MaxClockSpeed)|$($c.LoadPercentage)|$t\""
    )
    cpu_out = run_ps(cpu_ps, timeout=60)
    cpu_line = next((l.strip() for l in (cpu_out or "").splitlines() if "|" in l), "")
    if cpu_line:
        parts = [p.strip() for p in cpu_line.split("|")]
        parts += [""] * (6 - len(parts))
        name, cores, threads, mhz, load, temp = parts[:6]
        detail = f"Процессор: {name}"
        if cores and threads:
            detail += f" — {cores} ядер / {threads} потоков"
        if mhz:
            detail += f", {mhz} МГц"
        if load:
            detail += f", загрузка {load}%"
        if temp:
            detail += f", {temp} °C"
        else:
            # MSAcpi_ThermalZoneTemperature на большинстве настольных
            # плат не заполняется — это не ошибка сбора, поэтому пишем
            # прямо, а не молча опускаем температуру.
            detail += ", температура не отдаётся платой"
        lines.append(detail)
        logf(f"  {detail}")
        try:
            if load and int(load) >= 90:
                problems.append(
                    f"процессор загружен на {load}% — если это не рабочая нагрузка, "
                    "проверьте, что зациклилось (антивирус, обновления, зависшая задача)"
                )
        except ValueError:
            pass
        try:
            if temp and float(temp) >= 85:
                problems.append(f"процессор горячий ({temp} °C) — проверьте охлаждение и запылённость")
        except ValueError:
            pass
    else:
        lines.append("Процессор: получить сведения не удалось.")
        logf("  Процессор: получить сведения не удалось.")

    # --- Оперативная память: объём, занято, модули ---
    ram_ps = (
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "$totalKb = $os.TotalVisibleMemorySize; $freeKb = $os.FreePhysicalMemory; "
        "$mods = @(Get-CimInstance Win32_PhysicalMemory); "
        "$slots = (Get-CimInstance Win32_PhysicalMemoryArray | "
        "          Measure-Object -Property MemoryDevices -Sum).Sum; "
        "$speeds = ($mods | ForEach-Object { $_.Speed }) -join ','; "
        # ВАЖНО: строка собирается интерполяцией, а не через оператор "+".
        # В PowerShell поведение "+" задаёт ЛЕВЫЙ операнд: $totalKb —
        # число, поэтому "$totalKb + '|'" пытался преобразовать '|' в
        # число, падал с ошибкой, и весь Write-Output не выполнялся.
        # Из-за этого пункт сообщал "получить сведения не удалось", хотя
        # процессор и диски (там первым идёт строка) собирались нормально.
        "Write-Output \"$totalKb|$freeKb|$($mods.Count)|$slots|$speeds\""
    )
    ram_out = run_ps(ram_ps, timeout=60)
    ram_line = next((l.strip() for l in (ram_out or "").splitlines() if "|" in l), "")
    if ram_line:
        parts = [p.strip() for p in ram_line.split("|")]
        parts += [""] * (5 - len(parts))
        total_kb, free_kb, mod_count, slots, speeds = parts[:5]
        try:
            total_gb = round(int(total_kb) / (1024 ** 2), 1)
            free_ram_gb = round(int(free_kb) / (1024 ** 2), 1)
            used_gb = round(total_gb - free_ram_gb, 1)
            used_pct = (used_gb / total_gb * 100) if total_gb else 0
            detail = f"Оперативная память: {used_gb} из {total_gb} ГБ занято ({used_pct:.0f}%)"
            if mod_count:
                detail += f", модулей {mod_count}"
                if slots:
                    detail += f" из {slots} слотов"
            if speeds:
                detail += f", частота {speeds} МГц"
            lines.append(detail)
            logf(f"  {detail}")
            if used_pct >= 90:
                problems.append(
                    f"оперативная память занята на {used_pct:.0f}% — система уходит в файл "
                    "подкачки и заметно тормозит"
                )
        except (ValueError, ZeroDivisionError):
            lines.append("Оперативная память: получить сведения не удалось.")
            logf("  Оперативная память: получить сведения не удалось.")
    else:
        lines.append("Оперативная память: получить сведения не удалось.")
        logf("  Оперативная память: получить сведения не удалось.")

    # --- Результаты встроенной диагностики памяти Windows ---
    # Windows пишет результат mdsched.exe в журнал событий. Если тест
    # когда-либо находил ошибки — это самое важное, что можно сказать
    # про память, и это стоит показать явно.
    mem_ps = (
        "try { "
        "  $e = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-MemoryDiagnostics-Results'} "
        "       -MaxEvents 1 -ErrorAction Stop; "
        "  Write-Output ($e.TimeCreated.ToString('yyyy-MM-dd') + '|' + $e.Id + '|' + ($e.Message -replace '\\r?\\n', ' ')) "
        "} catch { Write-Output 'NONE' }"
    )
    mem_out = (run_ps(mem_ps, timeout=60) or "").strip()
    mem_line = next((l.strip() for l in mem_out.splitlines() if "|" in l), "")
    if mem_line:
        date_s, _, rest = mem_line.partition("|")
        event_id, _, message = rest.partition("|")
        # Показываем вывод одним словом-вердиктом, а не куском текста
        # события на 160 символов: подробности всё равно ничего не
        # добавляют, а место занимали заметное.
        # Id 1202 — тест нашёл ошибки, 1201 — прошёл чисто.
        bad = (event_id.strip() == "1202" or "обнаруж" in message.lower()
               or "error" in message.lower())
        verdict = "обнаружены ошибки" if bad else "ошибок не найдено"
        lines.append(f"Тест памяти Windows: {verdict} (проверка от {date_s})")
        logf(f"  Тест памяти Windows от {date_s}: {verdict}.")
        if bad:
            problems.append(
                "встроенный тест памяти Windows сообщал об ошибках — "
                "стоит проверить модули памяти (mdsched.exe)"
            )
    else:
        lines.append("Тест памяти Windows: не запускался (mdsched.exe).")
        logf("  Тест памяти Windows ранее не запускался.")

    # --- Диски: здоровье, тип, температура ---
    disk_ps = (
        "foreach ($d in @(Get-PhysicalDisk -ErrorAction SilentlyContinue)) { "
        "  $t = ''; "
        "  try { $t = (Get-StorageReliabilityCounter -PhysicalDisk $d -ErrorAction Stop).Temperature } catch {}; "
        "  Write-Output ($d.FriendlyName + '|' + $d.HealthStatus + '|' + $d.MediaType + '|' + "
        "                [math]::Round($d.Size / 1GB) + '|' + $t) "
        "}"
    )
    disk_out = run_ps(disk_ps, timeout=90)
    any_disk = False
    for line in (disk_out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 4 or not parts[0].strip():
            continue
        any_disk = True
        name, health, media, size = (p.strip() for p in parts[:4])
        temp = parts[4].strip() if len(parts) > 4 else ""
        extra = f", {temp} °C" if temp else ""
        lines.append(f"Диск: {name} — {size} ГБ, {media or 'тип неизвестен'}, состояние: {health}{extra}")
        logf(f"  Диск {name}: {health}, {size} ГБ{extra}")
        if health and health.lower() not in ("healthy", "работоспособен", "исправен"):
            problems.append(f"диск {name} сообщает о состоянии «{health}» — стоит проверить и заранее заменить")
        try:
            if temp and int(float(temp)) >= 60:
                problems.append(f"диск {name} горячий ({temp} °C) — проверьте охлаждение и запылённость")
        except ValueError:
            pass
    if not any_disk:
        lines.append("Диски: получить сведения не удалось.")
        logf("  Диски: получить сведения не удалось.")

    # --- Свободное место по всем разделам ---
    vol_ps = (
        "foreach ($v in @(Get-Volume -ErrorAction SilentlyContinue | "
        "  Where-Object { $_.DriveLetter -and $_.Size -gt 0 })) { "
        # Интерполяция вместо "+": DriveLetter — это char, а не строка,
        # и оператор "+" в PowerShell по левому операнду ушёл бы в
        # числовое сложение (та же ловушка, что сломала сбор о памяти).
        "  Write-Output \"$($v.DriveLetter)|$([math]::Round($v.Size / 1GB, 1))|"
        "$([math]::Round($v.SizeRemaining / 1GB, 1))\" "
        "}"
    )
    vol_out = run_ps(vol_ps, timeout=60)
    for line in (vol_out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3 or not parts[0].strip():
            continue
        letter, total_s, free_s = (p.strip().replace(",", ".") for p in parts)
        try:
            total_v, free_v = float(total_s), float(free_s)
        except ValueError:
            continue
        pct_free = (free_v / total_v * 100) if total_v else 0
        lines.append(f"Раздел {letter}: свободно {free_v} из {total_v} ГБ ({pct_free:.0f}%)")
        logf(f"  Раздел {letter}: свободно {free_v} / {total_v} ГБ ({pct_free:.0f}%)")
        if pct_free < 10:
            problems.append(
                f"на разделе {letter}: осталось {pct_free:.0f}% свободного места — "
                "Windows начнёт тормозить и может не установить обновления"
            )

    # --- Устройства с ошибками в диспетчере устройств ---
    err_ps = (
        "foreach ($d in @(Get-PnpDevice -ErrorAction SilentlyContinue | "
        "  Where-Object { $_.Status -ne 'OK' -and $_.Status -ne 'Unknown' -and $_.Present })) { "
        "  Write-Output ($d.FriendlyName + '|' + $d.Status + '|' + $d.Class) "
        "}"
    )
    err_out = run_ps(err_ps, timeout=90)
    bad_devices = [l.strip() for l in (err_out or "").splitlines() if l.strip() and "|" in l]
    if bad_devices:
        lines.append(f"Устройства с ошибками: {len(bad_devices)}")
        logf(f"  Устройств с ошибками в диспетчере: {len(bad_devices)}")
        for entry in bad_devices[:15]:
            name, _, rest = entry.partition("|")
            status, _, cls = rest.partition("|")
            lines.append(f"  - {name.strip()} ({cls.strip() or 'без класса'}): {status.strip()}")
            logf(f"    - {name.strip()}: {status.strip()}")
        problems.append(
            f"в диспетчере устройств {len(bad_devices)} устройств(а) с ошибкой — "
            "обычно не встал драйвер"
        )
    else:
        lines.append("Устройства с ошибками: нет.")
        logf("  Устройств с ошибками в диспетчере нет.")

    # Список подключённой USB-периферии здесь раньше выводился целиком
    # (15+ строк вида "USB-устройство ввода") — он занимал больше места,
    # чем всё остальное вместе, и ничего не говорил: имена у HID-устройств
    # обезличенные. Убран: что подключено, видно в диспетчере устройств,
    # а для диагностики важны только устройства С ОШИБКАМИ (выше).

    # --- Итог ---
    if problems:
        lines.append("")
        lines.append("Требует внимания:")
        logf(f"  Найдено проблем: {len(problems)}.")
        for p in problems:
            lines.append(f"  ! {p}")
            logf(f"    ! {p}")
    else:
        lines.append("")
        lines.append("Проблем не обнаружено.")
        logf("  Проблем не обнаружено.")

    return "\n".join(lines), problems


def _find_speedtest_cli():
    """
    Ищет speedtest.exe (официальный Ookla Speedtest CLI) — сначала рядом
    с программой (можно положить туда вручную), потом в PATH.
    Возвращает путь или None.
    """
    candidates = [resource_path("speedtest.exe")]
    exe_dir = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    candidates.append(os.path.join(exe_dir, "speedtest.exe"))

    for c in candidates:
        if c and os.path.isfile(c):
            return c

    found = shutil.which("speedtest")
    if found:
        return found
    return None


def step_internet_speed(logf):
    """
    Проверка скорости интернета через официальный Ookla Speedtest CLI
    (speedtest.exe), запускаемый без окна в фоне, с выводом в JSON.
    Если speedtest.exe не найден рядом с программой и не установлен
    в PATH — используется резервный способ (скачивание тестового файла
    по HTTPS через urllib), чтобы шаг не проваливался совсем.
    Возвращает текстовый блок для отчёта.
    """
    logf("Проверка скорости интернет-соединения...")
    result_lines = ["\n[Скорость интернет-соединения]"]

    cli_path = _find_speedtest_cli()
    if cli_path:
        logf(f"  Используется Speedtest CLI: {cli_path}")
        cmd = f'"{cli_path}" --accept-license --accept-gdpr -f json'
        code, out, err = run_cmd(cmd, timeout=60)
        if code == 0 and out:
            try:
                data = json.loads(out.strip().splitlines()[-1])
                down_mbps = data["download"]["bandwidth"] * 8 / 1_000_000
                up_mbps = data["upload"]["bandwidth"] * 8 / 1_000_000
                ping_ms = data.get("ping", {}).get("latency")
                isp = data.get("isp", "?")
                server = data.get("server", {}).get("name", "?")
                result_lines.append(f"Провайдер: {isp}")
                result_lines.append(f"Сервер: {server}")
                result_lines.append(f"Скачивание: {down_mbps:.2f} Мбит/с")
                result_lines.append(f"Отдача: {up_mbps:.2f} Мбит/с")
                if ping_ms is not None:
                    result_lines.append(f"Пинг: {ping_ms:.1f} мс")
                logf(f"  Скачивание: {down_mbps:.2f} Мбит/с, отдача: {up_mbps:.2f} Мбит/с, пинг: {ping_ms}")
                return "\n".join(result_lines)
            except Exception as e:
                logf(f"  Не удалось разобрать вывод Speedtest CLI ({e}), пробуем резервный способ...")
        else:
            logf(f"  Speedtest CLI завершился с ошибкой (код {code}): {err or out}. Пробуем резервный способ...")
    else:
        logf("  Speedtest CLI (speedtest.exe) не найден рядом с программой — используется резервный способ.")

    # Резервный способ: простое скачивание тестового файла по HTTPS.
    # timeout в urlopen ограничивает КАЖДУЮ отдельную операцию сокета
    # (включая каждый resp.read()), а не общее время скачивания — поэтому
    # он должен быть заметно больше, чем время на один чанк при медленном
    # соединении, а не общий предел в 10-12 секунд, как было раньше.
    import urllib.request

    test_urls = [
        ("Cloudflare (10MB)", "https://speed.cloudflare.com/__down?bytes=10000000"),
        ("Hetzner (10MB)", "https://speed.hetzner.de/10MB.bin"),
        ("Cloudflare (2MB)", "https://speed.cloudflare.com/__down?bytes=2000000"),
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) sclean-speedtest"}
    measured = False
    SOCKET_TIMEOUT = 20
    OVERALL_TIME_LIMIT = 20

    for label, url in test_urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            start = time.time()
            downloaded = 0
            max_bytes = 25 * 1024 * 1024
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as resp:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded >= max_bytes or time.time() - start > OVERALL_TIME_LIMIT:
                        break
            elapsed = time.time() - start

            if elapsed > 0.05 and downloaded > 50 * 1024:
                mbps = (downloaded * 8 / elapsed) / 1_000_000
                result_lines.append(
                    f"Скачано {downloaded/1024/1024:.2f} МБ за {elapsed:.2f} сек — ~{mbps:.2f} Мбит/с ({label}, резервный способ)"
                )
                logf(f"  {label}: ~{mbps:.2f} Мбит/с (резервный способ)")
                measured = True
                break
            else:
                logf(f"  {label}: получен слишком маленький ответ ({downloaded} байт), пробуем другой источник...")
        except Exception as e:
            logf(f"  {label}: не удалось выполнить проверку ({e})")

    if not measured:
        result_lines.append("Не удалось измерить скорость соединения (нет ответа от тестовых серверов — возможно, заблокировано антивирусом/файрволом или нет интернета).")
        logf("  Скорость соединения измерить не удалось.")

    return "\n".join(result_lines)


def get_quick_system_summary():
    """
    Быстрая сводка "ОС / CPU / RAM / материнская плата" для отображения
    в шапке программы сразу при запуске — заменяет прежний отдельный
    пункт списка "Сбор информации о системе", который собирал полный
    отчёт (включая диски и видеокарту) и сохранял его в отдельный файл.
    Теперь это не действие пользователя, а всегда видимая справка.

    Один вызов PowerShell вместо нескольких отдельных — быстрее и не
    задерживает старт GUI сильнее необходимого (всё равно выполняется в
    фоновом потоке, см. CleanerApp._load_system_summary_async).
    Возвращает словарь с ключами os/cpu/ram/board, значения — строки
    "?" при неудаче отдельного запроса (не валит всю сводку целиком).
    """
    ps = (
        "$osInfo = Get-CimInstance Win32_OperatingSystem; "
        "$os = $osInfo.Caption; "
        "$build = $osInfo.BuildNumber; "
        "$ubr = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' -Name UBR "
        "-ErrorAction SilentlyContinue).UBR; "
        "$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name; "
        "$ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1); "
        "$boardMaker = (Get-CimInstance Win32_BaseBoard).Manufacturer; "
        "$boardModel = (Get-CimInstance Win32_BaseBoard).Product; "
        "Write-Output ($os + '|' + $build + '.' + $ubr + '|' + $cpu + '|' + $ram + '|' + $boardMaker + ' ' + $boardModel)"
    )
    out = run_ps(ps, timeout=15)
    parts = (out or "").strip().split("|")
    if len(parts) != 5:
        return {"os": "?", "build": "?", "cpu": "?", "ram": "?", "board": "?"}
    os_name, build, cpu, ram, board = (p.strip() for p in parts)
    return {
        "os": os_name or "?",
        "build": build or "?",
        "cpu": cpu or "?",
        "ram": f"{ram} ГБ" if ram else "?",
        "board": board or "?",
    }


# Реестр шагов: id -> (заголовок, функция, возвращает_текст, описание для
# подсказки, рекомендуемое ли (входит в пресет "рекомендуемые настройки"),
# рискованное ли (требует отдельного подтверждения), примерное время в секундах).
STEPS = [
    ("clean_temp",     "Очистка временных файлов, логов и кэшей", step_clean_temp, False,
     "Удаляет временные файлы пользователя и системы, логи CBS/DISM, старый Prefetch,\n"
     "отчёты об ошибках, кэш миниатюр и кэш обновлений Windows. Не трогает файлы моложе\n"
     "суток в Prefetch и не удаляет собственные файлы работающей программы.",
     True, False, 15),
    ("recycle_bin",    "Очистка корзины", step_recycle_bin, False,
     "Полностью очищает корзину без подтверждения.",
     True, False, 5),
    ("cleanmgr",       "Очистка диска (cleanmgr)", step_cleanmgr, False,
     "Запускает встроенную «Очистку диска» Windows по быстрым категориям (временные\n"
     "файлы, кэши, корзина, эскизы и похожие). Тяжёлые категории, связанные с\n"
     "обновлениями Windows, вынесены в отдельный пункт «Глубокая очистка обновлений»,\n"
     "чтобы этот пункт всегда выполнялся предсказуемо быстро и не подвисал.",
     True, False, 90),
    ("sfc_dism",       "Проверка системы: sfc /scannow + DISM RestoreHealth", step_sfc_dism, False,
     "Проверяет и восстанавливает целостность системных файлов Windows.\n"
     "Может занимать 10-30 минут в зависимости от системы.",
     False, False, 900),
    ("defrag",         "Оптимизация диска C: (TRIM для SSD / дефраг для HDD)", step_defrag, False,
     "Автоматически определяет тип диска: для SSD выполняется только TRIM,\n"
     "для HDD — полная дефрагментация.",
     True, False, 60),
    ("power_plan",     "Электропитание: включить схему «Высокая производительность»", step_power_plan, False,
     "Переключает схему электропитания на «Высокая производительность».\n"
     "Текущая схема сохраняется в бэкап и может быть восстановлена.",
     True, False, 3),
    ("visual_fx",      "Визуальные эффекты: быстродействие (сглаживание шрифтов остаётся)", step_visual_effects, False,
     "Отключает визуальные эффекты (анимации, тени, прозрачность) для быстродействия,\n"
     "но оставляет включённым сглаживание экранных шрифтов (ClearType).\n"
     "Текущие настройки сохраняются в бэкап.",
     True, False, 3),
    ("firewall",       "Отключить брандмауэр во всех профилях", step_firewall, False,
     "Полностью отключает брандмауэр Windows во всех профилях (Domain/Private/Public).\n"
     "Текущее состояние сохраняется в бэкап и может быть восстановлено.",
     True, False, 3),
    ("disable_services", "Отключить ненужные службы Windows", step_disable_services, False,
     "Отключает автозапуск безопасного набора неиспользуемых системных служб\n"
     "(факс, индексирование поиска, телеметрия, удалённый реестр и похожие) —\n"
     "не трогает сеть, печать, звук, USB-устройства и ничего, что может быть\n"
     "нужно POS-оборудованию. Тип запуска каждой службы сохраняется в бэкап.",
     True, False, 10),
    ("restore_points", "Очистка старых точек восстановления", step_restore_points, False,
     "Удаляет старые точки восстановления системы, оставляя только самую свежую —\n"
     "освобождает место на тесных SSD моноблоков. Не входит в бэкап sclean —\n"
     "это отдельный, независимый механизм самой Windows.",
     True, False, 15),
    ("cleanmgr_deep", "Глубокая очистка обновлений Windows", step_cleanmgr_deep, False,
     "Удаляет старые обновления, предыдущие установки Windows, файлы ESD и оптимизации\n"
     "доставки, затем сжимает хранилище компонентов (WinSxS) через DISM.\n"
     "Освобождает больше всего места (часто 3-10 ГБ), но работает долго — обработку\n"
     "ведёт «Установщик модулей Windows» (TiWorker.exe). Отмечайте, когда есть время:\n"
     "выполнение прерывается автоматически через 15 минут или при зависании.",
     False, False, 900),
    ("usb_power", "Отключить энергосбережение USB-портов", step_usb_power_management, False,
     "Снимает галочку «Разрешить отключение этого устройства для экономии энергии»\n"
     "в диспетчере устройств для всех USB-концентраторов — порты не будут отключаться\n"
     "сами по себе. Важно для сканеров штрихкодов, чековых принтеров и другой\n"
     "периферии на моноблоках/планшетах, которая иначе может периодически отваливаться.",
     True, False, 10),
    ("internet_speed", "Проверка скорости интернет-соединения", step_internet_speed, True,
     "Измеряет скорость скачивания через Speedtest CLI (если найден) или резервным\n"
     "способом — скачиванием тестового файла.",
     True, False, 30),
]

# Маленький значок слева от текста каждого пункта — чисто визуальная
# подсказка типа действия (очистка/службы/диск/сеть и т.п.), не влияет ни
# на что кроме отображения. Отдельный словарь, а не поле в кортеже STEPS,
# чтобы не трогать arity кортежа и все места, где он распаковывается.
STEP_ICONS = {
    "clean_temp": "🧹",
    "recycle_bin": "🗑",
    "cleanmgr": "💽",
    "sfc_dism": "🛡",
    "defrag": "⚡",
    "power_plan": "🔋",
    "visual_fx": "🎨",
    "firewall": "🧱",
    "disable_services": "⚙",
    "restore_points": "📍",
    "cleanmgr_deep": "📦",
    "usb_power": "🔌",
    "internet_speed": "🌐",
}

# id пунктов, отмечаемых пресетом "рекомендуемые настройки" — берутся из
# поля recommended самих STEPS. Сейчас не отмечаются только два самых
# долгих пункта: "Проверка системы: sfc/DISM" и "Глубокая очистка
# обновлений Windows" (каждый до 15+ минут). Отключение брандмауэра в
# набор ВХОДИТ — на моноблоках с iikoFront это штатная настройка
# (см. историю правок), поэтому старая оговорка про него убрана.
RECOMMENDED_STEP_IDS = {s[0] for s in STEPS if s[5]}
RISKY_STEP_IDS = {s[0] for s in STEPS if s[6]}
STEP_ESTIMATED_SEC = {s[0]: s[7] for s in STEPS}


def format_eta(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"~{seconds} сек"
    minutes = seconds // 60
    rem = seconds % 60
    if rem == 0:
        return f"~{minutes} мин"
    return f"~{minutes} мин {rem} сек"


# ============================================================
# Автообновление через GitHub Releases
# ============================================================
#
# Как это работает и что нужно на твоей стороне:
#
# 1. Публикуешь новую версию как GitHub Release с тегом вида "v1.9.0"
#    (латинская v + номер версии, совпадающий с APP_VERSION в этом
#    файле) и прикрепляешь к релизу собранный sclean.exe как asset.
#    Это можно делать вручную через веб-интерфейс GitHub — Releases ->
#    Draft a new release -> прикрепить файл -> Publish. Никакого своего
#    сервера, FTP или БД не нужно — GitHub хранит и раздаёт файлы сам,
#    бесплатно, по HTTPS.
#
# 2. Программа при старте (и по кнопке "Проверить обновление") дёргает
#    публичный API-эндпоинт GitHub:
#      https://api.github.com/repos/<GITHUB_REPO>/releases/latest
#    Он не требует авторизации для публичных репозиториев и отдаёт JSON
#    с тегом последнего релиза и прямыми ссылками на прикреплённые файлы.
#
# 3. Если тег релиза новее APP_VERSION — предлагаем скачать. Скачивание
#    идёт во временный файл рядом с текущим exe, затем маленький
#    сгенерированный .bat дожидается закрытия текущего процесса,
#    подменяет exe и перезапускает программу. Так сделано потому что
#    Windows не даёт перезаписать/удалить файл работающего процесса
#    напрямую.
#
# Если решишь сменить хостинг на свой сайт/FTP вместо GitHub — нужно
# заменить только _fetch_latest_release() ниже (она должна вернуть
# словарь {"version": "1.9.0", "download_url": "https://..."}), всё
# остальное (сравнение версий, скачивание, подмена файла) переиспользуется
# без изменений.

def _parse_version(v):
    """'1.9.0' -> (1, 9, 0); нечисловые суффиксы отбрасываются."""
    parts = []
    for chunk in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _fetch_latest_release(timeout=15):
    """
    Запрашивает последний релиз с GitHub. Возвращает словарь
    {"version": str, "download_url": str, "notes": str} или None, если
    репозиторий не настроен, недоступен, или у него ещё нет релизов.
    Никогда не бросает исключений наружу — только логирует через return None.
    """
    if not GITHUB_REPO or "/" not in GITHUB_REPO or GITHUB_REPO.startswith("твой-"):
        return None

    import urllib.request
    import urllib.error

    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    headers = {
        "User-Agent": "sclean-updater",
        "Accept": "application/vnd.github+json",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    tag = data.get("tag_name", "")
    if not tag:
        return None

    download_url = None
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".exe"):
            download_url = asset.get("browser_download_url")
            break

    if not download_url:
        return None

    return {
        "version": tag.lstrip("vV"),
        "download_url": download_url,
        "notes": data.get("body", "") or "",
    }


def check_for_update():
    """
    Проверяет, доступна ли версия новее текущей. Возвращает информацию
    о релизе (см. _fetch_latest_release) или None, если обновлений нет
    либо проверка не удалась (нет сети, репозиторий не настроен и т.д.).
    Не должно мешать обычной работе программы — вызывается в фоновом
    потоке, любые сетевые проблемы тихо игнорируются.
    """
    release = _fetch_latest_release()
    if release is None:
        return None
    if _parse_version(release["version"]) <= _parse_version(APP_VERSION):
        return None
    return release


def download_update(download_url, on_progress=None, timeout=30):
    """
    Скачивает exe новой версии во временный файл рядом с текущим
    исполняемым файлом (та же папка — важно, чтобы .bat ниже мог
    переместить его одной операцией, без проблем с правами между
    разными дисками). Возвращает путь к скачанному файлу или None при
    ошибке. on_progress(downloaded_bytes, total_bytes), если передан,
    вызывается по ходу скачивания (total_bytes может быть 0, если
    сервер не прислал Content-Length).
    """
    import urllib.request

    current_exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    target_dir = os.path.dirname(current_exe)
    tmp_path = os.path.join(target_dir, "sclean_update.tmp")

    headers = {"User-Agent": "sclean-updater"}
    try:
        req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            downloaded = 0
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        try:
                            on_progress(downloaded, total)
                        except Exception:
                            pass
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return None

    return tmp_path


def apply_update_and_restart(new_exe_path):
    """
    Подменяет текущий exe на скачанный и перезапускает программу.
    Работающий процесс не может перезаписать или удалить собственный
    exe-файл в Windows, поэтому используется классическая схема:
    генерируется маленький .bat, который:
      1) ждёт несколько секунд, пока текущий процесс успеет завершиться
         (после вызова этой функции программа сама вызывает sys.exit);
      2) заменяет старый exe новым (copy /Y, затем удаляет .tmp);
      3) заново запускает обновлённый exe;
      4) удаляет сам себя.
    Затем .bat запускается через Popen (не дожидаясь), и текущий процесс
    завершается. Требует, чтобы программа уже была под правами
    администратора (она и так их требует при каждом запуске — см.
    is_admin()/relaunch_as_admin()), иначе copy может не пройти, если
    exe лежит в защищённой системной папке.
    """
    if not getattr(sys, "frozen", False):
        # В режиме разработки (запуск как .py) подмена exe не имеет
        # смысла — обновление применяется только к собранному exe.
        return False

    current_exe = sys.executable
    target_dir = os.path.dirname(current_exe)
    bat_path = os.path.join(target_dir, "sclean_apply_update.bat")

    # "start" здесь намеренно НЕ используется для перезапуска exe: start
    # порождает ещё один процесс cmd.exe в цепочке (bat -> cmd -> start ->
    # exe), а сам bat запущен из процесса, который почти сразу завершается
    # (sys.exit после Popen). Из-за этого разрыва цепочки родитель/потомок
    # Windows не может определить "родительский процесс" для UAC-проверки
    # манифеста (uac_admin=True) и показывает "Security validation
    # failure: failed to obtain executable path for parent proces!" —
    # обновление всё равно проходит, но с лишним диалогом. Прямой вызов
    # exe без "start" — на одно звено короче, UAC поднимает процесс от
    # самого bat/cmd напрямую и ошибка не возникает.
    # chcp 65001 переключает кодовую страницу консоли cmd.exe на UTF-8 на
    # время выполнения .bat. Это нужно, потому что пути к exe почти всегда
    # содержат кириллицу (имя пользователя, "Рабочий стол", ручные копии
    # файла вида "sclean (2) — копия.exe" и т.п.), а если записать .bat в
    # кодировке mbcs (= активная ANSI-кодовая страница Windows на момент
    # записи) и она разойдётся с кодовой страницей, в которой .bat реально
    # выполняется, "copy" получает искажённые кириллические имена и вместо
    # перезаписи оригинального exe создаёт новый файл с "мусорным" именем.
    # UTF-8 + chcp 65001 — единственная комбинация, не зависящая от текущей
    # системной локали.
    bat_content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        f':retry\r\n'
        f'copy /Y "{new_exe_path}" "{current_exe}" >nul 2>&1\r\n'
        f'if errorlevel 1 (\r\n'
        f'    timeout /t 1 /nobreak >nul\r\n'
        f'    goto retry\r\n'
        f')\r\n'
        f'del "{new_exe_path}" >nul 2>&1\r\n'
        f'"{current_exe}"\r\n'
        f'del "%~f0" >nul 2>&1\r\n'
    )
    try:
        with open(bat_path, "w", encoding="utf-8-sig") as f:
            f.write(bat_content)
    except Exception:
        try:
            with open(bat_path, "w", encoding="mbcs") as f:
                f.write(bat_content)
        except Exception:
            return False

    try:
        subprocess.Popen(
            f'"{bat_path}"',
            shell=True,
            cwd=target_dir,
            creationflags=(subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0),
        )
    except Exception:
        return False

    return True


# ============================================================
# GUI
# ============================================================

# ============================================================
# Тёмная цветовая схема
# ============================================================

DARK_BG = "#1e1e1f"
DARK_BG_ALT = "#28282a"
DARK_FG = "#e6e6e8"
DARK_FG_DIM = "#9b9b9e"
DARK_ACCENT = "#7a1620"
DARK_ACCENT_TEXT = "#f0d9d9"
DARK_ENTRY_BG = "#242426"
DARK_BORDER = "#4a4a4d"
# Приглушённая нейтральная рамка для внешних границ 4 крупных блоков UI —
# отдельная от DARK_BORDER (который используется для тонких разделителей
# строк внутри блока 2). Нейтральный серый вместо яркого акцентного
# цвета: блоки визуально разделены, но не конкурируют за внимание с
# красной подсветкой выбранных пунктов.
DARK_BLOCK_BORDER = "#4a4a4d"
# Акцент для ETA долгих пунктов (>60 сек) — тёплый жёлто-оранжевый,
# заметный на тёмном фоне, но не конкурирующий с красным DARK_ACCENT
# выбранных строк. Используется только для текста времени, не для фона.
DARK_ETA_WARN = "#d9a441"


class Tooltip:
    """
    Простая всплывающая подсказка: показывается через небольшую задержку
    после наведения на widget, скрывается при уходе курсора или клике.
    """

    def __init__(self, widget, text, delay_ms=450):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip_win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button-1>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip_win or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        label = tk.Label(
            win, text=self.text, justify="left", bg="#2c2424", fg=DARK_FG,
            font=(APP_FONT, 8), relief="solid", borderwidth=1,
            highlightbackground=DARK_BORDER, padx=8, pady=6,
        )
        label.pack()
        self._tip_win = win

    def _hide(self, _event=None):
        self._cancel()
        if self._tip_win:
            try:
                self._tip_win.destroy()
            except Exception:
                pass
            self._tip_win = None


class StepRow(tk.Frame):
    """
    Кастомная строка-чекбокс: квадратный индикатор (закрашенный чёрный
    квадрат, когда отмечено) + текст. Клик по всей строке переключает
    состояние, и вся строка подсвечивается акцентным цветом, когда
    пункт выбран — чтобы было явно видно, что отмечено. Подсказка при
    наведении объясняет, что делает шаг; рискованные шаги (например,
    отключение брандмауэра) помечены значком предупреждения.
    """

    def __init__(self, parent, text, variable, on_run_single, description="",
                 risky=False, eta_sec=None, on_toggle_expand=None, **kwargs):
        super().__init__(parent, bg=DARK_BG, **kwargs)
        self.variable = variable
        self.on_toggle_callback = None
        self.eta_sec = eta_sec

        self.box_canvas = tk.Canvas(
            self, width=20, height=20, bg=DARK_BG, highlightthickness=0, bd=0
        )
        self.box_canvas.pack(side="left", padx=(4, 8), pady=4)

        label_text = text + (" ⚠" if risky else "")
        self.label = tk.Label(
            self, text=label_text, bg=DARK_BG, fg=DARK_FG, font=(APP_FONT, 9), anchor="w"
        )
        self.label.pack(side="left", fill="x", expand=True, pady=4)

        self.run_btn = ttk.Button(self, text="Выполнить", width=14, command=on_run_single)
        self.run_btn.pack(side="right", padx=(8, 6), pady=2)

        if eta_sec:
            # Вертикальный разделитель отделяет блок времени выполнения
            # от кнопки "Выполнить" — раньше они визуально сливались.
            self.eta_sep = tk.Frame(self, bg=DARK_BORDER, width=1)
            self.eta_sep.pack(side="right", fill="y", pady=6)

            self.eta_label = tk.Label(
                self, text=format_eta(eta_sec), bg=DARK_BG, fg=DARK_FG_DIM, font=(APP_FONT, 8)
            )
            self.eta_label.pack(side="right", padx=(10, 10), pady=4)

            # Ещё один разделитель — отделяет название пункта от блока
            # времени выполнения.
            self.name_sep = tk.Frame(self, bg=DARK_BORDER, width=1)
            self.name_sep.pack(side="right", fill="y", pady=6)
        else:
            self.eta_label = None
            self.eta_sep = None
            self.name_sep = None

        # Кнопка разворачивания — только у пунктов, где передан
        # on_toggle_expand (сейчас только "Отключить ненужные службы
        # Windows"): показывает/скрывает панель с индивидуальными
        # чекбоксами под строкой. Пакуется последней с side="right",
        # поэтому оказывается левее блока ETA — явно отдельный элемент
        # управления, а не часть строки времени выполнения. Своя
        # подсветка (self.expanded) показывает, открыта ли панель сейчас,
        # независимо от того, отмечен ли сам пункт.
        self.expand_btn = None
        self.expanded = False
        if on_toggle_expand is not None:
            self.expand_btn = tk.Label(
                self, text="▸ Подробнее", bg=DARK_BG, fg=DARK_FG_DIM,
                font=(APP_FONT, 8, "bold"), cursor="hand2", padx=6, pady=2,
            )
            self.expand_btn.pack(side="right", padx=(6, 10), pady=2)
            self.expand_btn.bind("<Button-1>", lambda e: on_toggle_expand())

            # Подсветка при наведении — без неё кнопка выглядела как
            # обычная подпись, и было неочевидно, что на неё можно
            # нажать (курсор-рука появляется только прямо над текстом).
            # Пока панель раскрыта, цвет задан в _draw() и наведение его
            # не трогает, чтобы не спорить с индикацией "панель открыта".
            self.expand_btn.bind("<Enter>", self._on_expand_hover_in)
            self.expand_btn.bind("<Leave>", self._on_expand_hover_out)
            Tooltip(self.expand_btn, "Показать список служб и включить/отключить каждую по отдельности")

        tooltip_targets = [self, self.box_canvas, self.label]
        if self.eta_label is not None:
            tooltip_targets.append(self.eta_label)
        for widget in tooltip_targets:
            widget.bind("<Button-1>", self._toggle)
            if description:
                Tooltip(widget, description)

        self._draw()

    def _toggle(self, _event=None):
        self.variable.set(not self.variable.get())
        self._draw()

    def _on_expand_hover_in(self, _event=None):
        if self.expand_btn is None or self.expanded:
            return
        self.expand_btn.configure(bg=DARK_BG_ALT, fg=DARK_ETA_WARN)

    def _on_expand_hover_out(self, _event=None):
        if self.expand_btn is None or self.expanded:
            return
        # Возвращаем цвет, соответствующий текущему состоянию строки.
        self._draw()

    def set_expand_arrow(self, expanded):
        if self.expand_btn is not None:
            self.expanded = expanded
            self.expand_btn.configure(text=("▾ Подробнее" if expanded else "▸ Подробнее"))
            self._draw()

    def _draw(self):
        checked = self.variable.get()
        row_bg = DARK_ACCENT if checked else DARK_BG
        text_fg = DARK_ACCENT_TEXT if checked else DARK_FG

        self.configure(bg=row_bg)
        self.box_canvas.configure(bg=row_bg)
        self.label.configure(bg=row_bg, fg=text_fg)
        if self.eta_label is not None:
            # Долгие пункты (>60 сек) выделяются тёплым жёлто-оранжевым
            # цветом времени — сразу видно, какие займут больше времени,
            # не читая каждую цифру отдельно. Только на обычном (не
            # выбранном) фоне — на красном фоне выбранной строки желтизна
            # плохо читается и конфликтует с акцентом выбора, там
            # оставляем обычный светлый текст.
            is_long = (self.eta_sec or 0) >= 60
            if is_long and not checked:
                eta_fg = DARK_ETA_WARN
            else:
                eta_fg = DARK_ACCENT_TEXT if checked else DARK_FG_DIM
            self.eta_label.configure(bg=row_bg, fg=eta_fg)
        if self.expand_btn is not None:
            # Собственная подсветка: когда панель "Подробнее" раскрыта,
            # кнопка выделяется тёплым акцентом (тем же, что и
            # предупреждение по ETA) — видно, что панель открыта, даже
            # если сама строка пункта не отмечена галочкой.
            if self.expanded:
                self.expand_btn.configure(bg=DARK_ETA_WARN, fg="#1a1a1a")
            else:
                self.expand_btn.configure(bg=row_bg, fg=(DARK_ACCENT_TEXT if checked else DARK_FG_DIM))

        self.box_canvas.delete("all")
        # Квадрат в том же стиле, что и мастер-чекбокс: всегда белый с
        # рамкой, внутренний чёрный квадрат появляется только при отметке.
        self.box_canvas.create_rectangle(2, 2, 18, 18, outline=DARK_BORDER, width=2, fill="#ffffff")
        if checked:
            self.box_canvas.create_rectangle(5, 5, 15, 15, outline="", fill="#000000")


def resource_path(filename):
    """Путь к ресурсу (иконка/лого), работает и из .py, и из собранного .exe."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


class CleanerApp:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} — очистка и оптимизация системы")
        root.geometry("760x640")
        root.resizable(True, True)
        root.configure(bg=DARK_BG)

        self._set_window_icon()
        self._setup_dark_theme()

        self.msg_queue = queue.Queue()
        self.worker_thread = None
        self.check_vars = {}
        self.step_rows = {}
        self.log_lines = []
        self.current_steps = []
        self.current_statuses = {}
        self.cancel_requested = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)

        # Тихая проверка обновлений при старте — в фоновом потоке, чтобы
        # не задерживать открытие окна. Если GITHUB_REPO не настроен или
        # сети нет, check_for_update() просто вернёт None без ошибок.
        threading.Thread(target=self._check_update_background, args=(True,), daemon=True).start()

    def _set_window_icon(self):
        try:
            ico_path = resource_path("sclean.ico")
            if os.path.isfile(ico_path):
                self.root.iconbitmap(ico_path)
        except Exception:
            pass

    def _setup_dark_theme(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=DARK_BG, foreground=DARK_FG,
                         fieldbackground=DARK_ENTRY_BG, bordercolor=DARK_BORDER,
                         font=(APP_FONT, 9))
        style.configure("TFrame", background=DARK_BG)
        style.configure("TLabel", background=DARK_BG, foreground=DARK_FG)
        style.configure("Dim.TLabel", background=DARK_BG, foreground=DARK_FG_DIM)
        style.configure("Header.TLabel", background=DARK_BG, foreground=DARK_FG,
                         font=(APP_FONT, 10, "bold"))

        style.configure("TCheckbutton", background=DARK_BG, foreground=DARK_FG)
        style.map("TCheckbutton",
                  background=[("active", DARK_BG)],
                  foreground=[("disabled", DARK_FG_DIM)])

        style.configure("TButton", background=DARK_BG_ALT, foreground=DARK_FG,
                         bordercolor=DARK_BORDER, focusthickness=1, padding=5)
        style.map("TButton",
                  background=[("active", DARK_ACCENT), ("disabled", DARK_BG_ALT)],
                  foreground=[("disabled", DARK_FG_DIM)])

        style.configure("Horizontal.TProgressbar", background=DARK_ACCENT,
                         troughcolor=DARK_ENTRY_BG, bordercolor=DARK_BORDER,
                         lightcolor=DARK_ACCENT, darkcolor=DARK_ACCENT)

        style.configure("TScrollbar", background=DARK_BG_ALT, troughcolor=DARK_BG,
                         bordercolor=DARK_BORDER, arrowcolor=DARK_FG)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # ------------------------------------------------------------------
        # Прокручиваемый контейнер для всего содержимого окна: Canvas +
        # вертикальный Scrollbar. Нужен, чтобы при уменьшении окна ниже
        # естественной высоты содержимого (например, на маленьких экранах
        # моноблоков/планшетов) можно было прокрутить вниз колесом мыши
        # или полосой прокрутки, вместо того чтобы часть интерфейса
        # обрезалась и была недоступна. Все блоки 1-4 ниже крепятся не
        # напрямую к self.root, а к scroll_frame внутри canvas.
        # ------------------------------------------------------------------
        outer_container = tk.Frame(self.root, bg=DARK_BG)
        outer_container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(outer_container, bg=DARK_BG, highlightthickness=0, bd=0)
        v_scrollbar = ttk.Scrollbar(outer_container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=v_scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        v_scrollbar.pack(side="right", fill="y")

        scroll_frame = tk.Frame(self.canvas, bg=DARK_BG)
        self._scroll_frame_window = self.canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        def _update_scrollregion(_event=None):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _sync_scroll_frame_width(event):
            # Внутренний frame должен всегда иметь ширину canvas, иначе
            # содержимое "плавает" при изменении размера окна.
            self.canvas.itemconfigure(self._scroll_frame_window, width=event.width)

        scroll_frame.bind("<Configure>", _update_scrollregion)
        self.canvas.bind("<Configure>", _sync_scroll_frame_width)

        def _on_mousewheel(event):
            # Прокрутка колесом мыши в любом месте окна. delta положителен
            # при прокрутке вверх на Windows — делим на 120 (шаг колеса) и
            # инвертируем знак для естественного направления.
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ------------------------------------------------------------------
        # Блок 1: заголовок/версия приложения, выбор пунктов, "выбрать все",
        # "рекомендуемые настройки". Обёрнут рамкой для визуального отделения
        # от остальных блоков интерфейса.
        # ------------------------------------------------------------------
        block1_outer = tk.Frame(scroll_frame, bg=DARK_BLOCK_BORDER, bd=0)
        block1_outer.pack(fill="x", padx=10, pady=(10, 8))
        block1 = tk.Frame(block1_outer, bg=DARK_BG)
        block1.pack(fill="x", padx=2, pady=2)

        header_frame = ttk.Frame(block1)
        header_frame.pack(fill="x", padx=8, pady=(8, 0))

        self._logo_img = None
        try:
            # Используем заранее подготовленный маленький PNG (64×64,
            # пересчитан из исходника через качественный Lanczos-ресемплинг
            # на этапе сборки), а не tk.PhotoImage.subsample() в рантайме —
            # subsample делает грубое прореживание пикселей (ближайший
            # сосед) и даёт заметно размытый/рваный результат на маленьком
            # размере. 64px даёт запас под HiDPI-масштабирование, реальный
            # размер в шапке ~32px задаётся через zoom/subsample 1:2 ниже.
            logo_path = resource_path("sclean_logo_small.png")
            if not os.path.isfile(logo_path):
                logo_path = resource_path("sclean_logo.png")  # запасной вариант
            if os.path.isfile(logo_path):
                raw = tk.PhotoImage(file=logo_path)
                if raw.width() > 40:
                    # Подготовленный ассет 64px -> уменьшаем ровно в 2 раза
                    # (целочисленный subsample на малом шаге почти не теряет
                    # в качестве, в отличие от прореживания с 512px).
                    self._logo_img = raw.subsample(2, 2)
                else:
                    self._logo_img = raw
                ttk.Label(header_frame, image=self._logo_img, background=DARK_BG).pack(side="left", padx=(0, 8))
        except Exception:
            self._logo_img = None

        # Название программы и номер версии кликабельны: название ведёт на
        # страницу самого последнего релиза на GitHub (удобно проверить,
        # есть ли обновление, не открывая программу отдельно), а версия —
        # на страницу именно этой, установленной версии (например, чтобы
        # свериться со списком изменений конкретно этого билда). При
        # наведении добавляется подчёркивание и более яркий цвет — иначе
        # по виду не отличить от обычного текста, и то, что это ссылка,
        # не очевидно.
        def _make_link_label(parent, text, url, normal_fg, hover_fg, font_spec, side_pad=None):
            underline_font = font_spec + ("underline",)
            lbl = tk.Label(
                parent, text=text, font=font_spec, fg=normal_fg, bg=DARK_BG,
                cursor="hand2", bd=0,
            )
            if side_pad is not None:
                lbl.pack(side="left", **side_pad)
            else:
                lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
            lbl.bind("<Enter>", lambda e: lbl.configure(fg=hover_fg, font=underline_font))
            lbl.bind("<Leave>", lambda e: lbl.configure(fg=normal_fg, font=font_spec))
            return lbl

        app_name_label = _make_link_label(
            header_frame, APP_NAME,
            f"https://github.com/{GITHUB_REPO}/releases/latest",
            normal_fg=DARK_FG, hover_fg=DARK_ACCENT_TEXT, font_spec=(APP_FONT, 14, "bold"),
        )
        Tooltip(app_name_label, "Открыть страницу последнего релиза на GitHub")

        version_label = _make_link_label(
            header_frame, f"  v{APP_VERSION}",
            f"https://github.com/{GITHUB_REPO}/releases/tag/v{APP_VERSION}",
            normal_fg=DARK_FG_DIM, hover_fg=DARK_ACCENT_TEXT, font_spec=(APP_FONT, 9),
        )
        Tooltip(version_label, f"Открыть страницу релиза v{APP_VERSION} на GitHub")

        self.update_btn = ttk.Button(header_frame, text="Проверить обновление", command=self.check_update_clicked)
        self.update_btn.pack(side="right")
        self.update_status_label = ttk.Label(header_frame, text="", style="Dim.TLabel")
        self.update_status_label.pack(side="right", padx=(0, 8))
        self._pending_release = None

        # Мини-индикатор заполнения диска C — сразу видно, насколько
        # тесно на диске, не открывая проводник отдельно (актуально для
        # моноблоков с маленькими SSD, где место — частый повод для
        # запуска программы). Обновляется при старте и после каждого
        # завершения набора пунктов (см. _refresh_disk_usage_label).
        # Обёртка с рамкой — нужна, чтобы дать понятную визуальную
        # обратную связь на клик (обычная подсветка hover/click на
        # tk.Canvas+ttk.Label иначе незаметна, в отличие от кнопок и
        # текстовых ссылок в шапке, где меняется цвет самого текста).
        self.disk_usage_row = tk.Frame(block1, bg=DARK_BG, highlightthickness=1, highlightbackground=DARK_BORDER)
        self.disk_usage_row.pack(fill="x", padx=8, pady=(2, 0))
        disk_usage_inner = tk.Frame(self.disk_usage_row, bg=DARK_BG)
        disk_usage_inner.pack(fill="x", padx=6, pady=3)
        self.disk_usage_canvas = tk.Canvas(
            disk_usage_inner, width=120, height=10, bg=DARK_ENTRY_BG, highlightthickness=0, bd=0,
        )
        self.disk_usage_canvas.pack(side="left", pady=2)
        self.disk_usage_label = ttk.Label(disk_usage_inner, text="", style="Dim.TLabel", font=(APP_FONT, 8))
        self.disk_usage_label.pack(side="left", padx=(6, 0))
        self._refresh_disk_usage_label()

        # Клик по индикатору открывает диск C: в проводнике. Hover
        # подсвечивает рамку и фон акцентным цветом, а сам клик даёт
        # короткую вспышку (_flash_disk_usage_row) — вместе понятно, что
        # элемент кликабельный и клик сработал, даже без смены курсора.
        def _disk_hover_on(_e=None):
            self.disk_usage_row.configure(highlightbackground=DARK_ACCENT_TEXT, bg=DARK_BG_ALT)
            disk_usage_inner.configure(bg=DARK_BG_ALT)
            self.disk_usage_label.configure(background=DARK_BG_ALT)

        def _disk_hover_off(_e=None):
            self.disk_usage_row.configure(highlightbackground=DARK_BORDER, bg=DARK_BG)
            disk_usage_inner.configure(bg=DARK_BG)
            self.disk_usage_label.configure(background=DARK_BG)

        def _disk_click(_e=None):
            os.startfile("C:\\")
            self._flash_disk_usage_row()

        for widget in (self.disk_usage_row, disk_usage_inner, self.disk_usage_canvas, self.disk_usage_label):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", _disk_click)
            widget.bind("<Enter>", _disk_hover_on)
            widget.bind("<Leave>", _disk_hover_off)
            Tooltip(widget, "Открыть диск C: в проводнике")

        # Кнопка автозагрузки: раньше "Оптимизация автозапуска" была
        # отдельным пунктом списка, автоматически отключавшим известное
        # ПО по жёстко зашитому списку (OneDrive/Skype/...) — заменена на
        # прямое открытие обеих папок автозагрузки в проводнике, чтобы
        # пользователь сам решал, что удалять/оставлять, без риска задеть
        # что-то нужное на конкретном моноблоке.
        startup_row = ttk.Frame(block1)
        startup_row.pack(fill="x", padx=8, pady=(4, 0))
        startup_btn = ttk.Button(
            startup_row, text="🚀 Открыть папки автозагрузки", command=self._open_startup_folders,
        )
        startup_btn.pack(side="left")
        Tooltip(
            startup_btn,
            "Открывает 2 папки автозагрузки в проводнике — для текущего пользователя\n"
            "и для всех пользователей. Удалить ненужные ярлыки можно вручную.",
        )

        # Краткая сводка о системе (ОС/CPU/RAM/плата) — раньше это был
        # отдельный пункт списка "Сбор информации о системе", теперь
        # показывается сразу в шапке при запуске. Опрос идёт в фоновом
        # потоке (Get-CimInstance занимает 1-3 сек), чтобы не морозить GUI.
        # Текст сводки лежит в tk.Text, а не в ttk.Label, именно чтобы
        # его можно было выделить мышью и скопировать (Ctrl+C) — из
        # Label текст скопировать невозможно в принципе, а сведения о
        # системе как раз обычно нужно куда-то переслать. Виджет
        # настроен так, чтобы визуально не отличаться от обычной
        # подписи: без рамки, с фоном блока, невысокий, без курсора
        # ввода. Правка запрещена не через state="disabled" (это
        # блокирует и выделение), а перехватом нажатий клавиш.
        system_summary_row = ttk.Frame(block1)
        system_summary_row.pack(fill="x", padx=8, pady=(4, 6))
        self.system_summary_text = tk.Text(
            system_summary_row, height=2, wrap="word", font=(APP_FONT, 8),
            bg=DARK_BG, fg=DARK_FG_DIM, relief="flat", bd=0, highlightthickness=0,
            insertwidth=0, cursor="xterm", padx=0, pady=0,
            selectbackground=DARK_ACCENT, selectforeground=DARK_ACCENT_TEXT,
        )
        self.system_summary_text.insert("1.0", "Сведения о системе: загрузка…")
        self.system_summary_text.pack(side="left", fill="x", expand=True)

        def _summary_keypress(event):
            # Пропускаем только копирование и выделение всего текста,
            # остальные нажатия игнорируем — получается поле, доступное
            # только для чтения, но с рабочим выделением.
            if event.state & 0x4 and event.keysym.lower() in ("c", "a", "insert"):
                return None
            if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"):
                return None
            return "break"

        self.system_summary_text.bind("<Key>", _summary_keypress)
        self.system_summary_text.bind("<Control-a>", lambda e: (
            self.system_summary_text.tag_add("sel", "1.0", "end-1c"), "break")[1])
        # Подсказки при наведении здесь намеренно нет: всплывающее окно
        # перекрывало сам текст сводки ровно в тот момент, когда его
        # пытаются выделить мышью. Про возможность копирования говорит
        # кнопка "Копировать" рядом.

        self.copy_summary_btn = ttk.Button(
            system_summary_row, text="Копировать", width=12, command=self._copy_system_summary,
        )
        self.copy_summary_btn.pack(side="right", padx=(6, 0))
        Tooltip(self.copy_summary_btn, "Скопировать сведения о системе в буфер обмена")

        threading.Thread(target=self._load_system_summary_async, daemon=True).start()

        # Диагностика железа и периферии. Раньше была пунктом списка
        # задач, который нужно отметить и запустить — но она ничего не
        # меняет, только смотрит, поэтому логичнее ей быть рядом со
        # сведениями о системе и выполняться самой при старте.
        # В свёрнутом виде — одна строка с итогом (или числом проблем),
        # по кнопке разворачивается полный текст.
        diag_row = tk.Frame(block1, bg=DARK_BG)
        diag_row.pack(fill="x", padx=8, pady=(0, 2))
        self.diag_status_label = tk.Label(
            diag_row, text="🩺 Диагностика железа: проверка…", bg=DARK_BG, fg=DARK_FG_DIM,
            font=(APP_FONT, 8), anchor="w", justify="left",
        )
        self.diag_status_label.pack(side="left")

        self.diag_toggle_btn = tk.Label(
            diag_row, text="▸ Подробнее", bg=DARK_BG, fg=DARK_FG_DIM,
            font=(APP_FONT, 8, "bold"), cursor="hand2", padx=6, pady=1,
        )
        self.diag_toggle_btn.bind("<Button-1>", lambda e: self._toggle_diag_details())
        self.diag_toggle_btn.bind("<Enter>", lambda e: self.diag_toggle_btn.configure(
            bg=DARK_BG_ALT, fg=DARK_ETA_WARN) if not self._diag_expanded else None)
        self.diag_toggle_btn.bind("<Leave>", lambda e: self._draw_diag_toggle())
        # Кнопка появляется только когда диагностика закончится — до тех
        # пор разворачивать нечего.

        # Копирование всей диагностики одной кнопкой — тот же приём, что
        # и у сведений о системе выше: выделять мышью многострочный блок
        # неудобно, а переслать результат нужно как раз целиком.
        # Показывается вместе с кнопкой "Подробнее", по готовности.
        self.copy_diag_btn = ttk.Button(
            diag_row, text="Копировать", width=12, command=self._copy_diagnostics,
        )
        Tooltip(self.copy_diag_btn, "Скопировать результат диагностики железа в буфер обмена")

        self.diag_details_frame = tk.Frame(block1, bg=DARK_BG)
        # height задаётся динамически по фактическому числу строк (см.
        # _apply_hardware_diagnostics): после того как из блока убрали
        # список USB-периферии, он помещается целиком, и прокрутка
        # больше не нужна — а именно на неё жаловались.
        self.diag_details_text = tk.Text(
            self.diag_details_frame, height=8, wrap="word", font=(APP_FONT, 8),
            bg=DARK_ENTRY_BG, fg=DARK_FG_DIM, relief="flat", bd=0, highlightthickness=1,
            highlightbackground=DARK_BORDER, insertwidth=0, cursor="xterm",
            selectbackground=DARK_ACCENT, selectforeground=DARK_ACCENT_TEXT,
        )
        self.diag_details_text.pack(fill="both", expand=True, padx=2, pady=2)
        self.diag_details_text.bind("<Key>", _summary_keypress)

        self._diag_expanded = False
        self._diag_text = ""
        self._diag_problems = []
        threading.Thread(target=self._load_hardware_diagnostics_async, daemon=True).start()

        # Сворачиваемое краткое описание программы: что делает и где
        # сохраняется лог. Свёрнуто по умолчанию, чтобы не занимать место
        # у постоянных пользователей — разворачивается по клику на "ⓘ О программе".
        self.about_toggle_row = ttk.Frame(block1)
        self.about_toggle_row.pack(fill="x", padx=8, pady=(2, 0))
        self._about_expanded = False
        self.about_toggle_btn = ttk.Label(
            self.about_toggle_row, text="▸ ⓘ О программе", style="Dim.TLabel", cursor="hand2",
        )
        self.about_toggle_btn.pack(anchor="w")
        self.about_toggle_btn.bind("<Button-1>", lambda e: self._toggle_about())

        self.about_frame = ttk.Frame(block1)
        # Не .pack() здесь — показывается только при разворачивании.

        log_dir_hint = os.path.join("Рабочий стол", "sclean")
        about_text = (
            f"{APP_NAME} — отмечаете нужные пункты списка ниже (или выполняете каждый\n"
            "по отдельности) и запускаете: программа чистит систему и/или меняет\n"
            "настройки Windows одним нажатием. Перед изменением настроек текущее\n"
            "состояние сохраняется в бэкап — можно вернуть обратно кнопкой «Бэкап».\n"
            f"Отчёт о каждом запуске сохраняется в папку \"{log_dir_hint}\"."
        )
        ttk.Label(
            self.about_frame, text=about_text, style="Dim.TLabel", justify="left",
        ).pack(anchor="w", padx=(16, 0), pady=(2, 6))

        top_frame = ttk.Frame(block1)
        top_frame.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(top_frame, text="Выберите пункты для выполнения:", style="Header.TLabel").pack(anchor="w")

        # Строка управления: master-чекбокс (белый квадрат) отмечает/снимает
        # все пункты сразу повторным кликом — отдельная кнопка "Снять всё"
        # не нужна, это дублировало бы ту же функцию.
        master_frame = ttk.Frame(block1)
        master_frame.pack(fill="x", padx=8, pady=(4, 8))

        self.master_var = tk.BooleanVar(value=False)
        self.master_box = tk.Canvas(
            master_frame, width=20, height=20, bg=DARK_BG, highlightthickness=0, bd=0, cursor="hand2"
        )
        self.master_box.pack(side="left", padx=(2, 8))
        self.master_box.bind("<Button-1>", self._toggle_master)

        master_label = ttk.Label(master_frame, text="Выбрать все пункты", style="Dim.TLabel", cursor="hand2")
        master_label.pack(side="left")
        master_label.bind("<Button-1>", self._toggle_master)

        preset_btn = ttk.Button(master_frame, text="Рекомендуемые настройки", command=self.select_recommended)
        preset_btn.pack(side="right")
        Tooltip(
            preset_btn,
            "Отметить обычный набор для обслуживания: очистка, оптимизация диска,\n"
            "электропитание, службы, USB и диагностика.\n\n"
            "Не отмечаются два самых долгих пункта — «Проверка системы: sfc/DISM»\n"
            "и «Глубокая очистка обновлений Windows»: каждый может занять\n"
            "15 минут и более, их отмечают вручную, когда есть время.",
        )

        self._draw_master_box()

        # ------------------------------------------------------------------
        # Блок 2: сами пункты выполнения (чекбокс + название + ~ETA +
        # кнопка "Выполнить" для каждой строки). Общая рамка вокруг всего
        # списка — строки внутри разделены линией того же цвета, но
        # визуально принадлежат одному блоку. Рамка и разделители утолщены
        # (3px) и осветлены для более чёткого визуального отделения блока
        # и строк друг от друга.
        # ------------------------------------------------------------------
        steps_outer = tk.Frame(scroll_frame, bg=DARK_BLOCK_BORDER, bd=0)
        steps_outer.pack(fill="x", padx=10, pady=(0, 8))

        steps_frame = tk.Frame(steps_outer, bg=DARK_BG)
        steps_frame.pack(fill="x", padx=2, pady=2)

        # Выбранные для отключения службы из безопасного списка (id ->
        # включена ли галочка в развёрнутой панели). По умолчанию все
        # отмечены — соответствует прежнему поведению "весь набор".
        self.services_selected = list(SAFE_DISABLE_SERVICES.keys())
        self.services_panel = None
        self.services_panel_expanded = False

        for idx, (step_id, title, _func, _ret, desc, _rec, risky, est_sec) in enumerate(STEPS):
            var = tk.BooleanVar(value=False)
            var.trace_add("write", lambda *args: self._sync_master_box())
            self.check_vars[step_id] = var
            icon = STEP_ICONS.get(step_id, "")
            row_text = f"{icon}  {title}" if icon else title

            on_toggle_expand = None
            if step_id == "disable_services":
                on_toggle_expand = lambda sid=step_id: self._toggle_services_panel(steps_frame)

            row = StepRow(
                steps_frame, row_text, var,
                on_run_single=lambda sid=step_id: self.run_single(sid),
                description=desc, risky=risky, eta_sec=est_sec,
                on_toggle_expand=on_toggle_expand,
            )
            row.pack(fill="x")
            self.step_rows[step_id] = row

            if step_id == "disable_services":
                self.services_row = row

            if idx < len(STEPS) - 1:
                # Тонкий разделитель (1px) — той же толщины, что и
                # вертикальные разделители у ETA внутри строки.
                sep = tk.Frame(steps_frame, bg=DARK_BORDER, height=1)
                sep.pack(fill="x")

        # ------------------------------------------------------------------
        # Блок 3: основные действия — запуск всего отмеченного, отмена,
        # сворачивание в трей, открытие отчёта, бэкап.
        # ------------------------------------------------------------------
        block3_outer = tk.Frame(scroll_frame, bg=DARK_BLOCK_BORDER, bd=0)
        block3_outer.pack(fill="x", padx=10, pady=(0, 8))
        block3 = tk.Frame(block3_outer, bg=DARK_BG)
        block3.pack(fill="x", padx=2, pady=2)

        btns_frame = ttk.Frame(block3)
        btns_frame.pack(fill="x", padx=8, pady=6)

        self.run_all_btn = ttk.Button(btns_frame, text="Выполнить всё отмеченное", command=self.run_selected)
        self.run_all_btn.pack(side="left")

        self.cancel_btn = ttk.Button(btns_frame, text="Отмена", command=self.cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.cancel_btn, "Останавливает выполнение после завершения текущего шага\n(мягкая отмена — текущий шаг не прерывается на середине).")

        tray_btn = ttk.Button(btns_frame, text="Свернуть", command=lambda: self._minimize_to_tray(auto=False))
        tray_btn.pack(side="left", padx=(6, 0))

        self.restore_btn = ttk.Button(btns_frame, text="Бэкап", command=self.run_restore)
        self.restore_btn.pack(side="right")

        self.open_report_btn = ttk.Button(btns_frame, text="Открыть отчёт", command=self.open_report, state="disabled")
        self.open_report_btn.pack(side="right", padx=(0, 6))

        # ------------------------------------------------------------------
        # Блок 4: процесс выполнения — статус, индикатор прогресса и журнал
        # выполняемых пунктов. Визуально отделён от блока с кнопками.
        # ------------------------------------------------------------------
        block4_outer = tk.Frame(scroll_frame, bg=DARK_BLOCK_BORDER, bd=0)
        block4_outer.pack(fill="x", padx=10, pady=(0, 8))
        block4 = tk.Frame(block4_outer, bg=DARK_BG)
        block4.pack(fill="x", padx=2, pady=2)

        progress_frame = ttk.Frame(block4)
        progress_frame.pack(fill="x", padx=8, pady=(8, 6))

        # Текст статуса и таймер — в одной строке; кнопка "Завершить
        # очистку диска" — в СВОЕЙ отдельной строке ниже (а не справа в
        # той же строке). Раньше при длинном тексте статуса (например,
        # "Очистка диска работает в фоне (~42 сек) — закройте её окно,
        # когда закончит, или нажмите «Завершить очистку диска»") и узком
        # окне кнопка вытеснялась за пределы видимой области вместо
        # переноса — казалось, что она пропала, хотя на деле просто не
        # помещалась. wraplength на статусе + отдельная строка под кнопку
        # гарантируют, что кнопка всегда видна независимо от ширины окна.
        status_row = ttk.Frame(progress_frame)
        status_row.pack(fill="x")

        self.status_label = ttk.Label(
            status_row, text="Готово к запуску. 0%", font=(APP_FONT, 9), wraplength=520,
        )
        self.status_label.pack(side="left", anchor="w")

        # Таймер общего времени выполнения — обновляется раз в секунду,
        # пока идёт выполнение, показывает мин:сек. Отдельно от текста
        # статуса, чтобы не мигать вместе с частой сменой сообщений.
        self.timer_label = ttk.Label(status_row, text="", style="Dim.TLabel", font=(APP_FONT, 9))
        self.timer_label.pack(side="left", padx=(10, 0))
        self.run_start_time = None
        self.timer_after_id = None

        kill_cleanmgr_row = ttk.Frame(progress_frame)
        kill_cleanmgr_row.pack(fill="x")

        # Кнопка принудительного завершения "Очистки диска" — видна, только
        # пока cleanmgr реально работает в фоне (см. _run_cleanmgr_async /
        # _on_cleanmgr_pid). Позволяет не ждать закрытия её окна вручную.
        # В своей строке (а не справа от статуса) — не может быть обрезана
        # или вытеснена за пределы окна длинным текстом статуса.
        self.kill_cleanmgr_btn = ttk.Button(
            kill_cleanmgr_row, text="Завершить очистку диска", command=self._kill_cleanmgr,
        )
        self.cleanmgr_pid = None
        # Мутабельный словарь (не bool) — чтобы замыкание внутри _worker
        # видело изменения, сделанные позже из GUI-потока по кнопке.
        self.cleanmgr_stop_flag = {"stop": False}

        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=4)

        # Крупная сводка освобождённого места — появляется только после
        # завершения набора пунктов, если реально что-то удалилось.
        # Не .pack() здесь — показывается точечно через
        # _show_freed_summary(), чтобы не занимать место, пока программа
        # не запускалась ни разу за эту сессию.
        self.freed_summary_label = tk.Label(
            progress_frame, text="", bg=DARK_BG, fg=DARK_ACCENT_TEXT,
            font=(APP_FONT, 16, "bold"),
        )

        # Журнал (упрощённый): список выбранных пунктов с их статусом
        # выполнения (ожидание / выполняется / готово) — без построчных
        # деталей команд, всё это есть только в сохранённом файле отчёта.
        log_frame = ttk.Frame(block4)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        ttk.Label(log_frame, text="Выполняемые пункты:").pack(anchor="w")

        text_container = ttk.Frame(log_frame)
        text_container.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            text_container, wrap="word", height=14, state="disabled",
            bg=DARK_ENTRY_BG, fg=DARK_FG, insertbackground=DARK_FG,
            selectbackground=DARK_ACCENT, relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=DARK_BORDER,
            highlightcolor=DARK_ACCENT, font=(APP_FONT, 9),
        )
        scrollbar = ttk.Scrollbar(text_container, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log_text.tag_configure("pending", foreground=DARK_FG_DIM)
        self.log_text.tag_configure("running", foreground="#ffffff", font=(APP_FONT, 9, "bold"))
        self.log_text.tag_configure("done", foreground="#7fbf7f")
        self.log_text.tag_configure("error", foreground="#ff8080")
        self.log_text.tag_configure("cancelled", foreground=DARK_FG_DIM, font=(APP_FONT, 9, "italic"))
        self.log_text.tag_configure("summary", foreground=DARK_ACCENT_TEXT, font=(APP_FONT, 9, "bold"))

        # Футер
        footer_frame = ttk.Frame(scroll_frame)
        footer_frame.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(footer_frame, text=f"{APP_NAME} build {APP_VERSION}  ·  автор: {APP_AUTHOR}",
                  style="Dim.TLabel").pack(side="right")

        self.report_path = None

    def select_all(self):
        for step_id, var in self.check_vars.items():
            var.set(True)
            self.step_rows[step_id]._draw()
        self._draw_master_box()

    def deselect_all(self):
        for step_id, var in self.check_vars.items():
            var.set(False)
            self.step_rows[step_id]._draw()
        self._draw_master_box()

    def _refresh_disk_usage_label(self):
        """
        Перерисовывает мини-индикатор заполнения диска C (полоска +
        текст "X.X / Y.Y ГБ (Z%)"). Вызывается при старте программы и
        после каждого завершения набора пунктов, чтобы отражать
        актуальное место сразу после очистки, без перезапуска.
        """
        info = get_disk_usage_info("C:\\")
        self.disk_usage_canvas.delete("all")
        if info is None:
            self.disk_usage_label.configure(text="Диск C: н/д")
            return
        used_gb, total_gb, percent = info

        w, h = 120, 10
        self.disk_usage_canvas.create_rectangle(0, 0, w, h, outline=DARK_BORDER, width=1, fill=DARK_ENTRY_BG)
        fill_w = max(1, int(w * percent / 100))
        # Заполнение >85% подсвечивается тем же тёплым акцентом, что и
        # долгие пункты ETA — единая визуальная логика "внимание нужно
        # сюда", не выдумывая ещё один цвет.
        bar_color = DARK_ETA_WARN if percent >= 85 else DARK_ACCENT
        self.disk_usage_canvas.create_rectangle(0, 0, fill_w, h, outline="", fill=bar_color)

        self.disk_usage_label.configure(text=f"Диск C: {used_gb} / {total_gb} ГБ ({percent}%)")

    def _flash_disk_usage_row(self):
        """
        Короткая вспышка рамки диска C (акцент -> обычная рамка) сразу
        после клика — визуальное подтверждение "клик сработал", помимо
        уже открывшегося окна проводника, которое может появиться не
        мгновенно или свернуться за окно программы.
        """
        self.disk_usage_row.configure(highlightbackground=DARK_ACCENT, highlightthickness=2)
        self.root.after(200, lambda: self.disk_usage_row.configure(
            highlightbackground=DARK_BORDER, highlightthickness=1,
        ))

    def _open_startup_folders(self):
        """
        Открывает обе стандартные папки автозагрузки Windows в проводнике
        (текущий пользователь + все пользователи) — пользователь сам
        решает, что оставить или удалить, без риска автоматически задеть
        что-то нужное на конкретном моноблоке/планшете.
        """
        user_folder = os.path.join(
            os.getenv("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
        )
        all_users_folder = os.path.join(
            os.getenv("ProgramData", r"C:\ProgramData"),
            "Microsoft", "Windows", "Start Menu", "Programs", "StartUp",
        )
        opened_any = False
        for folder in (user_folder, all_users_folder):
            if folder and os.path.isdir(folder):
                try:
                    os.startfile(folder)
                    opened_any = True
                except Exception:
                    continue
        if not opened_any:
            messagebox.showwarning(
                "Автозагрузка",
                "Не удалось найти папки автозагрузки на этом компьютере.",
            )

    def _load_system_summary_async(self):
        """
        Выполняется в фоновом потоке при старте программы: собирает
        краткую сводку ОС/CPU/RAM/материнской платы через PowerShell
        (get_quick_system_summary) и передаёт результат в GUI-поток через
        self.root.after, не блокируя окно на время опроса (1-3 сек).
        Заменяет прежний отдельный пункт списка "Сбор информации о
        системе" — теперь видно сразу, без запуска очистки.
        """
        summary = get_quick_system_summary()
        text = (
            f"🖥 {summary['os']} (сборка {summary['build']})   ·   ⚙ {summary['cpu']}   ·   "
            f"🧠 {summary['ram']}   ·   🔧 {summary['board']}"
        )
        self.root.after(0, lambda: self._set_system_summary_text(text))

    def _set_system_summary_text(self, text):
        """
        Заменяет текст в поле сводки. Поле только для чтения за счёт
        перехвата клавиш, а не state="disabled", поэтому вставлять текст
        можно напрямую, без временного разблокирования.
        """
        self.system_summary_text.delete("1.0", "end")
        self.system_summary_text.insert("1.0", text)

    def _load_hardware_diagnostics_async(self):
        """
        Фоновый сбор диагностики железа при старте программы. Занимает
        несколько секунд (несколько запросов к WMI), поэтому выполняется
        вне GUI-потока, а результат отдаётся в интерфейс через after().
        """
        try:
            text, problems = collect_hardware_diagnostics()
        except Exception as e:
            text, problems = f"Не удалось выполнить диагностику: {e}", []
        self.root.after(0, lambda: self._apply_hardware_diagnostics(text, problems))

    def _apply_hardware_diagnostics(self, text, problems):
        """
        Показывает итог диагностики в шапке: одна строка со сводкой и
        кнопка разворачивания полного текста. Строка подсвечивается
        тёплым акцентом, если что-то требует внимания — тем же цветом,
        что и долгие пункты и переполненный диск.
        """
        self._diag_text = text
        self._diag_problems = problems

        if problems:
            word = "проблема" if len(problems) == 1 else (
                "проблемы" if 2 <= len(problems) <= 4 else "проблем")
            self.diag_status_label.configure(
                text=f"🩺 Диагностика железа: {len(problems)} {word} — требует внимания",
                fg=DARK_ETA_WARN,
            )
        else:
            self.diag_status_label.configure(
                text="🩺 Диагностика железа: проблем не обнаружено", fg=DARK_FG_DIM,
            )

        self.diag_details_text.delete("1.0", "end")
        self.diag_details_text.insert("1.0", text)
        # Подгоняем высоту под содержимое, чтобы блок показывался
        # целиком и не приходилось его листать. Потолок 20 строк — на
        # случай, если устройств с ошибками окажется необычно много.
        line_count = text.count("\n") + 1
        self.diag_details_text.configure(height=min(max(line_count, 4), 20))
        self.diag_toggle_btn.pack(side="left", padx=(8, 0))
        self.copy_diag_btn.pack(side="right", padx=(6, 0))
        self._draw_diag_toggle()

    def _copy_diagnostics(self):
        """
        Кладёт весь результат диагностики железа в буфер обмена.
        Работает независимо от того, развёрнута панель или нет: текст
        хранится в self._diag_text, а не берётся из виджета.
        """
        text = (self._diag_text or "").strip()
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            return
        self.copy_diag_btn.configure(text="Скопировано")
        self.root.after(1200, lambda: self.copy_diag_btn.configure(text="Копировать"))

    def _draw_diag_toggle(self):
        if self._diag_expanded:
            self.diag_toggle_btn.configure(text="▾ Подробнее", bg=DARK_ETA_WARN, fg="#1a1a1a")
        else:
            self.diag_toggle_btn.configure(text="▸ Подробнее", bg=DARK_BG, fg=DARK_FG_DIM)

    def _toggle_diag_details(self):
        self._diag_expanded = not self._diag_expanded
        if self._diag_expanded:
            self.diag_details_frame.pack(fill="x", padx=8, pady=(0, 6))
        else:
            self.diag_details_frame.pack_forget()
        self._draw_diag_toggle()

    def _copy_system_summary(self):
        """
        Кладёт сводку о системе в буфер обмена целиком — быстрее, чем
        выделять мышью, когда нужно просто переслать характеристики.
        """
        text = self.system_summary_text.get("1.0", "end-1c").strip()
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            return
        # Короткая обратная связь на самой кнопке — окно с сообщением
        # ради копирования строки было бы избыточным.
        self.copy_summary_btn.configure(text="Скопировано")
        self.root.after(1200, lambda: self.copy_summary_btn.configure(text="Копировать"))

    def _toggle_services_panel(self, steps_frame):
        """
        Показывает/скрывает панель с индивидуальными чекбоксами для
        каждой из 13 безопасных служб (SAFE_DISABLE_SERVICES), встроенную
        сразу под строкой "Отключить ненужные службы Windows". Каждый
        чекбокс отображает и меняет РЕАЛЬНОЕ текущее состояние службы в
        системе сразу по клику (через set_service_disabled), а не только
        то, что будет применено при следующем запуске пункта — так что
        панель работает и как индивидуальный переключатель, независимо
        от того, отмечен ли сам пункт в общем списке.
        """
        self.services_panel_expanded = not self.services_panel_expanded
        self.services_row.set_expand_arrow(self.services_panel_expanded)

        if not self.services_panel_expanded:
            if self.services_panel is not None:
                self.services_panel.pack_forget()
            return

        if self.services_panel is None:
            panel = tk.Frame(steps_frame, bg=DARK_BG_ALT)
            self.services_panel = panel

            tk.Label(
                panel, text="Каждый переключатель применяется сразу, независимо от запуска пункта:",
                bg=DARK_BG_ALT, fg=DARK_FG_DIM, font=(APP_FONT, 8), anchor="w",
            ).pack(fill="x", padx=(30, 8), pady=(6, 4))

            self.service_row_vars = {}
            for name, ru_label in SAFE_DISABLE_SERVICES.items():
                self._build_service_toggle_row(panel, name, ru_label)

        # pack(after=...) размещает панель СРАЗУ под строкой служб, даже
        # если между ними уже есть другие виджеты (следующий разделитель) —
        # без этого панель уехала бы в конец steps_frame.
        self.services_panel.pack(fill="x", after=self.services_row)

    def _build_service_toggle_row(self, parent, name, ru_label):
        """
        Одна строка внутри развёрнутой панели служб: маленький квадратный
        чекбокс (тот же визуальный стиль, что и в остальной программе) +
        русское название + техническое имя службы бледным шрифтом.
        Состояние читается из системы при построении строки (через
        get_service_startup_type) — если служба уже отключена вручную или
        отсутствует в системе, чекбокс сразу покажет актуальное состояние.
        """
        current = get_service_startup_type(name)
        is_disabled_now = current == "Disabled"
        available = current is not None

        # self.services_selected — список служб, которые будут отключены
        # при запуске всего пункта "Отключить ненужные службы Windows"
        # разом (через "Выполнить"/"Выполнить всё отмеченное"). Изначально
        # включает весь безопасный набор; чекбокс здесь как включает
        # службу немедленно в системе, так и убирает её из этого списка
        # (или наоборот) — оба действия синхронизированы.
        if not available and name in self.services_selected:
            self.services_selected.remove(name)

        row = tk.Frame(parent, bg=DARK_BG_ALT)
        row.pack(fill="x", padx=(30, 8), pady=1)

        var = tk.BooleanVar(value=(not is_disabled_now) and available)

        box = tk.Canvas(row, width=16, height=16, bg=DARK_BG_ALT, highlightthickness=0, bd=0)
        box.pack(side="left", padx=(0, 8), pady=2)

        text = f"{ru_label}  ({name})" if available else f"{ru_label}  ({name}) — не найдена в системе"
        fg = DARK_FG if available else DARK_FG_DIM
        label = tk.Label(row, text=text, bg=DARK_BG_ALT, fg=fg, font=(APP_FONT, 8), anchor="w")
        label.pack(side="left", fill="x", expand=True)

        status_label = tk.Label(
            row, text=("отключена" if is_disabled_now else "включена") if available else "",
            bg=DARK_BG_ALT, fg=DARK_FG_DIM, font=(APP_FONT, 8),
        )
        status_label.pack(side="right", padx=(6, 4))

        def draw():
            box.delete("all")
            outline = DARK_BORDER if available else DARK_FG_DIM
            box.create_rectangle(1, 1, 15, 15, outline=outline, width=2, fill="#ffffff")
            if var.get():
                box.create_rectangle(4, 4, 12, 12, outline="", fill="#000000")

        def toggle(_event=None):
            if not available:
                return
            new_enabled = not var.get()
            ok = set_service_disabled(name, not new_enabled)
            if ok:
                var.set(new_enabled)
                status_label.configure(text="включена" if new_enabled else "отключена")
                if new_enabled:
                    if name in self.services_selected:
                        self.services_selected.remove(name)
                else:
                    if name not in self.services_selected:
                        self.services_selected.append(name)
            draw()

        if available:
            box.bind("<Button-1>", toggle)
            label.bind("<Button-1>", toggle)

        draw()

    def _toggle_about(self):
        """
        Разворачивает/сворачивает краткое описание программы под шапкой.
        Свёрнуто по умолчанию — постоянным пользователям не нужно видеть
        его каждый раз, но новичок может развернуть кликом.
        """
        self._about_expanded = not self._about_expanded
        if self._about_expanded:
            self.about_toggle_btn.configure(text="▾ ⓘ О программе")
            self.about_frame.pack(fill="x", after=self.about_toggle_row)
        else:
            self.about_toggle_btn.configure(text="▸ ⓘ О программе")
            self.about_frame.pack_forget()

    def _toggle_master(self, _event=None):
        # Клик по мастер-чекбоксу: если что-то выбрано — снимает всё,
        # если ничего не выбрано — отмечает всё.
        any_checked = any(v.get() for v in self.check_vars.values())
        if any_checked:
            self.deselect_all()
        else:
            self.select_all()

    def _sync_master_box(self):
        # Вызывается при изменении любого отдельного чекбокса, чтобы
        # мастер-чекбокс отражал состояние "всё выбрано" / "не всё".
        self._draw_master_box()

    def _draw_master_box(self):
        if not hasattr(self, "master_box"):
            return
        all_checked = bool(self.check_vars) and all(v.get() for v in self.check_vars.values())

        self.master_box.delete("all")
        self.master_box.create_rectangle(2, 2, 18, 18, outline=DARK_BORDER, width=2, fill="#ffffff")
        if all_checked:
            self.master_box.create_rectangle(5, 5, 15, 15, outline="", fill="#000000")

    def _render_steps_panel(self, steps_to_run, statuses, summary=None):
        """
        Перерисовывает список пунктов и их состояний:
        pending / running / done / error. Никаких построчных деталей
        команд — те уходят только в файл отчёта.

        summary, если передан, добавляется отдельной итоговой строкой в
        конце — используется при полном завершении выполнения, чтобы
        сразу было видно общий результат, не листая список пунктов.
        """
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")

        icons = {"pending": "○", "running": "▶", "done": "✓", "error": "✗", "cancelled": "–"}
        for step_id, title, *_rest in steps_to_run:
            state = statuses.get(step_id, "pending")
            icon = icons.get(state, "○")
            self.log_text.insert("end", f"{icon}  {title}\n", state)

        if summary:
            self.log_text.insert("end", "\n" + summary + "\n", "summary")

        self.log_text.configure(state="disabled")

    def _check_update_background(self, silent):
        """
        Выполняется в фоновом потоке (см. вызовы в __init__ и
        check_update_clicked). silent=True — тихая проверка при старте,
        не показывает сообщение, если обновлений нет; silent=False —
        запущено вручную кнопкой, показывает результат в любом случае.
        """
        if not silent:
            self.root.after(0, lambda: self.update_status_label.configure(text="Проверка..."))
        release = check_for_update()
        self.root.after(0, lambda: self._on_update_check_done(release, silent))

    def _on_update_check_done(self, release, silent):
        if release is None:
            self._pending_release = None
            if not silent:
                self.update_status_label.configure(text="")
                messagebox.showinfo(
                    "Обновление",
                    f"Установлена актуальная версия ({APP_VERSION}) либо репозиторий\n"
                    "обновлений не настроен / недоступен.",
                )
            return

        self._pending_release = release
        self.update_status_label.configure(text=f"Доступна версия {release['version']}")

        notes = release.get("notes", "").strip()
        notes_txt = f"\n\nЧто нового:\n{notes[:500]}" if notes else ""
        if messagebox.askyesno(
            "Доступно обновление",
            f"Доступна новая версия {release['version']} (текущая: {APP_VERSION}).{notes_txt}\n\n"
            "Скачать и установить сейчас? Программа будет перезапущена.",
        ):
            self._download_and_apply_update(release)

    def check_update_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Занято", "Дождитесь завершения текущей операции.")
            return
        self.update_btn.configure(state="disabled")
        threading.Thread(target=self._check_update_background, args=(False,), daemon=True).start()
        self.root.after(3000, lambda: self.update_btn.configure(state="normal"))

    def _download_and_apply_update(self, release):
        self.update_status_label.configure(text="Скачивание обновления...")
        self.update_btn.configure(state="disabled")

        def progress(downloaded, total):
            if total:
                pct = int(downloaded / total * 100)
                text = f"Скачивание обновления... {pct}%"
            else:
                text = f"Скачивание обновления... {downloaded // 1024} КБ"
            self.root.after(0, lambda: self.update_status_label.configure(text=text))

        def worker():
            path = download_update(release["download_url"], on_progress=progress)
            self.root.after(0, lambda: self._on_update_downloaded(path))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_downloaded(self, path):
        if not path:
            self.update_status_label.configure(text="")
            self.update_btn.configure(state="normal")
            messagebox.showerror(
                "Обновление",
                "Не удалось скачать обновление. Проверьте подключение к интернету и попробуйте ещё раз.",
            )
            return

        if apply_update_and_restart(path):
            # Программа будет заменена и перезапущена сгенерированным
            # .bat-скриптом — закрываем текущий процесс, чтобы .bat мог
            # подменить exe-файл.
            self.root.destroy()
        else:
            self.update_status_label.configure(text="")
            self.update_btn.configure(state="normal")
            messagebox.showerror(
                "Обновление",
                "Не удалось применить обновление автоматически.\n"
                f"Скачанный файл: {path}\nЗамените exe вручную.",
            )

    def _kill_cleanmgr(self):
        """
        Принудительно завершает очистку диска — для случаев, когда
        пользователь не хочет ждать её фактического завершения. Убивает
        по ИМЕНИ процесса (cleanmgr.exe), а не по сохранённому PID: как
        объяснено в step_cleanmgr, реальная работа иногда продолжается
        в процессе, отличном от того, что мы изначально запустили, так
        что PID сам по себе недостаточен, чтобы гарантированно всё
        остановить. self.cleanmgr_stop_flag сигнализирует опросному
        циклу в step_cleanmgr прекратить ожидание немедленно.
        """
        if not messagebox.askyesno(
            "Завершить очистку диска",
            "Принудительно завершить процесс очистки диска?\n"
            "Очистка будет прервана до её фактического завершения.",
        ):
            return
        self.cleanmgr_stop_flag["stop"] = True
        _kill_processes_by_name("cleanmgr.exe")
        self.kill_cleanmgr_btn.pack_forget()
        self.cleanmgr_pid = None

    def _set_buttons_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.run_all_btn.configure(state=state)
        for child in self.root.winfo_children():
            pass  # кнопки "выполнить только это" не блокируем жёстко, но предотвращаем параллельный запуск через флаг

    def select_recommended(self):
        for step_id, var in self.check_vars.items():
            var.set(step_id in RECOMMENDED_STEP_IDS)
            self.step_rows[step_id]._draw()
        self._draw_master_box()

    def run_selected(self):
        selected = [s for s in STEPS if self.check_vars[s[0]].get()]
        if not selected:
            messagebox.showinfo("Нечего выполнять", "Отметьте хотя бы один пункт.")
            return
        self._start_run(selected)

    def run_single(self, step_id):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Занято", "Дождитесь завершения текущей операции.")
            return
        step = next(s for s in STEPS if s[0] == step_id)
        self._start_run([step])

    def run_restore(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Занято", "Дождитесь завершения текущей операции.")
            return

        backup = load_backup()
        if not backup:
            messagebox.showinfo(
                "Бэкап не найден",
                "Файл бэкапа настроек не найден (папка sclean на рабочем столе,\n"
                "sclean_backup.json).\nБэкап создаётся автоматически при выполнении шагов\n"
                "электропитания, брандмауэра или визуальных эффектов.",
            )
            return

        categories = self._ask_restore_categories(backup)
        if categories is None:
            return

        def restore_step(logf):
            restore_from_backup(logf, categories=categories)
            return None

        self._start_run([("restore", "Восстановление настроек из бэкапа", restore_step, False)])

    def _build_restore_checkbox_row(self, parent, text, var, available):
        """
        Строит строку с квадратным чекбоксом в едином стиле с основным
        списком пунктов очистки (белый квадрат с рамкой, чёрный внутренний
        квадрат при выборе) — вместо стандартного ttk.Checkbutton с
        крестиком, который используется системной темой Windows.
        """
        row = tk.Frame(parent, bg=DARK_BG)
        row.pack(fill="x", padx=14, pady=2)

        box_canvas = tk.Canvas(row, width=20, height=20, bg=DARK_BG, highlightthickness=0, bd=0)
        box_canvas.pack(side="left", padx=(0, 8))

        fg = DARK_FG if available else DARK_FG_DIM
        label = tk.Label(row, text=text, bg=DARK_BG, fg=fg, font=(APP_FONT, 9), anchor="w")
        label.pack(side="left", fill="x", expand=True)

        def draw():
            box_canvas.delete("all")
            outline = DARK_BORDER if available else DARK_FG_DIM
            box_canvas.create_rectangle(2, 2, 18, 18, outline=outline, width=2, fill="#ffffff")
            if var.get():
                box_canvas.create_rectangle(5, 5, 15, 15, outline="", fill="#000000")

        def toggle(_event=None):
            if not available:
                return
            var.set(not var.get())
            draw()

        if available:
            box_canvas.bind("<Button-1>", toggle)
            label.bind("<Button-1>", toggle)

        draw()

    def _ask_restore_categories(self, backup):
        """
        Диалог точечного восстановления: пользователь может выбрать, какие
        именно категории настроек восстанавливать, а не только всё сразу.
        Возвращает set категорий или None, если отменено.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("Восстановить настройки")
        dlg.configure(bg=DARK_BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        tk.Label(
            dlg, text=f"Бэкап сохранён: {backup.get('saved_at', '?')}\nВыберите, что восстановить:",
            bg=DARK_BG, fg=DARK_FG, font=(APP_FONT, 9), justify="left",
        ).pack(anchor="w", padx=14, pady=(14, 8))

        labels = {
            "power_plan": "Электропитание",
            "firewall": "Брандмауэр",
            "visual_effects": "Визуальные эффекты",
            "services": "Службы Windows",
            "startup_apps": "Автозапуск приложений",
            "usb_power": "Энергосбережение USB-портов",
        }
        availability = {
            "power_plan": bool(backup.get("power_plan_guid") or backup.get("power_timeouts")),
            "firewall": bool(backup.get("firewall_state")),
            "visual_effects": any(backup.get(k) not in (None, "") for k in
                                   ("visual_fx_setting", "min_animate", "drag_full_windows")),
            "services": bool(backup.get("services_state")),
            "startup_apps": bool(backup.get("startup_apps_disabled")),
            "usb_power": bool(backup.get("usb_power_states")),
        }
        vars_map = {}
        for cat in BACKUP_CATEGORIES:
            available = availability.get(cat, False)
            var = tk.BooleanVar(value=available)
            vars_map[cat] = var
            text = labels[cat] + ("" if available else " (нет данных в бэкапе)")
            self._build_restore_checkbox_row(dlg, text, var, available)

        result = {"categories": None}

        def on_ok():
            chosen = {cat for cat, var in vars_map.items() if var.get()}
            if not chosen:
                messagebox.showinfo("Нечего восстанавливать", "Выберите хотя бы одну категорию.", parent=dlg)
                return
            result["categories"] = chosen
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=14, pady=(10, 14))
        ttk.Button(btns, text="Отмена", command=on_cancel).pack(side="right")
        ttk.Button(btns, text="Восстановить", command=on_ok).pack(side="right", padx=(0, 6))

        dlg.wait_window()
        return result["categories"]

    def _start_run(self, steps_to_run):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Занято", "Дождитесь завершения текущей операции.")
            return

        self.cancel_requested = False
        self.progress.configure(maximum=100, value=0)
        self.open_report_btn.configure(state="disabled")
        self.restore_btn.configure(state="disabled")
        self.cancel_btn.configure(state=("normal" if len(steps_to_run) > 1 else "disabled"))
        self.log_lines = []
        self.freed_summary_label.pack_forget()

        self.current_steps = steps_to_run
        self.current_statuses = {s[0]: "pending" for s in steps_to_run}
        self._render_steps_panel(steps_to_run, self.current_statuses)

        self.run_start_time = time.time()
        self._tick_timer()

        self.worker_thread = threading.Thread(
            target=self._worker, args=(steps_to_run,), daemon=True
        )
        self.worker_thread.start()

    def _tick_timer(self):
        """
        Обновляет self.timer_label раз в секунду, пока self.run_start_time
        установлен — даёт наглядное подтверждение, что программа активна
        и не зависла, даже если статус-текст надолго не меняется (как во
        время ожидания закрытия окна "Очистки диска").
        """
        if self.run_start_time is None:
            return
        elapsed = int(time.time() - self.run_start_time)
        minutes, seconds = divmod(elapsed, 60)
        self.timer_label.configure(text=f"⏱ {minutes:02d}:{seconds:02d}")
        self.timer_after_id = self.root.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self.timer_after_id is not None:
            try:
                self.root.after_cancel(self.timer_after_id)
            except Exception:
                pass
            self.timer_after_id = None
        self.run_start_time = None

    def cancel_run(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        self.cancel_requested = True
        self.cancel_btn.configure(state="disabled")

        # "Очистка диска" (cleanmgr) выполняется в отдельном фоновом
        # потоке параллельно с остальными пунктами (см. _worker) и не
        # проверяет cancel_requested — только cleanmgr_stop_flag. Без
        # этого нажатие "Отмена" останавливало бы только ещё не начатые
        # обычные пункты, а cleanmgr продолжал бы молча работать в фоне,
        # как будто отмена его не касается.
        if self.cleanmgr_pid is not None:
            self.cleanmgr_stop_flag["stop"] = True
            _kill_processes_by_name("cleanmgr.exe")
            self.kill_cleanmgr_btn.pack_forget()
            self.cleanmgr_pid = None
            self.status_label.configure(text="Отмена запрошена — очистка диска остановлена, завершаем оставшееся...")
        else:
            self.status_label.configure(text="Отмена запрошена — завершится после текущего шага...")

    def _worker(self, steps_to_run):
        # Каждый пункт получает свой собственный список строк — logf()
        # внутри step_*-функций раньше просто отбрасывался, из-за чего
        # отчёт показывал только общий статус "выполнено/ошибка", без
        # деталей. Теперь строки накапливаются per-step через
        # _make_logf(), а в конце попадают в step_results третьим
        # элементом кортежа — то, что просил пользователь: что конкретно
        # очистилось/включилось/отключилось и почему.
        step_details = {}
        # Объём, реально освобождённый каждым пунктом (в байтах) — шаги
        # сообщают его через report_freed_bytes(). Итог считается как
        # сумма этих значений, а не как разница свободного места до/после
        # (см. подробное объяснение в report_freed_bytes).
        step_freed = {}

        def _make_logf(step_id):
            lines = []
            step_details[step_id] = lines

            def logf(msg):
                lines.append(str(msg))

            return logf

        start_time = time.time()
        free_before = get_free_space_gb("C:\\")

        collected_texts = []
        failures = []  # (title, причина) для шагов, завершившихся с ошибкой
        failures_lock = threading.Lock()
        step_results = []  # (title, status, details) для всех выбранных пунктов, в отчёт
        step_results_lock = threading.Lock()
        cancelled = False

        # "Очистка диска" (cleanmgr) показывает собственное окно, которое
        # пользователь закрывает вручную по завершении — время ожидания
        # непредсказуемо и не должно блокировать остальные пункты. Поэтому
        # cleanmgr, если он выбран, запускается в отдельном фоновом потоке
        # сразу и выполняется параллельно с остальными шагами; основной
        # цикл ниже обрабатывает все прочие пункты последовательно и не
        # ждёт его. Перед финальным отчётом мы дожидаемся завершения этого
        # потока, чтобы "Освобождено места" учитывало и его результат.
        # Оба пункта на базе cleanmgr ("Очистка диска" и "Глубокая очистка
        # обновлений Windows") делят одно пространство имён процессов:
        # определение завершения и принудительная остановка работают по
        # имени cleanmgr.exe. Запускать их одновременно нельзя — они
        # видели бы процессы друг друга и завершали бы их. Поэтому оба
        # уходят в ОДИН фоновый поток и выполняются в нём последовательно,
        # параллельно остальным пунктам.
        cleanmgr_entries = []
        other_steps = []
        for step in steps_to_run:
            if step[0] in ("cleanmgr", "cleanmgr_deep"):
                cleanmgr_entries.append(step)
            else:
                other_steps.append(step)

        cleanmgr_thread = None
        if cleanmgr_entries:
            first_title = cleanmgr_entries[0][1]
            self.msg_queue.put(("status", f"Выполняется в фоне: {first_title}"))

            self.cleanmgr_stop_flag["stop"] = False

            def _on_cleanmgr_pid(pid):
                # Сообщаем GUI-потоку PID запущенного cleanmgr — используется
                # только для отображения кнопки "Завершить очистку диска";
                # само завершение бьёт по имени процесса, а не по этому PID.
                self.msg_queue.put(("cleanmgr_pid", pid))

            def _cleanmgr_should_stop():
                return self.cleanmgr_stop_flag["stop"]

            def _run_cleanmgr_async():
                for entry in cleanmgr_entries:
                    c_id, c_title, c_func, c_returns_text = entry[:4]
                    if self.cleanmgr_stop_flag["stop"] or self.cancel_requested:
                        self.msg_queue.put(("step_status", (c_id, "cancelled")))
                        with step_results_lock:
                            step_results.append((c_title, "cancelled", step_details.get(c_id, [])))
                        continue

                    self.msg_queue.put(("step_status", (c_id, "running")))
                    c_logf = _make_logf(c_id)
                    try:
                        result = c_func(c_logf, on_pid=_on_cleanmgr_pid, should_stop=_cleanmgr_should_stop)
                        step_freed[c_title] = getattr(c_logf, "freed_bytes", 0)
                        if c_returns_text and result:
                            collected_texts.append(result)
                        state = "cancelled" if self.cleanmgr_stop_flag["stop"] else "done"
                        self.msg_queue.put(("step_status", (c_id, state)))
                        with step_results_lock:
                            step_results.append((c_title, state, step_details.get(c_id, [])))
                    except Exception as e:
                        with failures_lock:
                            failures.append((c_title, str(e)))
                        with step_results_lock:
                            step_results.append((c_title, "error", step_details.get(c_id, [])))
                        self.msg_queue.put(("step_status", (c_id, "error")))

            cleanmgr_thread = threading.Thread(target=_run_cleanmgr_async, daemon=True)
            cleanmgr_thread.start()

        total_other = len(other_steps)
        for idx, (step_id, title, func, returns_text, *_rest) in enumerate(other_steps, start=1):
            if self.cancel_requested:
                cancelled = True
                for rem_id, rem_title, *_r2 in other_steps[idx - 1:]:
                    self.msg_queue.put(("step_status", (rem_id, "cancelled")))
                    step_results.append((rem_title, "cancelled", []))
                break

            self.msg_queue.put(("step_status", (step_id, "running")))
            pct_before = int(round((idx - 1) / total_other * 100)) if total_other else 0
            eta = STEP_ESTIMATED_SEC.get(step_id)
            eta_txt = f"  (ожидается {format_eta(eta)})" if eta else ""
            self.msg_queue.put(("status", f"Выполняется: {title}  —  {pct_before}%{eta_txt}"))
            step_logf = _make_logf(step_id)
            try:
                if step_id == "disable_services":
                    # Пункт можно развернуть в GUI и снять отметки с
                    # отдельных служб — тогда выполняется только
                    # выбранное подмножество, а не весь безопасный
                    # список. self.services_selected обновляется
                    # чекбоксами внутри развёрнутой панели.
                    result = func(step_logf, selected_names=self.services_selected)
                else:
                    result = func(step_logf)
                step_freed[title] = getattr(step_logf, "freed_bytes", 0)
                if returns_text and result:
                    collected_texts.append(result)
                self.msg_queue.put(("step_status", (step_id, "done")))
                step_results.append((title, "done", step_details.get(step_id, [])))
            except Exception as e:
                with failures_lock:
                    failures.append((title, str(e)))
                step_results.append((title, "error", step_details.get(step_id, [])))
                self.msg_queue.put(("step_status", (step_id, "error")))
            pct_after = int(round(idx / total_other * 100)) if total_other else 100
            self.msg_queue.put(("progress", pct_after))

        if cleanmgr_thread is not None:
            # Периодически обновляем статус с прошедшим временем ожидания,
            # вместо одного статичного сообщения на весь период join() —
            # так по интерфейсу видно, что программа не зависла, а реально
            # ждёт, пока пользователь закроет окно очистки диска.
            wait_start = time.time()
            limit_txt = format_eta(int(CLEANMGR_MAX_SECONDS))
            while cleanmgr_thread.is_alive():
                waited = int(time.time() - wait_start)
                self.msg_queue.put((
                    "status",
                    f"Очистка диска работает в фоне ({format_eta(waited)}, предел {limit_txt} на пункт) — "
                    f"прервётся сама при зависании, или нажмите «Завершить очистку диска»",
                ))
                cleanmgr_thread.join(timeout=2)

        # Отдельного блока с текстами шагов в конце отчёта больше нет:
        # результат проверки скорости и так печатался построчно внутри
        # своего пункта, а в конце дублировался почти дословно. Тексты
        # по-прежнему собираются (шаги их возвращают), но в отчёт не
        # выводятся — вся информация уже есть в детализации пунктов.
        free_after = get_free_space_gb("C:\\")
        elapsed = time.time() - start_time

        # Итог считаем суммой того, что шаги измерили сами. Разница
        # свободного места остаётся в отчёте, но только справочно: она
        # включает постороннюю запись на диск и потому бывает
        # отрицательной даже при успешной очистке.
        freed_gb = round(sum(step_freed.values()) / (1024 ** 3), 2)
        delta_gb = None
        if free_before is not None and free_after is not None:
            delta_gb = round(free_after - free_before, 2)

        report_path = self._write_report(
            free_before, free_after, elapsed, failures, cancelled,
            step_results, step_freed, freed_gb, delta_gb,
        )
        rotate_old_reports()

        self.msg_queue.put(("done", (report_path, cancelled, freed_gb)))

    def _write_report(self, free_before, free_after, elapsed_sec, failures, cancelled,
                      step_results=None, step_freed=None, freed_total_gb=None, delta_gb=None):
        # speed_text больше не принимается: результат проверки скорости
        # печатался и в детализации своего пункта, и отдельным блоком в
        # конце отчёта — второе было дословным повтором первого.
        date_str = datetime.date.today().isoformat()
        time_str = datetime.datetime.now().strftime("%H%M%S")
        report_path = os.path.join(get_app_data_dir(), f"sclean_{date_str}_{time_str}.txt")

        step_freed = step_freed or {}
        if delta_gb is None and free_before is not None and free_after is not None:
            delta_gb = round(free_after - free_before, 2)

        status_labels = {
            "done": "выполнено",
            "error": "ошибка",
            "cancelled": "отменено",
        }

        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("ОТЧЁТ ОБ ОЧИСТКЕ И ОПТИМИЗАЦИИ СИСТЕМЫ\n")
                f.write(f"{APP_NAME} build {APP_VERSION}  ·  автор: {APP_AUTHOR}\n")
                f.write("=" * 60 + "\n\n")

                # ИТОГ идёт первым: раньше отчёт начинался с длинного
                # списка пунктов, и главные цифры (сколько освободили,
                # сколько заняло) приходилось искать в самом низу.
                results = list(step_results or [])
                done_n = sum(1 for e in results if e[1] == "done")
                err_n = sum(1 for e in results if e[1] == "error")
                cancel_n = sum(1 for e in results if e[1] == "cancelled")

                summary_bits = [f"выполнено {done_n} из {len(results)}"]
                if err_n:
                    summary_bits.append(f"с ошибкой {err_n}")
                if cancel_n:
                    summary_bits.append(f"отменено {cancel_n}")
                f.write("ИТОГ: " + ", ".join(summary_bits) + "\n")
                if cancelled:
                    f.write("Выполнение прервано пользователем.\n")

                if freed_total_gb is not None:
                    f.write(f"Освобождено: {freed_total_gb} ГБ")
                    if free_after is not None:
                        f.write(f"; свободно на C: {free_after} ГБ")
                    f.write(f"; заняло {elapsed_sec:.0f} сек\n")

                # Причины невыполнения — сразу под итогом, это первое,
                # что нужно увидеть, если что-то пошло не так.
                if failures:
                    f.write("\nНе удалось выполнить:\n")
                    for title, reason in failures:
                        f.write(f"  - {title}: {reason}\n")

                # Подробности по пунктам. Раньше здесь печатались все
                # пункты подряд вместе с построчной детализацией, и
                # объём очистки повторялся трижды (в детализации пункта,
                # в разбивке по пунктам и в общей сумме), а скорость
                # соединения — дважды. Теперь: строка на пункт, а
                # детализация — только там, где ей есть что сказать.
                if results:
                    f.write("\nПо пунктам:\n")
                    for entry in results:
                        if len(entry) == 3:
                            title, status, details = entry
                        else:
                            title, status = entry
                            details = []
                        label = status_labels.get(status, status)
                        freed_b = step_freed.get(title, 0)
                        suffix = f" — освобождено {format_bytes_gb(freed_b)} ГБ" if freed_b else ""
                        f.write(f"  [{label}] {title}{suffix}\n")
                        for line in details:
                            text = str(line).strip()
                            # Заголовочные строки вида "Очистка ...:" в
                            # начале каждого шага дублируют название
                            # пункта, а строки о бэкапе — служебные.
                            if not text or text.endswith("...") or text.startswith("Бэкап:"):
                                continue
                            f.write(f"      {text}\n")

                f.write(f"\nСвободно на C: {free_before} ГБ до, {free_after} ГБ после")
                if delta_gb is not None:
                    f.write(f" (изменение {delta_gb} ГБ с учётом записи других программ)")
                f.write("\n")
        except Exception:
            pass

        return report_path

    def open_report(self):
        if not (self.report_path and os.path.isfile(self.report_path)):
            return
        # Открываем сам файл отчёта (в текстовом редакторе по умолчанию)
        # и одновременно папку с ним в проводнике, с файлом выделенным —
        # чтобы сразу было видно остальные отчёты/бэкап рядом, без
        # отдельного похода в папку sclean на рабочем столе.
        os.startfile(self.report_path)
        try:
            subprocess.Popen(["explorer.exe", f"/select,{self.report_path}"])
        except Exception:
            pass

    def _minimize_to_tray(self, auto=False):
        """
        Сворачивает окно в панель задач. Раньше здесь использовался
        pystray для настоящей иконки в системном трее, но эта
        зависимость (pystray + Pillow) добавляла ~15-18 МБ к весу exe —
        для программы, которую нужно часто передавать на удалённые
        моноблоки/планшеты по сети, это неоправданно. Обычное
        сворачивание в панель задач (root.iconify()) даёт тот же
        практический результат — окно не мешает работе — без лишнего веса.
        """
        self.root.iconify()

    def _restore_from_tray(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if messagebox.askyesno(
                "Операция выполняется",
                "Сейчас выполняется операция. Свернуть в трей вместо закрытия?",
            ):
                self._minimize_to_tray(auto=False)
                return
        self._restore_from_tray()
        self.root.destroy()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "step_status":
                    step_id, state = payload
                    self.current_statuses[step_id] = state
                    self._render_steps_panel(self.current_steps, self.current_statuses)
                    if step_id == "cleanmgr" and state in ("done", "error", "cancelled"):
                        # Очистка диска завершилась (сама или была убита) —
                        # прячем кнопку принудительного завершения.
                        self.kill_cleanmgr_btn.pack_forget()
                        self.cleanmgr_pid = None
                elif kind == "cleanmgr_pid":
                    self.cleanmgr_pid = payload
                    self.kill_cleanmgr_btn.pack(side="left", pady=(4, 0))
                elif kind == "status":
                    self.status_label.configure(text=payload)
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "done":
                    report_path, cancelled, freed_gb = payload
                    self.report_path = report_path
                    self.status_label.configure(text="Отменено." if cancelled else "Готово. 100%")
                    if not cancelled:
                        self.progress.configure(value=100)
                    self._refresh_disk_usage_label()

                    # Крупная сводка освобождённого места. Величина —
                    # сумма измеренного самими пунктами, поэтому она
                    # никогда не отрицательная (раньше сюда приходила
                    # разница свободного места и могла быть вида -0.01).
                    # Порог 0.01 ГБ отсекает округление до нуля.
                    if not cancelled and freed_gb is not None and freed_gb >= 0.01:
                        self.freed_summary_label.configure(text=f"✓ Освобождено {freed_gb} ГБ")
                        self.freed_summary_label.pack(pady=(2, 4))
                    else:
                        self.freed_summary_label.pack_forget()
                    self.open_report_btn.configure(state="normal")
                    self.restore_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.kill_cleanmgr_btn.pack_forget()
                    self.cleanmgr_pid = None
                    # Таймер останавливается, но последнее значение (общее
                    # время выполнения) остаётся на экране, а не пропадает.
                    self._stop_timer()

                    # Итоговая строка в журнале — сводка по всем пунктам,
                    # чтобы сразу было видно общий результат, не сверяя
                    # значки по каждой строке отдельно.
                    done_count = sum(1 for s in self.current_statuses.values() if s == "done")
                    error_count = sum(1 for s in self.current_statuses.values() if s == "error")
                    total_count = len(self.current_statuses)
                    if cancelled:
                        summary = f"Выполнение отменено: завершено {done_count} из {total_count}."
                    elif error_count:
                        summary = (
                            f"Готово: {done_count} из {total_count} выполнено успешно, "
                            f"{error_count} с ошибкой."
                        )
                    else:
                        summary = f"Готово: все пункты выполнены успешно ({done_count} из {total_count})."
                    self._render_steps_panel(self.current_steps, self.current_statuses, summary=summary)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main():
    if os.name != "nt":
        return

    if not is_admin():
        relaunch_as_admin()
        return

    root = tk.Tk()
    app = CleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

import os
import sys
import time
import json
import shutil
import subprocess
import datetime
import threading
import queue

import tkinter as tk
from tkinter import ttk, messagebox


# ============================================================
# Метаданные приложения
# ============================================================

APP_NAME = "sclean"
APP_VERSION = "1.9.3"
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
    out = run_ps(
        "(powercfg /getactivescheme) -replace '.*GUID: ([0-9a-fA-F-]+).*', '$1'"
    )
    guid = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if guid:
        save_backup({"power_plan_guid": guid})
        logf(f"  Бэкап: текущая схема электропитания сохранена ({guid}).")
    else:
        logf("  Бэкап: не удалось определить текущую схему электропитания.")


def backup_firewall(logf):
    states = {}
    for profile in ("Domain", "Private", "Public"):
        code, out, err = run_cmd(f"netsh advfirewall show {profile.lower()}profile state", timeout=15)
        enabled = "ON" if out and "ON" in out.upper() else "OFF" if out and "OFF" in out.upper() else "UNKNOWN"
        states[profile] = enabled
    save_backup({"firewall_state": states})
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


# Категории бэкапа, доступные для точечного восстановления (используется
# и диалогом "Бэкап", и полным restore_from_backup).
BACKUP_CATEGORIES = ("power_plan", "firewall", "visual_effects")


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

    total_freed = 0
    for label, folder, min_age in targets:
        deleted, errors, freed = safe_delete_files_in(folder, min_age_minutes=min_age)
        total_freed += freed
        logf(f"  {label}: удалено {deleted}, пропущено {errors}")

    logf("Остановка службы wuauserv...")
    run_cmd("net stop wuauserv", timeout=30)
    deleted, errors, freed = safe_delete_files_in(r"C:\Windows\SoftwareDistribution\Download")
    total_freed += freed
    logf(f"  Кэш обновлений Windows: удалено {deleted}, ошибок {errors}")
    run_cmd("net start wuauserv", timeout=30)
    logf("Служба wuauserv запущена обратно.")
    logf(f"  Итого освобождено временными файлами: {format_bytes_gb(total_freed)} ГБ")


def step_recycle_bin(logf):
    logf("Очистка корзины...")
    out = run_ps(
        "try { Clear-RecycleBin -Confirm:$false -ErrorAction Stop; Write-Output OK } "
        "catch { Write-Output $_.Exception.Message }",
        timeout=60,
    )
    logf(f"  Результат: {(out or 'нет ответа').strip()}")


CLEANMGR_SAGESET_ID = "65432"  # произвольный номер профиля, используется только этой программой


def step_cleanmgr(logf, on_pid=None, should_stop=None):
    """
    cleanmgr /sagerun запускается в обычном, полностью видимом режиме —
    пользователь видит прогресс по каждой категории очистки в отдельных
    окошках, которые появляются и исчезают сами по себе (это нормальное
    штатное поведение самого cleanmgr, не баг).

    Важно: нельзя дождаться завершения через proc.wait() на PID
    изначально запущенного процесса — на практике cleanmgr иногда
    порождает дополнительный процесс с тем же именем (или пересоздаёт
    себя) в процессе работы, из-за чего исходный PID завершается
    (proc.wait() возвращается), пока настоящая очистка продолжается в
    другом процессе. Поэтому вместо ожидания одного PID мы каждые пару
    секунд опрашиваем систему по имени процесса "cleanmgr.exe" через
    tasklist и считаем очистку завершённой только тогда, когда таких
    процессов не осталось совсем — это учитывает и дочерние/повторные
    процессы, а не только тот, что мы запустили напрямую.

    on_pid, если передан, вызывается с PID запущенного процесса cleanmgr
    сразу после старта — даёт GUI возможность быстро найти хоть один
    PID для информации, но принудительное завершение (кнопка в GUI)
    всё равно бьёт по имени процесса, а не по этому PID, по той же
    причине. should_stop, если передан, — функция без аргументов,
    возвращающая True, если пользователь запросил принудительную
    остановку через GUI — тогда опрос сразу прекращается.
    """
    logf("Настройка профиля очистки диска (без диалогов выбора категорий)...")

    # Отмечаем "выбрано" (StateFlags<ID>=2) для всех известных категорий
    # очистки диска — тот же набор, что и /verylowdisk выбирает по умолчанию.
    ps = (
        "$base = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VolumeCaches'; "
        "Get-ChildItem $base | ForEach-Object { "
        f"  try {{ Set-ItemProperty -Path $_.PsPath -Name 'StateFlags{CLEANMGR_SAGESET_ID}' -Value 2 -Type DWord -ErrorAction Stop }} catch {{}} "
        "}; "
        "Write-Output OK"
    )
    out = run_ps(ps, timeout=30)
    if out.strip() == "OK":
        logf("  Профиль очистки настроен на все доступные категории.")
    else:
        logf("  Не удалось настроить профиль через реестр, пробуем стандартный режим...")

    logf(f"Запуск cleanmgr /sagerun:{CLEANMGR_SAGESET_ID} — окна прогресса появляются")
    logf("  и закрываются сами по каждой категории, это нормально...")

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    cleanmgr_path = os.path.join(system_root, "System32", "cleanmgr.exe")
    try:
        proc = subprocess.Popen(
            [cleanmgr_path, f"/sagerun:{CLEANMGR_SAGESET_ID}"],
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
    while True:
        if should_stop and should_stop():
            logf("  Очистка диска остановлена принудительно.")
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
                proc.wait()
                break
        else:
            consecutive_errors = 0

        time.sleep(poll_interval)

    logf("  cleanmgr завершён — все процессы cleanmgr.exe закрыты.")


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
    logf("Настройка электропитания: высокая производительность...")
    backup_power_plan(logf)
    HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
    code, out, err = run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=30)
    if code == 0:
        logf("  Схема активирована.")
        return
    logf("  Схема не найдена, создаём из шаблона...")
    run_cmd(f"powercfg -duplicatescheme {HIGH_PERF_GUID}", timeout=30)
    code2, out2, err2 = run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=30)
    if code2 == 0:
        logf("  Схема создана и активирована.")
    else:
        logf(f"  Не удалось активировать (код {code2}): {err2 or err}")


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


def step_system_info(logf):
    logf("Сбор информации о системе...")
    info_lines = []
    info_lines.append(f"Отчёт о системе — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    info_lines.append("=" * 60)

    cpu = run_ps("(Get-CimInstance Win32_Processor | Select-Object -First 1).Name")
    cpu_cores = run_ps("(Get-CimInstance Win32_Processor | Select-Object -First 1).NumberOfCores")
    cpu_threads = run_ps("(Get-CimInstance Win32_Processor | Select-Object -First 1).NumberOfLogicalProcessors")
    info_lines.append("\n[Процессор]")
    info_lines.append(f"Модель: {cpu or 'не удалось определить'}")
    info_lines.append(f"Физических ядер: {cpu_cores or '?'}")
    info_lines.append(f"Логических потоков: {cpu_threads or '?'}")

    ram_total = run_ps("[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 2)")
    ram_speed = run_ps("(Get-CimInstance Win32_PhysicalMemory | Select-Object -First 1).Speed")
    ram_type_raw = run_ps("(Get-CimInstance Win32_PhysicalMemory | Select-Object -First 1).SMBIOSMemoryType")
    ram_modules = run_ps("(Get-CimInstance Win32_PhysicalMemory).Count")
    info_lines.append("\n[Оперативная память]")
    info_lines.append(f"Всего: {ram_total or '?'} ГБ")
    info_lines.append(f"Количество модулей: {ram_modules or '?'}")
    info_lines.append(f"Частота (первый модуль): {ram_speed or '?'} МГц")
    info_lines.append(f"Код типа памяти (SMBIOSMemoryType): {ram_type_raw or '?'}")

    info_lines.append("\n[Диски]")
    disks_raw = run_ps(
        "Get-CimInstance Win32_DiskDrive | ForEach-Object { "
        "'{0} | {1}GB' -f $_.Model, ([math]::Round($_.Size/1GB,1)) }"
    )
    if disks_raw:
        for line in disks_raw.splitlines():
            info_lines.append(f"Физический диск: {line.strip()}")
    else:
        info_lines.append("Не удалось получить список физических дисков.")

    volumes_raw = run_ps(
        "Get-Volume | Where-Object { $_.DriveLetter } | ForEach-Object { "
        "'{0}: всего {1}GB, свободно {2}GB, тип {3}' -f $_.DriveLetter, "
        "([math]::Round($_.Size/1GB,1)), ([math]::Round($_.SizeRemaining/1GB,1)), $_.FileSystem }"
    )
    if volumes_raw:
        for line in volumes_raw.splitlines():
            info_lines.append(f"Раздел {line.strip()}")
    else:
        info_lines.append("Не удалось получить список разделов.")

    media_type_c = get_disk_media_type("C")
    info_lines.append(f"Тип диска C: {media_type_c}")

    # Видеокарта(ы)
    info_lines.append("\n[Видеокарта]")
    gpu_raw = run_ps(
        "Get-CimInstance Win32_VideoController | ForEach-Object { "
        "$vram = if ($_.AdapterRAM -gt 0) { '{0}GB' -f [math]::Round($_.AdapterRAM/1GB,1) } else { '?' }; "
        "'{0} | VRAM: {1}' -f $_.Name, $vram }"
    )
    if gpu_raw:
        for line in gpu_raw.splitlines():
            name = line.strip()
            name_lower = name.lower()
            if any(k in name_lower for k in ("intel", "radeon(tm) graphics", "vega", "amd radeon graphics")) \
               and not any(k in name_lower for k in ("rtx", "gtx", "geforce", "rx 5", "rx 6", "rx 7", "rx 9")):
                kind = "встроенная"
            elif "nvidia" in name_lower or "geforce" in name_lower or "rtx" in name_lower or "gtx" in name_lower:
                kind = "дискретная"
            elif "radeon" in name_lower:
                kind = "дискретная (уточните вручную для APU)"
            else:
                kind = "не определено"
            info_lines.append(f"{name} — {kind}")
    else:
        info_lines.append("Не удалось получить список видеокарт.")

    # Материнская плата
    info_lines.append("\n[Материнская плата]")
    board_maker = run_ps("(Get-CimInstance Win32_BaseBoard).Manufacturer")
    board_model = run_ps("(Get-CimInstance Win32_BaseBoard).Product")
    info_lines.append(f"{(board_maker or '').strip()} {(board_model or '?').strip()}".strip())

    # Устройство
    device_name = run_ps("$env:COMPUTERNAME")
    info_lines.append("\n[Устройство]")
    info_lines.append(f"Имя устройства: {device_name or '?'}")

    # ОС — расширенная информация (аналог "О системе" в параметрах Windows)
    os_name = run_ps("(Get-CimInstance Win32_OperatingSystem).Caption")
    os_build = run_ps("(Get-CimInstance Win32_OperatingSystem).BuildNumber")
    os_ubr = run_ps(
        "try { (Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' "
        "-Name UBR -ErrorAction Stop).UBR } catch { '' }"
    )
    os_display_version = run_ps(
        "try { (Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' "
        "-Name DisplayVersion -ErrorAction Stop).DisplayVersion } catch { '' }"
    )
    os_install_date = run_ps(
        "try { "
        "  $d = (Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' "
        "  -Name InstallDate -ErrorAction Stop).InstallDate; "
        "  ([DateTimeOffset]::FromUnixTimeSeconds($d)).LocalDateTime.ToString('dd.MM.yyyy') "
        "} catch { '' }"
    )
    build_full = f"{os_build}.{os_ubr}" if (os_build and os_ubr) else (os_build or "?")

    info_lines.append("\n[Операционная система]")
    info_lines.append(f"Выпуск: {os_name or '?'}")
    if os_display_version:
        info_lines.append(f"Версия: {os_display_version}")
    if os_install_date:
        info_lines.append(f"Дата установки: {os_install_date}")
    info_lines.append(f"Сборка ОС: {build_full}")

    logf("Информация о системе собрана (см. верх отчёта).")
    return "\n".join(info_lines)


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
     "Запускает встроенную «Очистку диска» Windows по всем доступным категориям.\n"
     "Категории выбираются автоматически, но окно программы отображается как обычно —\n"
     "закройте его сами по завершении очистки. Может занять несколько минут.",
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
    ("internet_speed", "Проверка скорости интернет-соединения", step_internet_speed, True,
     "Измеряет скорость скачивания через Speedtest CLI (если найден) или резервным\n"
     "способом — скачиванием тестового файла.",
     True, False, 30),
]

# Информация о системе (CPU/RAM/диски/GPU/ОС) больше не отдельный пункт
# выбора — она собирается автоматически при каждом запуске и всегда
# попадает в отчёт, независимо от того, какие пункты отмечены.

# id пунктов, отмечаемых пресетом "рекомендуемые настройки" (безопасный
# набор: без sfc/dism — долгий — и без отключения брандмауэра — рискованно).
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
    bat_content = (
        "@echo off\r\n"
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
        with open(bat_path, "w", encoding="mbcs") as f:
            f.write(bat_content)
    except Exception:
        try:
            with open(bat_path, "w", encoding="utf-8") as f:
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
                 risky=False, eta_sec=None, **kwargs):
        super().__init__(parent, bg=DARK_BG, **kwargs)
        self.variable = variable
        self.on_toggle_callback = None

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

    def _draw(self):
        checked = self.variable.get()
        row_bg = DARK_ACCENT if checked else DARK_BG
        text_fg = DARK_ACCENT_TEXT if checked else DARK_FG

        self.configure(bg=row_bg)
        self.box_canvas.configure(bg=row_bg)
        self.label.configure(bg=row_bg, fg=text_fg)
        if self.eta_label is not None:
            self.eta_label.configure(bg=row_bg)

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
        self.system_info_text = ""
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

        ttk.Label(header_frame, text=APP_NAME, style="Header.TLabel",
                  font=(APP_FONT, 14, "bold")).pack(side="left")
        ttk.Label(header_frame, text=f"  v{APP_VERSION}", style="Dim.TLabel").pack(side="left")

        self.update_btn = ttk.Button(header_frame, text="Проверить обновление", command=self.check_update_clicked)
        self.update_btn.pack(side="right")
        self.update_status_label = ttk.Label(header_frame, text="", style="Dim.TLabel")
        self.update_status_label.pack(side="right", padx=(0, 8))
        self._pending_release = None

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
            f"{APP_NAME} — программа для очистки временных файлов, оптимизации диска\n"
            "и настроек Windows одним запуском. Каждый пункт списка ниже можно выполнить\n"
            "по отдельности или всё выбранное сразу.\n\n"
            f"Отчёт о каждом запуске (что выполнено, сколько места освобождено, скорость\n"
            f"интернета, сведения о системе) сохраняется в папку \"{log_dir_hint}\" —\n"
            "файл вида sclean_ГГГГ-ММ-ДД_ЧЧММСС.txt. Там же хранится файл бэкапа\n"
            "изменённых настроек (sclean_backup.json) для восстановления через кнопку «Бэкап».\n"
            "Старые отчёты (более 30 штук) удаляются автоматически."
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
        Tooltip(preset_btn, "Отметить безопасный набор пунктов: без длительной проверки\nsfc/DISM и без отключения брандмауэра.")

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

        for idx, (step_id, title, _func, _ret, desc, _rec, risky, est_sec) in enumerate(STEPS):
            var = tk.BooleanVar(value=False)
            var.trace_add("write", lambda *args: self._sync_master_box())
            self.check_vars[step_id] = var
            row = StepRow(
                steps_frame, title, var,
                on_run_single=lambda sid=step_id: self.run_single(sid),
                description=desc, risky=risky, eta_sec=est_sec,
            )
            row.pack(fill="x")
            if idx < len(STEPS) - 1:
                # Тонкий разделитель (1px) — той же толщины, что и
                # вертикальные разделители у ETA внутри строки.
                sep = tk.Frame(steps_frame, bg=DARK_BORDER, height=1)
                sep.pack(fill="x")
            self.step_rows[step_id] = row

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

        status_row = ttk.Frame(progress_frame)
        status_row.pack(fill="x")

        self.status_label = ttk.Label(status_row, text="Готово к запуску. 0%", font=(APP_FONT, 9))
        self.status_label.pack(side="left", anchor="w")

        # Таймер общего времени выполнения — обновляется раз в секунду,
        # пока идёт выполнение, показывает мин:сек. Отдельно от текста
        # статуса, чтобы не мигать вместе с частой сменой сообщений.
        self.timer_label = ttk.Label(status_row, text="", style="Dim.TLabel", font=(APP_FONT, 9))
        self.timer_label.pack(side="left", padx=(10, 0))
        self.run_start_time = None
        self.timer_after_id = None

        # Кнопка принудительного завершения "Очистки диска" — видна, только
        # пока cleanmgr реально работает в фоне (см. _run_cleanmgr_async /
        # _on_cleanmgr_pid). Позволяет не ждать закрытия её окна вручную.
        self.kill_cleanmgr_btn = ttk.Button(
            status_row, text="Завершить очистку диска", command=self._kill_cleanmgr,
        )
        self.cleanmgr_pid = None
        # Мутабельный словарь (не bool) — чтобы замыкание внутри _worker
        # видело изменения, сделанные позже из GUI-потока по кнопке.
        self.cleanmgr_stop_flag = {"stop": False}

        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=4)

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
        }
        availability = {
            "power_plan": bool(backup.get("power_plan_guid")),
            "firewall": bool(backup.get("firewall_state")),
            "visual_effects": any(backup.get(k) not in (None, "") for k in
                                   ("visual_fx_setting", "min_animate", "drag_full_windows")),
        }
        vars_map = {}
        for cat in BACKUP_CATEGORIES:
            available = availability.get(cat, False)
            var = tk.BooleanVar(value=available)
            vars_map[cat] = var
            text = labels[cat] + ("" if available else " (нет данных в бэкапе)")
            chk = ttk.Checkbutton(dlg, text=text, variable=var, state=("normal" if available else "disabled"))
            chk.pack(anchor="w", padx=14, pady=2)

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
        self.status_label.configure(text="Отмена запрошена — завершится после текущего шага...")

    def _worker(self, steps_to_run):
        def logf(msg):
            # Шаги по-прежнему вызывают logf() по ходу выполнения, но
            # отчёт больше не хранит построчный технический журнал —
            # только сводку (что применено / место / скорость), поэтому
            # здесь ничего не накапливается.
            pass

        start_time = time.time()
        free_before = get_free_space_gb("C:\\")

        collected_texts = []
        failures = []  # (title, причина) для шагов, завершившихся с ошибкой
        failures_lock = threading.Lock()
        cancelled = False

        # "Очистка диска" (cleanmgr) показывает собственное окно, которое
        # пользователь закрывает вручную по завершении — время ожидания
        # непредсказуемо и не должно блокировать остальные пункты. Поэтому
        # cleanmgr, если он выбран, запускается в отдельном фоновом потоке
        # сразу и выполняется параллельно с остальными шагами; основной
        # цикл ниже обрабатывает все прочие пункты последовательно и не
        # ждёт его. Перед финальным отчётом мы дожидаемся завершения этого
        # потока, чтобы "Освобождено места" учитывало и его результат.
        cleanmgr_entry = None
        other_steps = []
        for step in steps_to_run:
            if step[0] == "cleanmgr":
                cleanmgr_entry = step
            else:
                other_steps.append(step)

        cleanmgr_thread = None
        if cleanmgr_entry is not None:
            cleanmgr_id, cleanmgr_title, cleanmgr_func, cleanmgr_returns_text = cleanmgr_entry[:4]
            self.msg_queue.put(("step_status", (cleanmgr_id, "running")))
            self.msg_queue.put(("status", f"Выполняется в фоне: {cleanmgr_title} (закройте окно по завершении)"))

            self.cleanmgr_stop_flag["stop"] = False

            def _on_cleanmgr_pid(pid):
                # Сообщаем GUI-потоку PID запущенного cleanmgr — используется
                # только для отображения кнопки "Завершить очистку диска";
                # само завершение бьёт по имени процесса, а не по этому PID.
                self.msg_queue.put(("cleanmgr_pid", pid))

            def _cleanmgr_should_stop():
                return self.cleanmgr_stop_flag["stop"]

            def _run_cleanmgr_async():
                try:
                    result = cleanmgr_func(logf, on_pid=_on_cleanmgr_pid, should_stop=_cleanmgr_should_stop)
                    if cleanmgr_returns_text and result:
                        collected_texts.append(result)
                    state = "cancelled" if self.cleanmgr_stop_flag["stop"] else "done"
                    self.msg_queue.put(("step_status", (cleanmgr_id, state)))
                except Exception as e:
                    with failures_lock:
                        failures.append((cleanmgr_title, str(e)))
                    self.msg_queue.put(("step_status", (cleanmgr_id, "error")))

            cleanmgr_thread = threading.Thread(target=_run_cleanmgr_async, daemon=True)
            cleanmgr_thread.start()

        total_other = len(other_steps)
        for idx, (step_id, title, func, returns_text, *_rest) in enumerate(other_steps, start=1):
            if self.cancel_requested:
                cancelled = True
                for rem_id, rem_title, *_r2 in other_steps[idx - 1:]:
                    self.msg_queue.put(("step_status", (rem_id, "cancelled")))
                break

            self.msg_queue.put(("step_status", (step_id, "running")))
            pct_before = int(round((idx - 1) / total_other * 100)) if total_other else 0
            eta = STEP_ESTIMATED_SEC.get(step_id)
            eta_txt = f"  (ожидается {format_eta(eta)})" if eta else ""
            self.msg_queue.put(("status", f"Выполняется: {title}  —  {pct_before}%{eta_txt}"))
            try:
                result = func(logf)
                if returns_text and result:
                    collected_texts.append(result)
                self.msg_queue.put(("step_status", (step_id, "done")))
            except Exception as e:
                with failures_lock:
                    failures.append((title, str(e)))
                self.msg_queue.put(("step_status", (step_id, "error")))
            pct_after = int(round(idx / total_other * 100)) if total_other else 100
            self.msg_queue.put(("progress", pct_after))

        if cleanmgr_thread is not None:
            # Периодически обновляем статус с прошедшим временем ожидания,
            # вместо одного статичного сообщения на весь период join() —
            # так по интерфейсу видно, что программа не зависла, а реально
            # ждёт, пока пользователь закроет окно очистки диска.
            wait_start = time.time()
            while cleanmgr_thread.is_alive():
                waited = int(time.time() - wait_start)
                self.msg_queue.put((
                    "status",
                    f"Очистка диска работает в фоне ({format_eta(waited)}) — "
                    f"закройте её окно, когда закончит, или нажмите «Завершить очистку диска»",
                ))
                cleanmgr_thread.join(timeout=2)

        # Информация о системе собирается всегда, отдельно от выбранных
        # пунктов — это не пункт выбора, а неизменная часть каждого отчёта.
        # Если пользователь отменил выполнение — не собираем её, отчёт и
        # так будет отражать факт отмены.
        system_info_text = ""
        if not cancelled:
            self.msg_queue.put(("status", "Сбор информации о системе..."))
            try:
                system_info_text = step_system_info(logf)
            except Exception as e:
                failures.append(("Сбор информации о системе", str(e)))

        collected_system_info = "\n".join(collected_texts)

        free_after = get_free_space_gb("C:\\")
        elapsed = time.time() - start_time

        report_path = self._write_report(
            collected_system_info, system_info_text, free_before, free_after, elapsed, failures, cancelled
        )
        rotate_old_reports()

        self.msg_queue.put(("done", (report_path, cancelled)))

    def _write_report(self, speed_text, system_info_text, free_before, free_after, elapsed_sec, failures, cancelled):
        date_str = datetime.date.today().isoformat()
        time_str = datetime.datetime.now().strftime("%H%M%S")
        report_path = os.path.join(get_app_data_dir(), f"sclean_{date_str}_{time_str}.txt")

        freed = None
        if free_before is not None and free_after is not None:
            freed = round(free_after - free_before, 2)

        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("ОТЧЁТ ОБ ОЧИСТКЕ И ОПТИМИЗАЦИИ СИСТЕМЫ\n")
                f.write(f"{APP_NAME} build {APP_VERSION}  ·  автор: {APP_AUTHOR}\n")
                f.write("=" * 60 + "\n\n")

                if cancelled:
                    f.write("Внимание: выполнение было прервано пользователем (отмена).\n\n")

                # Причины невыполнения пунктов показываются, только если
                # что-то реально не сработало — если всё прошло без ошибок,
                # этого раздела в отчёте не будет вовсе.
                if failures:
                    f.write("Не удалось выполнить:\n")
                    for title, reason in failures:
                        f.write(f"  - {title}: {reason}\n")
                    f.write("\n")

                f.write(f"Свободно на диске C до очистки:  {free_before} ГБ\n")
                f.write(f"Свободно на диске C после очистки: {free_after} ГБ\n")
                if freed is not None:
                    f.write(f"Освобождено места: {freed} ГБ\n")
                f.write(f"Общее время выполнения: {elapsed_sec:.1f} сек\n")

                if speed_text:
                    f.write("\n" + speed_text.strip() + "\n")

                if system_info_text:
                    f.write("\n" + system_info_text + "\n")
        except Exception:
            pass

        return report_path

    def open_report(self):
        if self.report_path and os.path.isfile(self.report_path):
            os.startfile(self.report_path)

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
                    self.kill_cleanmgr_btn.pack(side="right")
                elif kind == "status":
                    self.status_label.configure(text=payload)
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "done":
                    report_path, cancelled = payload
                    self.report_path = report_path
                    self.status_label.configure(text="Отменено." if cancelled else "Готово. 100%")
                    if not cancelled:
                        self.progress.configure(value=100)
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

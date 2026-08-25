import os
import sys
import time
import shutil
import subprocess
import datetime


# ============================================================
# Утилиты
# ============================================================

LOG_LINES = []


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG_LINES.append(f"[{ts}] {msg}")


def run_cmd(cmd, timeout=None):
    """Выполняет команду, возвращает (returncode, stdout, stderr). Не бросает исключений наружу."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def run_ps(ps_command, timeout=None):
    """Выполняет команду PowerShell, возвращает stdout (строку) или "" при ошибке."""
    cmd = f'powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "{ps_command}"'
    code, out, err = run_cmd(cmd, timeout=timeout)
    return out.strip() if code == 0 else ""


def get_free_space_gb(drive="C:\\"):
    try:
        total, used, free = shutil.disk_usage(drive)
        return round(free / (1024 ** 3), 2)
    except Exception:
        return None


# Файлы/расширения, которые никогда не трогаем даже внутри "безопасных" папок
PROTECTED_NAME_HINTS = ("desktop.ini", ".lnk")


def safe_delete_files_in(folder, skip_locked=True, min_age_minutes=0):
    """
    Безопасно чистит содержимое папки:
    - не трогает системные ссылки/desktop.ini;
    - не падает на файлах, занятых другим процессом (PermissionError просто пропускается);
    - опционально не трогает файлы младше min_age_minutes (чтобы не задеть то, что сейчас пишется).
    Возвращает (удалено_файлов, ошибок).
    """
    deleted = 0
    errors = 0

    if not folder or not os.path.isdir(folder):
        return deleted, errors

    now = time.time()
    min_age_sec = min_age_minutes * 60

    try:
        entries = os.listdir(folder)
    except (PermissionError, FileNotFoundError, OSError):
        return deleted, errors

    for name in entries:
        if name.lower() in PROTECTED_NAME_HINTS:
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
                os.remove(path)
                deleted += 1

            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                deleted += 1

        except (PermissionError, OSError):
            # файл занят другим процессом или защищён — пропускаем, не роняем скрипт
            errors += 1
            continue

    return deleted, errors


# ============================================================
# Шаг 1: очистка временных файлов и системного мусора
# ============================================================

def clean_temp_and_logs():
    log("=== Очистка временных файлов и логов ===")
    user = os.getenv("USERNAME")

    targets = []

    temp = os.getenv("TEMP")
    if temp:
        targets.append(("TEMP пользователя", temp, 0))

    if user:
        local_temp = fr"C:\Users\{user}\AppData\Local\Temp"
        targets.append(("Local Temp", local_temp, 0))

    targets.append(("Windows Temp", r"C:\Windows\Temp", 0))
    targets.append(("Логи CBS", r"C:\Windows\Logs\CBS", 0))
    targets.append(("Логи DISM", r"C:\Windows\Logs\DISM", 0))
    # Prefetch не трогаем свежие файлы (влияют на скорость запуска приложений
    # в ближайшее время), чистим только то, что старше суток.
    targets.append(("Prefetch (>24ч)", r"C:\Windows\Prefetch", 24 * 60))
    # Отчёты об ошибках Windows — безопасно чистить целиком
    targets.append(("WER ReportQueue", r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue", 0))
    targets.append(("WER ReportArchive", r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive", 0))
    # Кэш миниатюр explorer (безопасно, пересоздаётся автоматически)
    if user:
        thumb_cache = fr"C:\Users\{user}\AppData\Local\Microsoft\Windows\Explorer"
        targets.append(("Кэш миниатюр Explorer", thumb_cache, 0))

    for label, folder, min_age in targets:
        deleted, errors = safe_delete_files_in(folder, min_age_minutes=min_age)
        log(f"{label}: удалено объектов {deleted}, пропущено (занято/защищено) {errors} — {folder}")

    # Кэш обновлений Windows: останавливаем службу, чистим, запускаем обратно
    log("Остановка службы Центра обновлений (wuauserv)...")
    run_cmd("net stop wuauserv", timeout=30)
    deleted, errors = safe_delete_files_in(r"C:\Windows\SoftwareDistribution\Download")
    log(f"Кэш обновлений Windows: удалено объектов {deleted}, ошибок {errors}")
    run_cmd("net start wuauserv", timeout=30)
    log("Служба wuauserv запущена обратно.")


# ============================================================
# Шаг 2: корзина
# ============================================================

def clean_recycle_bin():
    log("=== Очистка корзины ===")
    code, out, err = run_cmd(
        'powershell -NoLogo -NoProfile -Command '
        '"try { Clear-RecycleBin -Confirm:$false -ErrorAction Stop; Write-Output OK } '
        'catch { Write-Output ($_.Exception.Message) }"',
        timeout=60,
    )
    result = (out or err or "нет ответа").strip()
    log(f"Очистка корзины: {result}")


# ============================================================
# Шаг 3: cleanmgr
# ============================================================

def run_cleanmgr_verylowdisk():
    log("=== cleanmgr /verylowdisk ===")
    code, out, err = run_cmd("cleanmgr /verylowdisk", timeout=600)
    log(f"cleanmgr завершён, код возврата: {code}")


# ============================================================
# Шаг 4: SFC / DISM (оставлены по решению пользователя)
# ============================================================

def run_sfc_and_dism():
    log("=== Проверка целостности системы: SFC ===")
    code, out, err = run_cmd("sfc /scannow", timeout=1800)
    log(f"sfc /scannow завершён, код возврата: {code}")

    log("=== Восстановление образа: DISM RestoreHealth ===")
    code, out, err = run_cmd("DISM /Online /Cleanup-Image /RestoreHealth", timeout=1800)
    log(f"DISM RestoreHealth завершён, код возврата: {code}")


# ============================================================
# Шаг 5: дефрагментация / TRIM с автоопределением типа диска
# ============================================================

def get_disk_media_type(drive_letter="C"):
    """
    Возвращает 'SSD', 'HDD' или 'Unknown' для указанного диска,
    используя Get-PhysicalDisk (Windows 8/2012+).
    """
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


def run_defrag_or_trim():
    log("=== Оптимизация диска C: (дефрагментация/TRIM) ===")
    media_type = get_disk_media_type("C")
    log(f"Определён тип диска C: {media_type}")

    if media_type == "SSD":
        log("SSD обнаружен — полная дефрагментация пропущена, выполняется только TRIM.")
        code, out, err = run_cmd("defrag C: /L /V", timeout=600)
        log(f"TRIM (defrag /L) завершён, код возврата: {code}")
    elif media_type == "HDD":
        log("HDD обнаружен — выполняется полная дефрагментация.")
        code, out, err = run_cmd("defrag C: /U /V", timeout=1800)
        log(f"Дефрагментация завершена, код возврата: {code}")
    else:
        log("Тип диска не определён — используется безопасный режим Windows: defrag C: /O (оптимизация по умолчанию для типа тома).")
        code, out, err = run_cmd("defrag C: /O /V", timeout=1800)
        log(f"Оптимизация завершена, код возврата: {code}")


# ============================================================
# Шаг 6: план электропитания — высокая производительность
# ============================================================

def set_high_performance_power_plan():
    log("=== Настройка электропитания: высокая производительность ===")

    # Стандартный GUID схемы "Высокая производительность" в Windows
    HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

    code, out, err = run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=30)
    if code == 0:
        log("Схема электропитания переключена на 'Высокая производительность'.")
        return

    # Если схема не была в списке доступных — создаём её из встроенного шаблона
    log("Готовая схема не найдена, пробуем добавить её из встроенного набора...")
    run_cmd(f"powercfg -duplicatescheme {HIGH_PERF_GUID}", timeout=30)
    code2, out2, err2 = run_cmd(f"powercfg /setactive {HIGH_PERF_GUID}", timeout=30)
    if code2 == 0:
        log("Схема 'Высокая производительность' создана и активирована.")
    else:
        log(f"Не удалось активировать высокую производительность (код {code2}): {err2 or err}")


# ============================================================
# Шаг 7: брандмауэр — отключение всех профилей
# ============================================================

def disable_firewall_all_profiles():
    log("=== Отключение брандмауэра Windows (все профили) ===")
    code, out, err = run_cmd("netsh advfirewall set allprofiles state off", timeout=30)
    if code == 0:
        log("Брандмауэр отключён во всех профилях (Domain/Private/Public).")
    else:
        log(f"Не удалось отключить брандмауэр (код {code}): {err}")


# ============================================================
# Шаг 8: сбор информации о системе
# ============================================================

def collect_system_info():
    log("=== Сбор информации о системе ===")
    info_lines = []
    info_lines.append(f"Отчёт о системе — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    info_lines.append("=" * 60)

    # Процессор
    cpu = run_ps(
        "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"
    )
    cpu_cores = run_ps(
        "(Get-CimInstance Win32_Processor | Select-Object -First 1).NumberOfCores"
    )
    cpu_threads = run_ps(
        "(Get-CimInstance Win32_Processor | Select-Object -First 1).NumberOfLogicalProcessors"
    )
    info_lines.append("\n[Процессор]")
    info_lines.append(f"Модель: {cpu or 'не удалось определить'}")
    info_lines.append(f"Физических ядер: {cpu_cores or '?'}")
    info_lines.append(f"Логических потоков: {cpu_threads or '?'}")

    # Оперативная память
    ram_total = run_ps(
        "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 2)"
    )
    ram_speed = run_ps(
        "(Get-CimInstance Win32_PhysicalMemory | Select-Object -First 1).Speed"
    )
    ram_type_raw = run_ps(
        "(Get-CimInstance Win32_PhysicalMemory | Select-Object -First 1).SMBIOSMemoryType"
    )
    ram_modules = run_ps(
        "(Get-CimInstance Win32_PhysicalMemory).Count"
    )
    info_lines.append("\n[Оперативная память]")
    info_lines.append(f"Всего: {ram_total or '?'} ГБ")
    info_lines.append(f"Количество модулей: {ram_modules or '?'}")
    info_lines.append(f"Частота (первый модуль): {ram_speed or '?'} МГц")
    info_lines.append(f"Код типа памяти (SMBIOSMemoryType): {ram_type_raw or '?'}")

    # Диски
    info_lines.append("\n[Диски]")
    disks_raw = run_ps(
        "Get-CimInstance Win32_DiskDrive | ForEach-Object { "
        "\\\"$($_.Model) | $([math]::Round($_.Size/1GB,1))GB\\\" }"
    )
    if disks_raw:
        for line in disks_raw.splitlines():
            info_lines.append(f"Физический диск: {line.strip()}")
    else:
        info_lines.append("Не удалось получить список физических дисков.")

    volumes_raw = run_ps(
        "Get-Volume | Where-Object { $_.DriveLetter } | ForEach-Object { "
        "\\\"$($_.DriveLetter): всего $([math]::Round($_.Size/1GB,1))GB, "
        "свободно $([math]::Round($_.SizeRemaining/1GB,1))GB, "
        "тип $($_.FileSystem)\\\" }"
    )
    if volumes_raw:
        for line in volumes_raw.splitlines():
            info_lines.append(f"Раздел {line.strip()}")
    else:
        info_lines.append("Не удалось получить список разделов.")

    media_type_c = get_disk_media_type("C")
    info_lines.append(f"Тип диска C: {media_type_c}")

    # ОС
    os_name = run_ps("(Get-CimInstance Win32_OperatingSystem).Caption")
    os_build = run_ps("(Get-CimInstance Win32_OperatingSystem).BuildNumber")
    info_lines.append("\n[Операционная система]")
    info_lines.append(f"{os_name or '?'} (build {os_build or '?'})")

    log("Информация о системе собрана.")
    return "\n".join(info_lines)


# ============================================================
# Отчёт
# ============================================================

def get_desktop_dir():
    userprofile = os.getenv("USERPROFILE")
    if not userprofile:
        return None
    return os.path.join(userprofile, "Desktop")


def write_report(system_info_text, free_before, free_after, elapsed_sec):
    desktop = get_desktop_dir()
    date_str = datetime.date.today().isoformat()

    if desktop and os.path.isdir(desktop):
        report_path = os.path.join(desktop, f"system_cleaner_report_{date_str}.txt")
    else:
        report_path = os.path.join(os.getenv("TEMP", "."), f"system_cleaner_report_{date_str}.txt")

    freed = None
    if free_before is not None and free_after is not None:
        freed = round(free_after - free_before, 2)

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("ОТЧЁТ ОБ ОЧИСТКЕ И ОПТИМИЗАЦИИ СИСТЕМЫ\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Свободно на диске C до очистки:  {free_before} ГБ\n")
            f.write(f"Свободно на диске C после очистки: {free_after} ГБ\n")
            if freed is not None:
                f.write(f"Освобождено места: {freed} ГБ\n")
            f.write(f"Общее время выполнения: {elapsed_sec:.1f} сек\n\n")

            f.write(system_info_text)
            f.write("\n\n")

            f.write("ЖУРНАЛ ВЫПОЛНЕНИЯ\n")
            f.write("=" * 60 + "\n")
            f.write("\n".join(LOG_LINES))
            f.write("\n")
    except Exception:
        pass

    return report_path


# ============================================================
# Точка входа
# ============================================================

def main():
    if os.name != "nt":
        return

    start_time = time.time()
    free_before = get_free_space_gb("C:\\")
    log(f"Свободно на диске C перед стартом: {free_before} ГБ")

    clean_temp_and_logs()
    clean_recycle_bin()
    run_cleanmgr_verylowdisk()
    run_sfc_and_dism()
    run_defrag_or_trim()
    set_high_performance_power_plan()
    disable_firewall_all_profiles()

    system_info_text = collect_system_info()

    free_after = get_free_space_gb("C:\\")
    log(f"Свободно на диске C после завершения: {free_after} ГБ")

    elapsed = time.time() - start_time
    report_path = write_report(system_info_text, free_before, free_after, elapsed)
    # Никаких print/input — тихий режим. Единственный видимый результат — txt-отчёт.


if __name__ == "__main__":
    main()

"""Daily backup of the live data: a safe copy of the database, plus uploads, exports and local settings, in one dated zip file.

Runs by itself in the backup container. For one backup straight away:  python deploy/backup.py --once
"""

from datetime import datetime, timedelta
import os
import signal
import sqlite3
import sys
import tempfile
import time
import zipfile

DATA_DIR = os.environ.get("PAYROLL_DATA_DIR", "/data")
BACKUP_DIR = os.environ.get("PAYROLL_BACKUP_DIR", "/backups")
KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS") or 14)
DATABASE = "sic_payroll.db"
FOLDERS = ("uploads", "exports")
BACKUP_HOUR = 2  # 2 a.m., when nobody is working

def make_backup():
    """Write one verified backup and return its path."""
    source = os.path.join(DATA_DIR, DATABASE)
    if not os.path.exists(source):
        raise FileNotFoundError(f"{source} not found")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"payroll_backup_{datetime.now().strftime('%Y-%m-%d_%H%M')}.zip"
    final_path = os.path.join(BACKUP_DIR, name)
    with tempfile.TemporaryDirectory() as work:
        copy_path = os.path.join(work, DATABASE)
        live = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
        copy = sqlite3.connect(copy_path)
        live.backup(copy)  # a consistent copy, even while people are using the app
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        copy.close()
        live.close()
        if result != "ok":
            raise RuntimeError(f"database copy failed its integrity check: {result}")
        partial_path = final_path + ".partial"
        with zipfile.ZipFile(partial_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy_path, DATABASE)
            settings = os.path.join(DATA_DIR, "local_settings.json")
            if os.path.exists(settings):
                archive.write(settings, "local_settings.json")
            for folder in FOLDERS:
                for root, _, files in os.walk(os.path.join(DATA_DIR, folder)):
                    for file_name in files:
                        path = os.path.join(root, file_name)
                        archive.write(path, os.path.relpath(path, DATA_DIR))
        os.replace(partial_path, final_path)
    return final_path

def remove_old_backups():
    """Delete backups past the keep period, always leaving the newest one."""
    backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("payroll_backup_") and f.endswith(".zip"))
    cutoff = time.time() - timedelta(days=KEEP_DAYS).total_seconds()
    for file_name in backups[:-1]:
        path = os.path.join(BACKUP_DIR, file_name)
        if os.path.getmtime(path) < cutoff:
            os.remove(path)

def backed_up_today():
    if not os.path.isdir(BACKUP_DIR):
        return False
    today = f"payroll_backup_{datetime.now().strftime('%Y-%m-%d')}_"
    return any(f.startswith(today) and f.endswith(".zip") for f in os.listdir(BACKUP_DIR))

def run_backup():
    path = make_backup()
    remove_old_backups()
    print(f"Backup saved: {os.path.basename(path)} ({os.path.getsize(path) / 1024:,.0f} KB)", flush=True)

def main():
    if "--once" in sys.argv:
        run_backup()
        return
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    while True:
        if datetime.now().hour >= BACKUP_HOUR and not backed_up_today():
            try:
                run_backup()
            except Exception as error:  # tried again in an hour
                print(f"BACKUP FAILED: {type(error).__name__}: {error}", flush=True)
        time.sleep(3600)

if __name__ == "__main__":
    main()

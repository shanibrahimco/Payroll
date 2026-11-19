"""Packs this computer's live Shan Ibrahim & Co. data into one zip file to upload to the server.

Run from the project folder, after stopping the app:  python deploy/prepare_data.py
The zip holds salaries, CNICs and bank details: keep it private and delete it once the server is running.
"""

import os
import sqlite3
import sys
import tempfile
import zipfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE = "sic_payroll.db"

def main():
    output = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(PROJECT, "payroll_data_for_server.zip")
    source = os.path.join(PROJECT, DATABASE)
    with tempfile.TemporaryDirectory() as work:
        copy_path = os.path.join(work, DATABASE)
        live = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
        copy = sqlite3.connect(copy_path)
        live.backup(copy)
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        staff = copy.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
        copy.close()
        live.close()
        if result != "ok":
            sys.exit(f"Stopped: the database failed its integrity check ({result}).")
        count = 1
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy_path, DATABASE)
            settings = os.path.join(PROJECT, "local_settings.json")
            if os.path.exists(settings):
                archive.write(settings, "local_settings.json")
                count += 1
            for folder in ("uploads", "exports"):
                for root, _, files in os.walk(os.path.join(PROJECT, folder)):
                    for file_name in files:
                        path = os.path.join(root, file_name)
                        archive.write(path, os.path.relpath(path, PROJECT))
                        count += 1
    print(f"Ready: {output}")
    print(f"{count} file(s), {staff} staff records, database integrity OK.")

if __name__ == "__main__":
    main()

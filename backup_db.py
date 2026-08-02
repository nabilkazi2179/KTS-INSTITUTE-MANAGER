"""Create a safe, timestamped backup of the local SQLite database.

Uses SQLite's online backup API, so it is safe to run while the app is
serving traffic - no risk of copying a half-written file.

Usage:
    python backup_db.py                  # -> backups/kts_YYYYMMDD_HHMMSS.db
    python backup_db.py --dir D:/backups # custom destination
    python backup_db.py --keep 30        # prune to the newest 30 backups
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def find_db():
    explicit = os.environ.get('SQLITE_PATH', '').strip()
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(BASE_DIR, 'kts_institute.db')


def backup(src, dest_dir, keep):
    if not os.path.exists(src):
        print(f'ERROR: no database found at {src}')
        print('Run the app at least once first, or set SQLITE_PATH.')
        return 1

    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(dest_dir, f'kts_{stamp}.db')

    source = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    target = sqlite3.connect(dest)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()

    size_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f'Backed up -> {dest}  ({size_mb:.2f} MB)')

    # Verify the copy actually opens and has the expected tables.
    check = sqlite3.connect(dest)
    try:
        students = check.execute('SELECT COUNT(*) FROM students').fetchone()[0]
        payments = check.execute('SELECT COUNT(*) FROM fee_payments').fetchone()[0]
        print(f'Verified: {students} students, {payments} payments.')
    except sqlite3.Error as e:
        print(f'WARNING: backup verification failed: {e}')
        return 1
    finally:
        check.close()

    if keep:
        backups = sorted(
            (f for f in os.listdir(dest_dir)
             if f.startswith('kts_') and f.endswith('.db')),
            reverse=True)
        for old in backups[keep:]:
            os.remove(os.path.join(dest_dir, old))
            print(f'Pruned old backup: {old}')
    return 0


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Back up the KTS SQLite database.')
    p.add_argument('--dir', default=os.path.join(BASE_DIR, 'backups'),
                   help='destination directory (default: ./backups)')
    p.add_argument('--keep', type=int, default=30,
                   help='how many backups to retain, 0 = keep all (default: 30)')
    a = p.parse_args()
    sys.exit(backup(find_db(), a.dir, a.keep))

"""
RigSync: one-way upload of acquisition data from a rig PC to the lab server, made for the Green lab's imaging pipeline
(mouse_imaging).

Like Create Synchronicity's "left to right (incremental)" mode, but it uploads each file only once. A ledger on the
rig PC remembers every file it uploaded, so a file the server later compresses (X.tif -> X.tif.zst, see
mouse_imaging's compress.py), moves or deletes is never uploaded again. Optionally it deletes files from the rig PC
once the server has a verified copy.

    - A file is uploaded when it hasn't changed for `min_age_min` minutes (ScanImage may still be writing it). It's
      written to the server as <name>.rigsync-tmp, checked against the original's SHA-256, then renamed, so the
      pipeline never sees a partial file. The modification time is kept.
    - Files already on the server (same size, or a .zst whose header records the same original size) are adopted
      into the ledger without copying, e.g. what Create Synchronicity uploaded before.
    - Nothing on the server is ever deleted. If the server folder isn't reachable, the run stops without doing
      anything.
    - Cleanup (optional, per profile): a file is deleted from the rig PC `keep_days` after its upload, and only if
      the server still has it. Mode 'compressed' also waits until the pipeline has compressed TIFFs to .tif.zst. A file
      that was adopted rather than uploaded by RigSync is compared byte for byte (SHA-256) with the server's copy
      first; reading a .zst for that needs the `zstandard` package (included in RigSync.exe).

Usage:
    RigSync.exe                       # GUI (or: python rigsync.py)
    RigSync.exe --run "Profile"       # run one profile without a window, as the scheduled task does
    RigSync.exe --run "Profile" --dry-run
    RigSync.exe --schedule "Profile"  # create the daily scheduled task (same as the Schedule button)

Settings, ledgers and logs are in %APPDATA%\\RigSync (override with the RIGSYNC_HOME environment variable).
See README.md next to this file.
"""
import os, re, sys, json, stat, time, queue, fnmatch, hashlib, sqlite3, logging, argparse, threading, subprocess
from pathlib import Path
from logging.handlers import RotatingFileHandler

APP = 'RigSync'
VERSION = '1.0'
CHUNK = 8 * 1024 * 1024
TMP_SUFFIX = '.rigsync-tmp'
ZST = '.zst'
TIF_EXT = ('.tif', '.tiff')
DEFAULT_EXCLUDE = ['.DS_Store', '._*', 'Thumbs.db', 'desktop.ini', '~$*', '*.tmp', '*' + TMP_SUFFIX,
                   '$RECYCLE.BIN', 'System Volume Information']
CLEANUP_MODES = {'never': 'Never delete from this PC',
                 'compressed': 'Delete once processed (compressed) on the server',
                 'uploaded': 'Delete once uploaded'}
DEFAULT_PROFILE = {'source': '', 'dest': '', 'exclude': DEFAULT_EXCLUDE, 'min_age_min': 5, 'verify': True,
                   'cleanup': 'never', 'keep_days': 7, 'run_at': '22:00'}
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW: no console flash when calling schtasks

# ---- Settings ----

def home():
    if os.environ.get('RIGSYNC_HOME'):
        d = Path(os.environ['RIGSYNC_HOME'])
    elif os.name == 'nt':
        d = Path(os.environ.get('APPDATA', Path.home())) / APP
    else:
        d = Path.home() / '.config' / 'rigsync'
    d.mkdir(parents=True, exist_ok=True)
    return d

def safe_name(name):
    return re.sub(r'[^\w.-]', '_', name)

def load_profiles():
    f = home() / 'profiles.json'
    profiles = json.loads(f.read_text(encoding='utf-8-sig')) if f.exists() else {}
    return {name: {**DEFAULT_PROFILE, **p} for name, p in profiles.items()}

def save_profiles(profiles):
    f = home() / 'profiles.json'
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps(profiles, indent=2), encoding='utf-8')
    os.replace(tmp, f)

def parse_times(text):
    """'22:00' or '12:00, 22:00' -> ['12:00', '22:00']. Raises ValueError for anything else."""
    times = []
    for part in re.split(r'[,;\s]+', text.strip()):
        if not part:
            continue
        m = re.fullmatch(r'(\d{1,2}):(\d{2})', part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f'"{part}" is not a time like 22:00')
        times.append(f'{int(m.group(1)):02d}:{m.group(2)}')
    if not times:
        raise ValueError('Enter at least one time, like 22:00')
    return sorted(set(times))

def check_profile(p):
    """Problems that prevent running a profile (list of messages)."""
    problems = []
    src, dst = p.get('source', '').strip(), p.get('dest', '').strip()
    if not src or not os.path.isdir(src):
        problems.append(f'Source folder not found: {src or "(not set)"}')
    if not dst or not os.path.isdir(dst):
        problems.append(f'Server folder not reachable: {dst or "(not set)"}')
    if src and dst and os.path.isdir(src) and os.path.isdir(dst):
        a, b = os.path.realpath(src), os.path.realpath(dst)
        if os.path.normcase(a) == os.path.normcase(b) or os.path.normcase(b).startswith(os.path.normcase(a) + os.sep) \
                or os.path.normcase(a).startswith(os.path.normcase(b) + os.sep):
            problems.append('Source and server folders must not contain each other')
    return problems

# ---- Helpers ----

def zst_content_size(path):
    """Original size recorded in a zstd frame header, or None (no file, not zstd, size not recorded)."""
    try:
        with open(path, 'rb') as f:
            h = f.read(18)
    except OSError:
        return None
    if len(h) < 6 or h[:4] != b'\x28\xb5\x2f\xfd':
        return None
    fhd = h[4]
    fcs_flag, single_segment, dict_flag = fhd >> 6, (fhd >> 5) & 1, fhd & 3
    pos = 5 + (0 if single_segment else 1) + (0, 1, 2, 4)[dict_flag]
    n = (1 if single_segment else 0, 2, 4, 8)[fcs_flag]
    if n == 0 or len(h) < pos + n:
        return None
    value = int.from_bytes(h[pos:pos + n], 'little')
    return value + 256 if n == 2 else value

def sha256_file(path, stop=None, zstd_compressed=False):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        if zstd_compressed:
            import zstandard
            f = zstandard.ZstdDecompressor().stream_reader(f)
        while chunk := f.read(CHUNK):
            if stop is not None and stop.is_set():
                raise Stopped()
            h.update(chunk)
    return h.hexdigest()

def fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1000 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1000

def stat_or_none(path):
    try:
        return os.stat(path)
    except OSError:
        return None

class Stopped(Exception):
    pass

class ProfileLock:
    """Exclusive per-profile lock, so a scheduled run and a run from the window never overlap. Released if the
    process dies."""
    def __init__(self, name):
        self.path = home() / 'locks' / (safe_name(name) + '.lock')
        self.f = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, 'a+')
        try:
            if os.name == 'nt':
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.f.close()
            self.f = None
            return False

    def release(self):
        if self.f:
            try:
                if os.name == 'nt':
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.f.close()
                self.f = None

# ---- Ledger ----

class Ledger:
    """What RigSync has uploaded (or found on the server) and deleted, per profile. SQLite in the settings folder."""
    def __init__(self, name):
        self.path = home() / 'ledgers' / (safe_name(name) + '.sqlite')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS files (rel TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT,
                method TEXT, uploaded_at REAL, verified INTEGER, deleted_at REAL, note TEXT);
            CREATE TABLE IF NOT EXISTS runs (started REAL, finished REAL, dry_run INTEGER, result TEXT, summary TEXT);
        ''')

    def all(self):
        return {r['rel']: r for r in self.db.execute('SELECT * FROM files')}

    def record(self, rel, size, mtime_ns, sha256, method, verified):
        self.db.execute('INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)',
                        (rel, size, mtime_ns, sha256, method, time.time(), int(verified)))
        self.db.commit()

    def update(self, rel, **fields):
        self.db.execute(f'UPDATE files SET {", ".join(k + " = ?" for k in fields)} WHERE rel = ?', (*fields.values(), rel))
        self.db.commit()

    def add_run(self, started, dry_run, result, summary):
        self.db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?)', (started, time.time(), int(dry_run), result, json.dumps(summary)))
        self.db.commit()

    def stats(self):
        r = self.db.execute('''SELECT COUNT(*) n, COALESCE(SUM(size), 0) bytes, COALESCE(SUM(deleted_at IS NOT NULL), 0) n_deleted,
                               COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN size END), 0) bytes_deleted FROM files''').fetchone()
        last = self.db.execute('SELECT * FROM runs WHERE dry_run = 0 ORDER BY started DESC LIMIT 1').fetchone()
        return dict(r), (dict(last) if last else None)

    def close(self):
        self.db.close()

# ---- Sync ----

def get_logger(name):
    logger = logging.getLogger(f'{APP}.{safe_name(name)}')
    if not logger.handlers:
        log_dir = home() / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / (safe_name(name) + '.log'), maxBytes=5_000_000, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger

class Syncer:
    """One run of a profile. `say(level, message)` and `progress(done_bytes, total_bytes, name)` report to the window."""
    def __init__(self, name, profile, say=None, progress=None, stop=None):
        self.name, self.p = name, profile
        self.src, self.dst = Path(profile['source']), Path(profile['dest'])
        self.logger = get_logger(name)
        self._say, self._progress = say, progress
        self.stop = stop or threading.Event()

    def say(self, level, message):
        self.logger.log(level, message)
        if self._say:
            self._say(level, message)

    def excluded(self, name):
        return any(fnmatch.fnmatch(name, pat) for pat in self.p['exclude'])

    def walk(self):
        """(rel, path, stat) for every file under the source folder."""
        for root, dirs, files in os.walk(self.src):
            dirs[:] = sorted(d for d in dirs if not self.excluded(d))
            for f in sorted(files):
                if self.excluded(f):
                    continue
                path = os.path.join(root, f)
                st = stat_or_none(path)
                if st:
                    yield Path(path).relative_to(self.src).as_posix(), path, st

    def server_copy(self, rel, size):
        """How the server holds this file: 'plain' (same size), 'compressed' (.zst of the same size) or None."""
        d = self.dst / rel
        ds = stat_or_none(d)
        if ds and ds.st_size == size:
            return 'plain'
        if zst_content_size(str(d) + ZST) == size:
            return 'compressed'
        return None

    def plan(self, ledger):
        """Classify every source file: copy, adopt (already on the server), done (in the ledger) or wait (too new)."""
        now, min_age = time.time(), self.p['min_age_min'] * 60
        rows = ledger.all()
        plan = {'copy': [], 'adopt': [], 'done': [], 'wait': []}
        for rel, path, st in self.walk():
            if self.stop.is_set():
                raise Stopped()
            row = rows.get(rel)
            if now - st.st_mtime < min_age:
                plan['wait'].append((rel, path, st, None))
            elif row and row['deleted_at'] is None and row['size'] == st.st_size and row['mtime_ns'] == st.st_mtime_ns:
                plan['done'].append((rel, path, st, row))
            else:
                held = self.server_copy(rel, st.st_size)
                if held:
                    plan['adopt'].append((rel, path, st, held))
                else:
                    plan['copy'].append((rel, path, st, 'replace' if (self.dst / rel).exists() else 'new'))
        return plan

    def copy_file(self, rel, path, st, done_bytes, total_bytes):
        d = self.dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.with_name(d.name + TMP_SUFFIX)
        h = hashlib.sha256()
        try:
            with open(path, 'rb') as fin, open(tmp, 'wb') as fout:
                n = 0
                while chunk := fin.read(CHUNK):
                    if self.stop.is_set():
                        raise Stopped()
                    h.update(chunk)
                    fout.write(chunk)
                    n += len(chunk)
                    if self._progress:
                        self._progress(done_bytes + n, total_bytes, rel)
                fout.flush()
                os.fsync(fout.fileno())
            st2 = os.stat(path)
            if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns) or n != st.st_size:
                raise IOError('the file changed while it was being copied; trying again next run')
            if self.p['verify'] and sha256_file(tmp, self.stop) != h.hexdigest():
                raise IOError('the copy on the server does not match the original (SHA-256)')
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, d)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return h.hexdigest()

    def cleanup(self, ledger, dry_run, summary):
        """Delete files from this PC whose server copy is safe. Returns nothing; counts go into summary."""
        mode, keep_s = self.p['cleanup'], self.p['keep_days'] * 86400
        if mode not in ('compressed', 'uploaded'):
            return
        now = time.time()
        for rel, row in sorted(ledger.all().items()):
            if self.stop.is_set():
                raise Stopped()
            if row['deleted_at'] is not None or now - row['uploaded_at'] < keep_s:
                continue
            path = self.src / rel
            st = stat_or_none(path)
            if st is None or (st.st_size, st.st_mtime_ns) != (row['size'], row['mtime_ns']):
                continue  # gone or changed; a changed file is uploaded again first
            held = self.server_copy(rel, row['size'])
            if held is None:
                self.say(logging.WARNING, f'Keeping {rel}: not on the server anymore (moved or deleted there?)')
                summary['kept'] += 1
                continue
            if mode == 'compressed' and rel.lower().endswith(TIF_EXT) and held != 'compressed':
                summary['kept'] += 1  # waiting for the pipeline
                continue
            if dry_run:
                summary['delete'] += 1
                summary['delete_bytes'] += row['size']
                self.say(logging.INFO, f'Would delete from this PC: {rel}' + ('' if row['verified'] else ' (after comparing with the server)'))
                continue
            if not row['verified']:
                # Adopted, not uploaded by RigSync: compare contents before deleting the only local copy
                try:
                    a = row['sha256'] or sha256_file(path, self.stop)
                    b = sha256_file(self.dst / rel, self.stop) if held == 'plain' else sha256_file(str(self.dst / rel) + ZST, self.stop, zstd_compressed=True)
                except ImportError:
                    self.say(logging.ERROR, f'Keeping {rel}: comparing with a .zst needs the zstandard package (pip install zstandard)')
                    summary['errors'] += 1
                    continue
                if a != b:
                    self.say(logging.ERROR, f'Keeping {rel}: the server copy differs from this one (SHA-256)')
                    ledger.update(rel, sha256=a, note='server copy differs')
                    summary['errors'] += 1
                    continue
                ledger.update(rel, sha256=a, verified=1)
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                os.remove(path)
            except OSError as e:
                self.say(logging.ERROR, f'Could not delete {rel}: {e}')
                summary['errors'] += 1
                continue
            ledger.update(rel, deleted_at=time.time())
            summary['delete'] += 1
            summary['delete_bytes'] += row['size']
            self.say(logging.INFO, f'Deleted from this PC: {rel}')

    def run(self, dry_run=False):
        """Returns (result, summary). result: 'ok', 'errors', 'busy', 'stopped' or 'failed'."""
        started = time.time()
        summary = {k: 0 for k in ['copy', 'copy_bytes', 'adopt', 'done', 'wait', 'delete', 'delete_bytes', 'kept', 'errors']}
        problems = check_profile(self.p)
        if problems:
            for msg in problems:
                self.say(logging.ERROR, msg)
            return 'failed', summary
        lock = ProfileLock(self.name)
        if not lock.acquire():
            self.say(logging.WARNING, 'Another run of this profile is in progress; skipped')
            return 'busy', summary
        ledger = Ledger(self.name)
        result = 'ok'
        try:
            self.say(logging.INFO, f'{"Preview" if dry_run else "Run"} of "{self.name}": {self.src} -> {self.dst}')
            plan = self.plan(ledger)
            summary.update(adopt=len(plan['adopt']), done=len(plan['done']), wait=len(plan['wait']))
            for rel, path, st, held in plan['adopt']:
                if not dry_run:
                    ledger.record(rel, st.st_size, st.st_mtime_ns, None, f'found on server ({held})', verified=False)
            if plan['adopt']:
                self.say(logging.INFO, f'{len(plan["adopt"])} files already on the server ({"would be " if dry_run else ""}added to the ledger without copying)')
            if plan['wait']:
                self.say(logging.INFO, f'{len(plan["wait"])} files changed in the last {self.p["min_age_min"]} min; waiting until they are finished')
            total = sum(st.st_size for _, _, st, _ in plan['copy'])
            done_bytes = 0
            for rel, path, st, kind in plan['copy']:
                if dry_run:
                    self.say(logging.INFO, f'Would upload: {rel} ({fmt_bytes(st.st_size)}{", replacing a different file" if kind == "replace" else ""})')
                    summary['copy'] += 1
                    summary['copy_bytes'] += st.st_size
                    continue
                try:
                    digest = self.copy_file(rel, path, st, done_bytes, total)
                    ledger.record(rel, st.st_size, st.st_mtime_ns, digest, 'uploaded', verified=self.p['verify'])
                    summary['copy'] += 1
                    summary['copy_bytes'] += st.st_size
                    self.say(logging.INFO, f'Uploaded {rel} ({fmt_bytes(st.st_size)}){" - replaced a different file" if kind == "replace" else ""}')
                except Stopped:
                    raise
                except Exception as e:
                    summary['errors'] += 1
                    self.say(logging.ERROR, f'Upload failed: {rel}: {e}')
                done_bytes += st.st_size
            self.cleanup(ledger, dry_run, summary)
            result = 'errors' if summary['errors'] else 'ok'
        except Stopped:
            result = 'stopped'
            self.say(logging.WARNING, 'Stopped')
        except Exception as e:
            result = 'failed'
            summary['errors'] += 1
            self.say(logging.ERROR, f'Run failed: {type(e).__name__}: {e}')
        finally:
            self.say(logging.INFO, summary_text(summary, dry_run))
            ledger.add_run(started, dry_run, result, summary)
            ledger.close()
            lock.release()
        return result, summary

def summary_text(s, dry_run=False):
    verb = ('would upload', 'would delete') if dry_run else ('uploaded', 'deleted from this PC')
    parts = [f'{verb[0]} {s["copy"]} ({fmt_bytes(s["copy_bytes"])})', f'{s["done"]} already uploaded']
    if s['adopt']:
        parts.append(f'{s["adopt"]} found on the server')
    if s['wait']:
        parts.append(f'{s["wait"]} still being written')
    if s['delete'] or s['kept']:
        parts.append(f'{verb[1]} {s["delete"]} ({fmt_bytes(s["delete_bytes"])})')
    if s['kept']:
        parts.append(f'{s["kept"]} kept for now')
    if s['errors']:
        parts.append(f'{s["errors"]} errors')
    return 'Summary: ' + ', '.join(parts)

# ---- Windows Task Scheduler ----

def task_name(name):
    return f'{APP}\\{safe_name(name)}'

def launch_command(name):
    """Command the scheduled task runs: the exe, or pythonw + this script, without a window."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--run', name]
    py = Path(sys.executable)
    pyw = py.with_name('pythonw.exe')
    return [str(pyw if pyw.exists() else py), str(Path(__file__).resolve()), '--run', name]

def schtasks(*args):
    return subprocess.run(['schtasks', *args], capture_output=True, text=True, creationflags=NO_WINDOW)

def task_xml(name, times):
    """Task Scheduler definition: run daily at each time; if the PC was off or logged out then, run as soon as
    possible afterwards (StartWhenAvailable); no time limit (a large upload can take hours); never two at once."""
    from xml.sax.saxutils import escape
    cmd = launch_command(name)
    triggers = ''.join(f"""
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{t}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>""" for t in times)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(f'{APP}: upload profile "{name}"')}</Description></RegistrationInfo>
  <Triggers>{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(cmd[0])}</Command><Arguments>{escape(subprocess.list2cmdline(cmd[1:]))}</Arguments></Exec>
  </Actions>
</Task>
"""

def install_schedule(name, run_at):
    """Create or replace the profile's scheduled task, running daily at the times in run_at ('22:00' or '12:00, 22:00')."""
    import tempfile
    xml = task_xml(name, parse_times(run_at))
    fd, path = tempfile.mkstemp(suffix='.xml')
    try:
        with os.fdopen(fd, 'w', encoding='utf-16') as f:
            f.write(xml)
        r = schtasks('/Create', '/F', '/TN', task_name(name), '/XML', path)
    finally:
        os.remove(path)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())

def remove_schedule(name):
    r = schtasks('/Delete', '/F', '/TN', task_name(name))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())

def schedule_status(name):
    """Text describing the scheduled task, or None if there is none (or not on Windows)."""
    if os.name != 'nt':
        return None
    r = schtasks('/Query', '/TN', task_name(name), '/FO', 'LIST')
    if r.returncode != 0:
        return None
    info = dict(line.split(':', 1) for line in r.stdout.splitlines() if ':' in line)
    return f'Scheduled · next run {info.get("Next Run Time", "?").strip()} · {info.get("Status", "").strip()}'

# ---- Window ----

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f'{APP} {VERSION}')
            self.geometry('1000x700')
            self.minsize(820, 560)
            self.profiles = load_profiles()
            self.current = None
            self.messages = queue.Queue()
            self.worker, self.stop = None, threading.Event()
            self.v = {k: tk.StringVar() for k in ['name', 'source', 'dest', 'exclude', 'min_age_min', 'keep_days', 'run_at', 'cleanup']}
            self.v_verify = tk.BooleanVar(value=True)
            self.build()
            self.refresh_list()
            if self.profiles:
                self.select(sorted(self.profiles)[0])
            else:
                self.new_profile()
            self.after(100, self.poll)

        def build(self):
            style = ttk.Style(self)
            if 'vista' in style.theme_names():
                style.theme_use('vista')
            pad = {'padx': 6, 'pady': 4}
            outer = ttk.PanedWindow(self, orient='horizontal')
            outer.pack(fill='both', expand=True, padx=8, pady=8)

            left = ttk.Frame(outer)
            ttk.Label(left, text='Profiles', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
            self.listbox = tk.Listbox(left, exportselection=False, activestyle='none', width=24)
            self.listbox.pack(fill='both', expand=True, pady=4)
            self.listbox.bind('<<ListboxSelect>>', lambda e: self.on_list_select())
            row = ttk.Frame(left)
            row.pack(fill='x')
            ttk.Button(row, text='New', command=self.new_profile).pack(side='left')
            ttk.Button(row, text='Duplicate', command=self.duplicate_profile).pack(side='left', padx=4)
            ttk.Button(row, text='Delete', command=self.delete_profile).pack(side='left')
            outer.add(left, weight=1)

            right = ttk.Frame(outer)
            outer.add(right, weight=4)
            form = ttk.LabelFrame(right, text='Settings')
            form.pack(fill='x')
            form.columnconfigure(1, weight=1)

            def entry_row(r, label, key, browse=False, hint=None):
                ttk.Label(form, text=label).grid(row=r, column=0, sticky='w', **pad)
                ttk.Entry(form, textvariable=self.v[key]).grid(row=r, column=1, sticky='ew', **pad)
                if browse:
                    ttk.Button(form, text='Browse...', command=lambda: self.browse(key)).grid(row=r, column=2, **pad)
                if hint:
                    ttk.Label(form, text=hint, foreground='#666').grid(row=r, column=2, sticky='w', **pad)

            entry_row(0, 'Name', 'name')
            entry_row(1, 'Source (this PC)', 'source', browse=True)
            entry_row(2, 'Server folder', 'dest', browse=True)
            entry_row(3, 'Skip names', 'exclude', hint='patterns, separated by ;')

            opts = ttk.Frame(form)
            opts.grid(row=4, column=0, columnspan=3, sticky='ew', **pad)
            ttk.Label(opts, text='Upload files unchanged for').pack(side='left')
            ttk.Spinbox(opts, from_=0, to=1440, width=5, textvariable=self.v['min_age_min']).pack(side='left', padx=4)
            ttk.Label(opts, text='min').pack(side='left')
            ttk.Checkbutton(opts, text='Verify each upload (SHA-256)', variable=self.v_verify).pack(side='left', padx=20)

            clean = ttk.Frame(form)
            clean.grid(row=5, column=0, columnspan=3, sticky='ew', **pad)
            ttk.Label(clean, text='Cleanup').pack(side='left')
            self.cleanup_box = ttk.Combobox(clean, state='readonly', width=44, values=list(CLEANUP_MODES.values()))
            self.cleanup_box.pack(side='left', padx=4)
            ttk.Label(clean, text='keep on this PC for').pack(side='left', padx=(8, 0))
            ttk.Spinbox(clean, from_=0, to=365, width=5, textvariable=self.v['keep_days']).pack(side='left', padx=4)
            ttk.Label(clean, text='days after upload').pack(side='left')

            sched = ttk.Frame(form)
            sched.grid(row=6, column=0, columnspan=3, sticky='ew', **pad)
            ttk.Label(sched, text='Run daily at').pack(side='left')
            ttk.Entry(sched, width=14, textvariable=self.v['run_at']).pack(side='left', padx=4)
            ttk.Label(sched, text='e.g. 22:00 or 12:00, 22:00', foreground='#666').pack(side='left')
            ttk.Button(sched, text='Schedule', command=self.schedule).pack(side='left', padx=(12, 4))
            ttk.Button(sched, text='Unschedule', command=self.unschedule).pack(side='left')
            self.sched_label = ttk.Label(sched, foreground='#666')
            self.sched_label.pack(side='left', padx=10)

            ttk.Button(form, text='Save', command=self.save).grid(row=7, column=2, sticky='e', **pad)

            actions = ttk.Frame(right)
            actions.pack(fill='x', pady=(8, 4))
            self.btn_preview = ttk.Button(actions, text='Preview', command=lambda: self.start(dry_run=True))
            self.btn_preview.pack(side='left')
            self.btn_run = ttk.Button(actions, text='Run now', command=lambda: self.start(dry_run=False))
            self.btn_run.pack(side='left', padx=4)
            self.btn_stop = ttk.Button(actions, text='Stop', command=self.stop.set, state='disabled')
            self.btn_stop.pack(side='left')
            ttk.Button(actions, text='Open logs', command=self.open_logs).pack(side='right')
            self.stats_label = ttk.Label(right, foreground='#444')
            self.stats_label.pack(fill='x')
            self.progress = ttk.Progressbar(right, mode='determinate', maximum=1000)
            self.progress.pack(fill='x', pady=4)
            self.status = ttk.Label(right, text='')
            self.status.pack(fill='x')

            logf = ttk.Frame(right)
            logf.pack(fill='both', expand=True, pady=(4, 0))
            self.log = tk.Text(logf, height=12, wrap='none', font=('Consolas', 9), state='disabled')
            sb = ttk.Scrollbar(logf, command=self.log.yview)
            self.log.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            self.log.pack(side='left', fill='both', expand=True)
            self.log.tag_configure('ERROR', foreground='#c0392b')
            self.log.tag_configure('WARNING', foreground='#a15c00')

        # profile list and form
        def refresh_list(self):
            self.listbox.delete(0, 'end')
            for name in sorted(self.profiles):
                self.listbox.insert('end', name)

        def on_list_select(self):
            sel = self.listbox.curselection()
            if sel:
                self.select(self.listbox.get(sel[0]))

        def select(self, name):
            self.current = name
            p = self.profiles.get(name, DEFAULT_PROFILE)
            self.v['name'].set(name)
            for k in ['source', 'dest', 'min_age_min', 'keep_days', 'run_at']:
                self.v[k].set(f'{p[k]:g}' if isinstance(p[k], (int, float)) else str(p[k]))
            self.v['exclude'].set('; '.join(p['exclude']))
            self.v_verify.set(bool(p['verify']))
            self.cleanup_box.set(CLEANUP_MODES[p['cleanup']])
            names = sorted(self.profiles)
            if name in names:
                self.listbox.selection_clear(0, 'end')
                self.listbox.selection_set(names.index(name))
            self.update_info()

        def form_profile(self):
            try:
                p = {'source': self.v['source'].get().strip(), 'dest': self.v['dest'].get().strip(),
                     'exclude': [x.strip() for x in self.v['exclude'].get().split(';') if x.strip()],
                     'min_age_min': float(self.v['min_age_min'].get()), 'verify': self.v_verify.get(),
                     'cleanup': {v: k for k, v in CLEANUP_MODES.items()}[self.cleanup_box.get()],
                     'keep_days': float(self.v['keep_days'].get()), 'run_at': ', '.join(parse_times(self.v['run_at'].get()))}
            except (ValueError, KeyError) as e:
                raise ValueError(f'Check the form: {e}')
            return {**DEFAULT_PROFILE, **p}

        def save(self, quiet=False):
            name = self.v['name'].get().strip()
            if not name:
                messagebox.showerror(APP, 'The profile needs a name.')
                return False
            try:
                p = self.form_profile()
            except ValueError as e:
                messagebox.showerror(APP, str(e))
                return False
            if p['cleanup'] != 'never' and p['keep_days'] < 1 and not quiet:
                if not messagebox.askyesno(APP, 'Files will be deleted from this PC right after they are uploaded. Continue?'):
                    return False
            if self.current != name and name in self.profiles:
                messagebox.showerror(APP, f'A profile named "{name}" already exists.')
                return False
            if self.current and self.current != name and self.current in self.profiles:
                del self.profiles[self.current]  # renamed; the old ledger stays under the old name
            self.profiles[name] = p
            save_profiles(self.profiles)
            self.current = name
            self.refresh_list()
            self.select(name)
            return True

        def new_profile(self):
            base, i = 'New profile', 1
            name = base
            while name in self.profiles:
                i += 1
                name = f'{base} {i}'
            self.current = None
            self.select(name)
            self.current = None

        def duplicate_profile(self):
            if not self.current:
                return
            name = self.current + ' copy'
            self.profiles[name] = dict(self.profiles[self.current])
            save_profiles(self.profiles)
            self.refresh_list()
            self.select(name)

        def delete_profile(self):
            if not self.current or self.current not in self.profiles:
                return
            if not messagebox.askyesno(APP, f'Delete the profile "{self.current}"? Its scheduled task is removed too. '
                                            'Files are not touched, and its ledger is kept in case you recreate it.'):
                return
            if schedule_status(self.current):
                try:
                    remove_schedule(self.current)
                except RuntimeError as e:
                    messagebox.showwarning(APP, f'Could not remove the scheduled task: {e}')
            del self.profiles[self.current]
            save_profiles(self.profiles)
            self.refresh_list()
            self.current = None
            if self.profiles:
                self.select(sorted(self.profiles)[0])
            else:
                self.new_profile()

        def browse(self, key):
            d = filedialog.askdirectory(initialdir=self.v[key].get() or None)
            if d:
                self.v[key].set(os.path.normpath(d))

        def update_info(self):
            name = self.current
            if not name or name not in self.profiles:
                self.stats_label.config(text='Not saved yet')
                self.sched_label.config(text='')
                return
            ledger = Ledger(name)
            try:
                s, last = ledger.stats()
            finally:
                ledger.close()
            text = f'Ledger: {s["n"]:,} files ({fmt_bytes(s["bytes"])}) uploaded or found on the server · {s["n_deleted"]:,} deleted from this PC ({fmt_bytes(s["bytes_deleted"])})'
            if last:
                text += f' · last run {time.strftime("%Y-%m-%d %H:%M", time.localtime(last["started"]))}: {last["result"]}'
            self.stats_label.config(text=text)
            self.sched_label.config(text=schedule_status(name) or ('Not scheduled' if os.name == 'nt' else 'Scheduling needs Windows'))

        # scheduling
        def schedule(self):
            if not self.save(quiet=True):
                return
            try:
                install_schedule(self.current, self.profiles[self.current]['run_at'])
            except (RuntimeError, OSError, ValueError) as e:
                messagebox.showerror(APP, f'Could not create the scheduled task:\n{e}')
            self.update_info()

        def unschedule(self):
            if not self.current:
                return
            try:
                remove_schedule(self.current)
            except (RuntimeError, OSError) as e:
                messagebox.showerror(APP, f'Could not remove the scheduled task:\n{e}')
            self.update_info()

        def open_logs(self):
            d = home() / 'logs'
            d.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt':
                os.startfile(d)
            else:
                messagebox.showinfo(APP, str(d))

        # runs
        def start(self, dry_run):
            if self.worker and self.worker.is_alive():
                return
            if not self.save(quiet=dry_run):
                return
            name, profile = self.current, self.profiles[self.current]
            problems = check_profile(profile)
            if problems:
                messagebox.showerror(APP, '\n'.join(problems))
                return
            self.stop.clear()
            self.log_clear()
            self.progress['value'] = 0
            for b in (self.btn_preview, self.btn_run):
                b.config(state='disabled')
            self.btn_stop.config(state='normal')
            self.status.config(text='Previewing…' if dry_run else 'Running…')
            say = lambda level, msg: self.messages.put(('log', level, msg))
            progress = lambda done, total, rel: self.messages.put(('progress', done, total, rel))
            syncer = Syncer(name, profile, say=say, progress=progress, stop=self.stop)

            def work():
                result, summary = syncer.run(dry_run=dry_run)
                self.messages.put(('done', result, summary, dry_run))
            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def poll(self):
            n = 0
            try:
                while n < 500:
                    m = self.messages.get_nowait()
                    n += 1
                    if m[0] == 'log':
                        self.log_add(m[1], m[2])
                    elif m[0] == 'progress':
                        _, done, total, rel = m
                        self.progress['value'] = 1000 * done / total if total else 0
                        self.status.config(text=f'Uploading {rel} · {fmt_bytes(done)} of {fmt_bytes(total)}')
                    elif m[0] == 'done':
                        _, result, summary, dry_run = m
                        for b in (self.btn_preview, self.btn_run):
                            b.config(state='normal')
                        self.btn_stop.config(state='disabled')
                        self.progress['value'] = 1000 if result == 'ok' else self.progress['value']
                        label = {'ok': 'Done', 'errors': 'Finished with errors', 'busy': 'Skipped: already running',
                                 'stopped': 'Stopped', 'failed': 'Failed'}[result]
                        self.status.config(text=f'{"Preview: " if dry_run else ""}{label}. {summary_text(summary, dry_run)}')
                        self.update_info()
            except queue.Empty:
                pass
            self.after(100, self.poll)

        def log_clear(self):
            self.log.config(state='normal')
            self.log.delete('1.0', 'end')
            self.log.config(state='disabled')

        def log_add(self, level, msg):
            self.log.config(state='normal')
            self.log.insert('end', time.strftime('%H:%M:%S ') + msg + '\n', logging.getLevelName(level))
            if int(self.log.index('end-1c').split('.')[0]) > 5000:
                self.log.delete('1.0', '1000.0')
            self.log.see('end')
            self.log.config(state='disabled')

    App().mainloop()

# ---- Command line ----

def main(argv=None):
    parser = argparse.ArgumentParser(description=f'{APP}: one-way upload that uploads each file only once.')
    parser.add_argument('--run', metavar='PROFILE', help='run a profile without a window')
    parser.add_argument('--dry-run', action='store_true', help='with --run: only list what would happen')
    parser.add_argument('--list', action='store_true', help='list profiles')
    parser.add_argument('--schedule', metavar='PROFILE', help="create the profile's scheduled task (daily at its run_at times)")
    parser.add_argument('--unschedule', metavar='PROFILE', help="remove the profile's scheduled task")
    args = parser.parse_args(argv)
    if args.list:
        for name, p in sorted(load_profiles().items()):
            print(f'{name}: {p["source"]} -> {p["dest"]} (cleanup: {p["cleanup"]}, daily at {p["run_at"]})')
        return 0
    if args.schedule or args.unschedule:
        name = args.schedule or args.unschedule
        profiles = load_profiles()
        try:
            if args.schedule:
                if name not in profiles:
                    raise ValueError(f'No profile named "{name}"')
                install_schedule(name, profiles[name]['run_at'])
            else:
                remove_schedule(name)
        except (RuntimeError, ValueError, OSError) as e:
            get_logger(name).error(f'{"Scheduling" if args.schedule else "Unscheduling"} failed: {e}')
            if sys.stderr:
                print(e, file=sys.stderr)
            return 1
        get_logger(name).info(f'Scheduled daily at {profiles[name]["run_at"]}' if args.schedule else 'Schedule removed')
        return 0
    if args.run:
        profiles = load_profiles()
        if args.run not in profiles:
            get_logger(args.run).error(f'No profile named "{args.run}"')
            print(f'No profile named "{args.run}"', file=sys.stderr)
            return 2
        say = (lambda level, msg: print(msg)) if sys.stdout else None  # no console in the windowed exe
        result, _ = Syncer(args.run, profiles[args.run], say=say).run(dry_run=args.dry_run)
        return 0 if result in ('ok', 'busy') else 1
    run_gui()
    return 0

if __name__ == '__main__':
    sys.exit(main())

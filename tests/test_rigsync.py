"""
Tests of RigSync's sync logic on temporary folders standing in for the rig PC and the server.
Run: python tests/test_rigsync.py   (needs: pip install zstandard)
"""
import os, sys, time, shutil, logging, tempfile, importlib.util
from pathlib import Path
import zstandard

base = Path(tempfile.mkdtemp(prefix='rigsync_test_'))
os.environ['RIGSYNC_HOME'] = str(base / 'home')
spec = importlib.util.spec_from_file_location('rigsync', Path(__file__).resolve().parent.parent / 'rigsync.py')
rs = importlib.util.module_from_spec(spec); spec.loader.exec_module(rs)

def compress_file(tif):
    """What the server's pipeline does after processing (mouse_imaging compress.py): X.tif -> X.tif.zst, whole file,
    original size in the header, modification time kept, original deleted."""
    tif = Path(tif)
    st = tif.stat()
    out = tif.with_name(tif.name + '.zst')
    with open(tif, 'rb') as fin, open(out, 'wb') as fout:
        with zstandard.ZstdCompressor(level=6, write_checksum=True).stream_writer(fout, size=st.st_size, closefd=False) as w:
            w.write(fin.read())
    os.utime(out, (st.st_atime, st.st_mtime))
    tif.unlink()

OLD = time.time() - 3600
def write(p, data, mtime=OLD):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); os.utime(p, (mtime, mtime))
def files(d):
    return sorted(p.relative_to(d).as_posix() for p in d.rglob('*') if p.is_file())
def check(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg)
    if not cond: check.failed = True
check.failed = False

# zstd header parser against the zstandard package
for n in [0, 1, 255, 256, 257, 300, 65791, 65792, 70000, 1_000_000]:
    p = base / f'z{n}.zst'
    p.write_bytes(zstandard.ZstdCompressor(level=3).compress(os.urandom(n)))
    check(rs.zst_content_size(p) == zstandard.get_frame_parameters(p.read_bytes()).content_size == n, f'zst header size {n}')
check(rs.zst_content_size(base / 'missing.zst') is None, 'zst header: missing file -> None')

rig, srv = base / 'rig', base / 'server'
srv.mkdir()
S = 'JG6/261020/session_1'
for i in range(1, 6):
    write(rig / S / f'920nm_G1R1_V1_L123_00001_{i:05d}.tif', os.urandom(300_000 + i))
write(rig / S / 'filter2' / '850nm_G1B2_V1_L123_00001_00001.tif', os.urandom(100_000))
write(rig / S / '.DS_Store', b'junk')
prof = {**rs.DEFAULT_PROFILE, 'source': str(rig), 'dest': str(srv), 'min_age_min': 5}
logs = []
say = lambda level, msg: logs.append(msg)

# 1. preview changes nothing
res, s = rs.Syncer('test', prof, say=say).run(dry_run=True)
check(res == 'ok' and s['copy'] == 6 and files(srv) == [], 'preview: 6 to upload, server untouched')
# 2. run uploads, skips .DS_Store, keeps mtimes, no temp files
res, s = rs.Syncer('test', prof, say=say).run()
check(res == 'ok' and s['copy'] == 6, 'run: uploaded 6')
check(files(srv) == [f for f in files(rig) if not f.endswith('.DS_Store')], 'server has the same files minus .DS_Store, no temp files')
f0 = f'{S}/920nm_G1R1_V1_L123_00001_00001.tif'
check(abs(os.stat(srv / f0).st_mtime - OLD) < 1 and (srv / f0).read_bytes() == (rig / f0).read_bytes(), 'content and mtime kept')
# 3. second run: nothing to do
res, s = rs.Syncer('test', prof, say=say).run()
check(s['copy'] == 0 and s['done'] == 6, 'rerun: nothing uploaded')
# 4. server compresses (pipeline compress step): still nothing uploaded
for p in list((srv / S).rglob('*.tif')):
    compress_file(p)
res, s = rs.Syncer('test', prof, say=say).run()
check(s['copy'] == 0 and not list(srv.rglob('*.tif')), 'after server compression: originals not uploaded again')
# 5. server deletes a file: not uploaded again either
(srv / (f0 + '.zst')).unlink()
res, s = rs.Syncer('test', prof, say=say).run()
check(s['copy'] == 0, 'file deleted on server: not uploaded again')
# 6. a file still being written waits, then uploads
new = rig / S / '920nm_G1R1_V1_L123_00001_00006.tif'
write(new, os.urandom(1000), mtime=time.time())
res, s = rs.Syncer('test', prof, say=say).run()
check(s['wait'] == 1 and s['copy'] == 0, 'recent file waits')
os.utime(new, (OLD, OLD))
res, s = rs.Syncer('test', prof, say=say).run()
check(s['copy'] == 1, 'then uploads once old enough')
# 7. a file changed on the rig after upload is uploaded again (replace)
write(rig / S / '920nm_G1R1_V1_L123_00001_00002.tif', os.urandom(5000), mtime=OLD + 10)
res, s = rs.Syncer('test', prof, say=say).run()
check(s['copy'] == 1 and (srv / S / '920nm_G1R1_V1_L123_00001_00002.tif').stat().st_size == 5000, 'changed file uploaded again')

# 8. adoption: a second rig whose files the server already has (Create Synchronicity uploads)
rig2 = base / 'rig2'
S2 = 'JG8/261021/session_1'
data = {i: os.urandom(200_000 + i) for i in range(1, 5)}
for i, d in data.items():
    write(rig2 / S2 / f'a_{i:05d}.tif', d)
    write(srv / S2 / f'a_{i:05d}.tif', d)
compress_file(srv / S2 / 'a_00001.tif')
compress_file(srv / S2 / 'a_00002.tif')
write(srv / S2 / 'a_00004.tif', os.urandom(len(data[4])))  # same size, different content
compress_file(srv / S2 / 'a_00004.tif')
write(rig2 / S2 / 'notes.txt', b'hello')                      # not on the server yet
prof2 = {**prof, 'source': str(rig2), 'cleanup': 'compressed', 'keep_days': 0}
res, s = rs.Syncer('test2', prof2, say=say).run(dry_run=True)
check(s['adopt'] == 4 and s['copy'] == 1, 'adoption preview: 4 found on server, 1 to upload')
logs.clear()
res, s = rs.Syncer('test2', prof2, say=say).run()
check(s['adopt'] == 4 and s['copy'] == 1, 'adoption run: no re-upload of the 4')
# cleanup 'compressed', keep_days 0: a_1, a_2 (compressed, verified by decompressing) deleted; a_3 (plain on server) kept;
# a_4 (compressed but different content) kept with an error; notes.txt (not a TIFF, uploaded) deleted
left = files(rig2)
check(left == [f'{S2}/a_00003.tif', f'{S2}/a_00004.tif'], f'cleanup compressed mode leaves a_3 and a_4: {left}')
check(any('differs' in m for m in logs) and res == 'errors', 'mismatching server copy reported and kept')
check(len(files(srv / S2)) == 5, 'nothing deleted on the server')
# 9. a rerun doesn't re-upload deleted files
res, s = rs.Syncer('test2', prof2, say=say).run()
check(s['copy'] == 0 and s['delete'] == 0, 'rerun after cleanup: nothing to do')
# 10. 'uploaded' mode deletes the plain-on-server one after verifying
prof3 = {**prof2, 'cleanup': 'uploaded'}
res, s = rs.Syncer('test2', prof3, say=say).run()
check(files(rig2) == [f'{S2}/a_00004.tif'], 'uploaded mode deletes a_3, still keeps the mismatching a_4')
# 11. keep_days respected
rig3 = base / 'rig3'; write(rig3 / 'x/y.tif', b'1234567')
prof4 = {**prof, 'source': str(rig3), 'cleanup': 'uploaded', 'keep_days': 7}
rs.Syncer('test4', prof4, say=say).run()
check(files(rig3) == ['x/y.tif'], 'keep_days: kept right after upload')
# 12. server unreachable: fails without touching anything
res, s = rs.Syncer('test', {**prof, 'dest': str(base / 'nope')}, say=say).run()
check(res == 'failed' and not (base / 'nope').exists(), 'server unreachable: run fails, nothing created')
# 13. lock
lock = rs.ProfileLock('test'); lock.acquire()
res, s = rs.Syncer('test', prof, say=say).run()
lock.release()
check(res == 'busy', 'concurrent run skipped')
# 14. stop during copy leaves no temp file
big = rig / S / '920nm_G1R1_V1_L123_00001_00007.tif'; write(big, os.urandom(40 * 1024 * 1024))
sy = rs.Syncer('test', prof, say=say)
sy._progress = lambda d, t, r: sy.stop.set()
res, s = sy.run()
check(res == 'stopped' and not list(srv.rglob('*' + rs.TMP_SUFFIX)) and not (srv / S / big.name).exists(), 'stop: no partial file on server')
# 15. CLI
os.environ['RIGSYNC_HOME'] = str(base / 'home')
rs.save_profiles({'cli': prof})
check(rs.main(['--run', 'cli', '--dry-run']) == 0, 'CLI dry run')
print('\nALL PASSED' if not check.failed else '\nSOME FAILED')
logging.shutdown()
shutil.rmtree(base, ignore_errors=True)
sys.exit(1 if check.failed else 0)

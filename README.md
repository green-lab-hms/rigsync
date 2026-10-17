# RigSync

RigSync uploads data from a rig PC to the lab server. It replaces Create Synchronicity for the Green lab's imaging pipeline ([mouse_imaging](https://github.com/green-lab-hms/mouse_imaging)).

It uploads each file **once**. After processing, the pipeline compresses raw TIFFs on the server (`X.tif` becomes `X.tif.zst`). Create Synchronicity then sees `X.tif` "missing" on the server and uploads it again, which put 1.9 TB of duplicates back on the server in October 2026. RigSync keeps a record (the *ledger*) of every file it uploaded. Anything the server later compresses, moves or deletes is never uploaded again.

It can also delete old data from the rig PC once the server has a safe copy.

## What it does on each run

1. **Uploads new files.** A file is uploaded once it hasn't changed for 5 minutes (setting: *Upload files unchanged for*). That way, it never copies a file ScanImage is still writing.
2. **Avoids partial files on the server.** Each file is written under a temporary name (`.rigsync-tmp`), checked against the original (SHA-256), then renamed. The pipeline never sees a half-copied file. The file's original date is kept.
3. **Skips files already on the server.** On the first run, files the server already has are added to the ledger without copying. That covers files with the same size, and compressed `.tif.zst` copies whose recorded original size matches. Switching from Create Synchronicity doesn't upload anything a second time.
4. **Optionally deletes old files from this PC.** See *Cleanup* below.

RigSync **never deletes anything on the server**. If the server folder can't be reached (e.g. the network drive isn't connected), the run stops without doing anything.

## Install

1. On the rig PC, open this repository's **Releases** page on GitHub (right-hand side of the repository's main page) and download `RigSync.exe` from the latest release. No GitHub account is needed if the repository is public.
2. Move it to a permanent place, e.g. `C:\RigSync\RigSync.exe`. The scheduled task runs the exe from where it is when you click **Schedule**. If you move it later, click **Schedule** again.
3. Double-click it. Windows may show a SmartScreen warning the first time, because the exe isn't signed: **More info** → **Run anyway**. No Python is needed.

To update, download the new release, replace the exe at the same path and start it once. Profiles, ledgers and schedules are kept.

Builds of every commit are also on the **Actions** tab: open a **Build RigSync** run, then **Artifacts** → `RigSync` (a zip; downloading needs a GitHub login).

If you'd rather run the script: install Python 3.9+ and `pip install zstandard`, then run `pythonw rigsync.py`.

## Set up a profile

A profile is one source folder on this PC and one folder on the server. Click **New** and fill in:

| Setting | Two-photon rig | ViRMEn PC | Sync (WinEDR) |
|---|---|---|---|
| Source (this PC) | the folder that contains `<mouse>\<date>\session_N\` | the ViRMEn data folder (same layout) | the folder that contains `<mouse>\<date>\session_00N.EDR` |
| Server folder | `...\data\raw\twophoton` | `...\data\raw\virmen` | `...\data\raw\sync` |
| Cleanup | *Delete once processed (compressed) on the server* | *Never* or *Delete once uploaded* | *Never* or *Delete once uploaded* |

The folder layout under the source has to be what the pipeline expects. ScanImage must save into `<mouse>\<date>\session_N\`, not directly into the date folder. The pipeline dashboard warns about TIFFs outside a session folder.

Then:
1. **Save**.
2. **Preview**. This lists what would be uploaded and deleted without changing anything. On the first run, check that the files Create Synchronicity already uploaded show up as "found on the server", not "Would upload".
3. **Run now** for the first upload. You can stop it at any time; the next run continues.
4. **Schedule**. Set *Run daily at* to a time when the rig isn't recording, e.g. `22:00`, or several times, e.g. `12:00, 22:00`. This creates a Windows scheduled task (`RigSync\<profile>` in Task Scheduler) that runs the profile at those times without opening a window.
   - It runs only while someone is logged in to the PC.
   - If the PC was off or logged out at that time, the run happens as soon as someone logs in.
   - A run has no time limit, so a large first upload can take all night.
   - Files still being written when a run starts are uploaded at the next run.
   - Sessions reach the pipeline after the next scheduled run, not right after recording. Use **Run now** to send one right away.

Then **turn off Create Synchronicity's scheduled sync** for the same folders, so the two don't both upload.

## Cleanup

| Mode | A file is deleted from this PC when... |
|---|---|
| Never delete from this PC | never (default) |
| Delete once processed (compressed) on the server | the pipeline has processed and compressed it (`X.tif.zst` exists) and *keep on this PC for* days have passed since upload. Files other than TIFFs only need the waiting time. |
| Delete once uploaded | *keep on this PC for* days have passed since upload |

In every mode, the server must still hold the file. For files RigSync found on the server rather than uploading itself, it first reads the server's copy and compares it byte for byte (SHA-256). If anything doesn't match, the file is kept and the log says why.

Empty folders are left in place.

## Logs and settings

**Open logs** shows the log folder. Settings, ledgers and logs are in `%APPDATA%\RigSync`:
- `profiles.json`: the profiles
- `ledgers\<profile>.sqlite`: what was uploaded, found or deleted
- `logs\<profile>.log`: one line per upload or deletion, and a summary per run

Command line, e.g. to check something from a terminal:

```
RigSync.exe --list
RigSync.exe --run "2P rig" --dry-run
RigSync.exe --schedule "2P rig"      # same as the Schedule button
RigSync.exe --unschedule "2P rig"
```

## Development

`rigsync.py` is a single file: standard library plus tkinter, and `zstandard` to read the server's `.tif.zst` files.

```
pip install zstandard
python tests/test_rigsync.py        # sync logic on temporary folders; prints PASS/FAIL per check
python rigsync.py                   # the window
```

Every push to GitHub runs `.github/workflows/build.yml` on Windows. It runs the tests, builds `RigSync.exe` with PyInstaller, uploads a test file with it, and creates and removes a scheduled task. To publish a release, tag a version:

```
git tag v1.0
git push origin v1.0
```

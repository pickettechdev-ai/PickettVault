# PickettVault

**USB project sync for makers — by PICKETTECH**
*Learn . Build . Innovate*

PickettVault keeps project folders on your Windows PC in sync with a dedicated USB stick. It keeps a snapshot of every file it replaces and writes a version-numbered changelog each time you sync. It can also mirror the whole stick to a backup drive.

Current version: **v1.3**

## Features

- **Two-way sync** between PC folders and a USB stick labelled `PICKETVAULT`
- **Preview before syncing:** see what's new, changed or deleted on each side
- **Conflict detection** when a file changed in both places (you choose which to keep)
- **Snapshots:** every overwritten or deleted file is kept, so nothing is ever lost
- **Changelog + versioning:** a note and a patch/minor/major version bump on every sync, written to `CHANGELOG.md`
- **Multi-PC:** each computer remembers its own folder locations
- **Mirror:** a one-way incremental backup of the whole vault to a second drive (works well with Google Drive for desktop for an off-site copy)
- Dark theme and built-in help

## Privacy

PickettVault has **no network code**. It never uploads, downloads or shares anything. Your files only ever move between your own PC, your own USB stick, and a backup folder you choose.

## Requirements

- Windows 10 or 11
- Python 3.8+ from [python.org](https://www.python.org/downloads/). Tkinter is included. No other packages are needed.

## Quick start

1. Rename your USB stick's label to `PICKETVAULT`.
2. Run `python pickettvault.py`.
3. Click **+ Add project**, name it and pick its folder.
4. Click **Scan**, review the list, then click **Sync**.

Press **F1** or click **Help** in the app for the full guide.

## Build a standalone .exe (optional)

```
pip install pyinstaller
pyinstaller --onefile --windowed --icon=pickettvault.ico --name PickettVault pickettvault.py
```

The app appears in `dist\PickettVault.exe`.

## License

Code: MIT, see [LICENSE](LICENSE).
The PICKETTECH name and logo are trademarks of PICKETTECH and are not covered by the MIT license.

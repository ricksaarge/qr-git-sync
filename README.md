# QR Git Sync

Transfer git repo changes between air-gapped machines using QR codes and a webcam.

**Sender** displays git bundle data as cycling QR codes on screen.
**Receiver** reads them via webcam, reconstructs the bundle, and applies it.

## Prerequisites

- **Python 3.6+**
- **Git** on PATH (`git --version` should work from terminal/command prompt)
- **Webcam** (for the receiving machine)

### Windows notes

- Install [Git for Windows](https://gitforwindows.org/) — make sure "Git from the command line" is selected during install
- Use `py` or `python` (not `python3`) in commands below
- If camera doesn't open, try `--camera 1` or close other apps using the camera

## Setup (both machines)

```bash
cd qr-git-sync
pip install -r requirements.txt
```

## Usage

### Check sync state
```bash
python qr_git_sync.py status
```

### Send changes (machine with new commits)
```bash
python qr_git_sync.py send --repo /path/to/repo
```
- Displays a carousel of QR codes. Point the other machine's camera at the screen.
- Uses `qr-sync-*` tags for incremental transfer (only new commits since last sync).
- `--full` to ignore tags and send the entire repo.
- `--chunk-size 400` for unreliable cameras (smaller QR = easier to read).

### Receive changes (other machine)
```bash
python qr_git_sync.py receive --repo /path/to/repo
```
- Opens webcam and watches for QR codes.
- Shows progress bar overlay on the camera feed.
- Automatically applies the bundle when all chunks are received.

## How it works

1. `git bundle create` packages commits into a single binary
2. Bundle is compressed (zlib) and base64-encoded
3. Payload is split into ~600-byte chunks, each becomes a QR code
4. Sender cycles through QR codes in a loop
5. Receiver collects chunks via webcam (order doesn't matter, duplicates ignored)
6. On completion: verify SHA256, decompress, apply bundle
7. Both machines get a `qr-sync-<timestamp>` tag for incremental sync next time

## Controls

| Key | Send mode | Receive mode |
|-----|-----------|--------------|
| `SPACE` | Pause/resume | — |
| `+` / `=` | Faster cycling | — |
| `-` | Slower cycling | — |
| `n` / `→` | Next QR | — |
| `p` / `←` | Previous QR | — |
| `q` / `ESC` | Quit | Quit |

## Bidirectional sync

Sync is one-direction per session. For two-way:
1. Machine A sends → Machine B receives
2. B resolves any merge conflicts
3. Machine B sends → Machine A receives

The machine that receives first acts as the merge authority.

## Limits

- ~600 bytes per QR code at default settings
- A 100KB bundle = ~170 QR codes ≈ 85 seconds per pass
- A 1MB bundle = ~1,700 QR codes ≈ 15 minutes per pass
- For large transfers, consider sneakernet (USB drive)

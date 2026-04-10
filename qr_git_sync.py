#!/usr/bin/env python3
"""
QR Git Sync — Transfer git repo changes between air-gapped machines via QR codes.

Sender displays QR codes on screen in a cycling carousel.
Receiver reads them via webcam, reconstructs the git bundle, and applies it.

Usage:
    python qr_git_sync.py send [--repo .] [--chunk-size 600] [--full]
    python qr_git_sync.py receive [--repo .] [--camera 0]
    python qr_git_sync.py status [--repo .]

Install:
    pip install qrcode[pil] opencv-python numpy

Controls (send):
    SPACE     pause / resume cycling
    +/=       faster cycling
    -         slower cycling
    n         next QR manually
    p         previous QR manually
    t         tag sync point (marks commits as sent)
    q / ESC   quit (without tagging)

Controls (receive):
    q / ESC   quit (saves partial progress for resume)
    r         retry / reset session

Platform notes:
    macOS     Grant camera access to Terminal/iTerm in System Settings > Privacy
    Windows   Requires Git for Windows on PATH. Camera may take a few seconds to open.
    Linux     Requires libgl1-mesa-glx (or equivalent) for OpenCV GUI support.
"""

import argparse
import atexit
import base64
import collections
import hashlib
import json
import os
import random
import string
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

try:
    import cv2
    import numpy as np
    import qrcode
    from PIL import Image
    # Pillow 9.1+ moved constants to Image.Resampling
    _NEAREST = getattr(Image, "Resampling", Image).NEAREST
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install qrcode[pil] opencv-python numpy")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 1
DEFAULT_CHUNK_SIZE = 600        # base64 chars per QR data payload
DEFAULT_DISPLAY_MS = 500        # ms per QR frame in carousel
QR_RENDER_SIZE = 580            # px for the QR image itself
WINDOW_W, WINDOW_H = 800, 720  # display window size
SYNC_TAG_PREFIX = "qr-sync-"
EC_LEVEL = qrcode.constants.ERROR_CORRECT_M  # 15% recovery
MAX_PRERENDER_CHUNKS = 5000     # above this, render on-the-fly
LAZY_CACHE_SIZE = 200           # LRU cache slots in lazy render mode
CAMERA_FAIL_LIMIT = 100         # consecutive read failures before bail
STALL_WARN_SECS = 30            # warn if no new chunks for this long
RESUME_DIR = Path(tempfile.gettempdir()) / "qr-git-sync"


# ---------------------------------------------------------------------------
# Cleanup — uses atexit so KeyboardInterrupt propagates normally
# ---------------------------------------------------------------------------

_cleanup_fns = []


def _register_cleanup(fn):
    """Register a cleanup function. Runs on normal exit, KeyboardInterrupt, or SystemExit."""
    _cleanup_fns.append(fn)


def _run_cleanup():
    for fn in _cleanup_fns:
        try:
            fn()
        except Exception:
            pass


atexit.register(_run_cleanup)


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _session_id():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _pkt(**fields):
    """Compact JSON packet."""
    return json.dumps(fields, separators=(",", ":"))


def pkt_meta(sid, n_chunks, n_bytes, sha16, branch):
    return _pkt(v=PROTOCOL_VERSION, t="meta", s=sid,
                n=n_chunks, sz=n_bytes, sha=sha16, ref=branch)


def pkt_data(sid, idx, total, b64, crc):
    return _pkt(t="d", s=sid, i=idx, n=total, c=crc, d=b64)


def pkt_end(sid):
    return _pkt(t="end", s=sid)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


def _git(args, cwd, check=True):
    # On Windows, use shell=False but ensure git is found via PATH.
    # creationflags prevents a console window flash on Windows.
    kwargs = {}
    if _IS_WINDOWS and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    r = subprocess.run(
        ["git"] + args, cwd=cwd,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", **kwargs,
    )
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r


def repo_root(path="."):
    return _git(["rev-parse", "--show-toplevel"], path).stdout.strip()


def current_branch(root):
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()


def is_empty_repo(root):
    """True if the repo has no commits yet."""
    r = _git(["rev-parse", "HEAD"], root, check=False)
    return r.returncode != 0


def list_branches(root):
    """Return list of local branch names."""
    r = _git(["branch", "--format=%(refname:short)"], root, check=False)
    return [b.strip() for b in r.stdout.splitlines() if b.strip()]


def last_sync_tag(root):
    r = _git(["tag", "-l", f"{SYNC_TAG_PREFIX}*", "--sort=-creatordate"],
             root, check=False)
    tags = [t for t in r.stdout.strip().splitlines() if t]
    return tags[0] if tags else None


def set_sync_tag(root):
    # Use milliseconds + random suffix to avoid collisions within same second
    ts = int(time.time() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase, k=3))
    tag = f"{SYNC_TAG_PREFIX}{ts}-{suffix}"
    _git(["tag", tag], root)
    return tag


def create_bundle(root, since=None):
    """Return bundle bytes.  Falls back to --all if incremental fails."""
    if is_empty_repo(root):
        raise RuntimeError("Cannot create bundle from empty repo (no commits).")

    fd, path = tempfile.mkstemp(suffix=".bundle")
    os.close(fd)
    try:
        if since:
            r = _git(["bundle", "create", path, f"{since}..HEAD"],
                      root, check=False)
            if r.returncode != 0:
                print(f"  Incremental bundle failed ({since}). Falling back to full.")
                since = None
        if not since:
            _git(["bundle", "create", path, "--all"], root)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def apply_bundle(root, data):
    """Write bundle to temp file, verify, and apply.

    Handles both empty repos (initial clone) and existing repos (fetch+merge).
    """
    fd, path = tempfile.mkstemp(suffix=".bundle")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(data)

        empty = is_empty_repo(root)

        if not empty:
            r = _git(["bundle", "verify", path], root, check=False)
            if r.returncode != 0:
                print(f"  Verify warning: {r.stderr.strip()}")

        if empty:
            # Empty repo: fetch refs into a temp namespace, then create local branches
            _git(["fetch", path, "+refs/heads/*:refs/remotes/bundle/*"], root)
            # List fetched branches
            r = _git(["branch", "-r", "--format=%(refname:short)"], root, check=False)
            remote_branches = [b.strip().replace("bundle/", "")
                               for b in r.stdout.splitlines()
                               if b.strip().startswith("bundle/")]
            target = "main" if "main" in remote_branches else (
                remote_branches[0] if remote_branches else "main")
            # Create local branch from the fetched ref and checkout
            _git(["checkout", "-b", target, f"bundle/{target}"], root, check=False)
            # Clean up remote refs
            for rb in remote_branches:
                _git(["update-ref", "-d", f"refs/remotes/bundle/{rb}"], root, check=False)
            print(f"  Initialized repo from bundle. Checked out '{target}'.")
        else:
            # Fetch from bundle, then merge FETCH_HEAD
            # (more reliable than `git pull <bundle>` which needs refspec guessing)
            r = _git(["fetch", path], root, check=False)
            if r.returncode != 0:
                print(f"  Fetch failed: {r.stderr.strip()}")
                return
            r = _git(["merge", "FETCH_HEAD", "--no-edit"], root, check=False)
            if r.returncode != 0:
                if "CONFLICT" in r.stdout or "CONFLICT" in r.stderr:
                    print("  Merge conflicts detected. Resolve manually.")
                else:
                    print(f"  Merge: {r.stderr.strip() or r.stdout.strip()}")
            else:
                output = r.stdout.strip()
                print(f"  {output}" if output else "  Merged successfully.")
    finally:
        os.unlink(path)


def has_changes(root, since):
    """Check whether there are commits since the given tag."""
    r = _git(["rev-list", "--count", f"{since}..HEAD"], root, check=False)
    if r.returncode != 0:
        return True  # can't tell → assume yes
    return int(r.stdout.strip()) > 0


# ---------------------------------------------------------------------------
# QR rendering
# ---------------------------------------------------------------------------

def render_qr(data_str, size=QR_RENDER_SIZE):
    """Return a numpy BGR image of the QR code."""
    qr = qrcode.QRCode(error_correction=EC_LEVEL, box_size=10, border=4)
    qr.add_data(data_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    # NEAREST keeps crisp pixel boundaries for QR codes
    img = img.resize((size, size), _NEAREST)
    return np.array(img)[:, :, ::-1]  # RGB→BGR for cv2


def _put_text(frame, text, x, y, scale=0.55, color=(0, 0, 0), thick=1):
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, thick, cv2.LINE_AA)


def sender_frame(qr_img, label, info, speed_ms, paused, idx, total):
    """Compose the sender display window."""
    frame = np.ones((WINDOW_H, WINDOW_W, 3), dtype=np.uint8) * 255
    h, w = qr_img.shape[:2]
    x0 = (WINDOW_W - w) // 2
    frame[10:10 + h, x0:x0 + w] = qr_img

    y = 10 + h + 25
    _put_text(frame, info, 20, y, 0.48, (80, 80, 80))
    y += 28
    _put_text(frame, f"QR {label}   |   Speed: {speed_ms}ms   |   "
              f"{'PAUSED' if paused else 'CYCLING'}", 20, y, 0.50,
              (0, 0, 200) if paused else (0, 140, 0))
    y += 18
    bar_w = WINDOW_W - 40
    cv2.rectangle(frame, (20, y), (20 + bar_w, y + 8), (220, 220, 220), -1)
    fill = int(bar_w * (idx + 1) / total) if total else 0
    cv2.rectangle(frame, (20, y), (20 + fill, y + 8), (0, 160, 0), -1)
    y += 22
    _put_text(frame, "t=tag sync   q=quit without tagging", 20, y, 0.42, (120, 120, 120))
    return frame


def _window_open(name):
    """Check if a cv2 window is still open (user didn't click X).

    Behavior of getWindowProperty varies by backend (GTK/Cocoa/Win32).
    WND_PROP_VISIBLE returns 1.0 if open on most backends.
    Some Windows builds return -1 when window is present but that flag isn't
    supported, so we also accept that as "open" and only treat 0.0 as closed.
    """
    try:
        val = cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE)
        return val != 0.0
    except cv2.error:
        return False


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------

def _resume_path(repo_root_path):
    """Return the path for saving/loading partial receive state."""
    safe = repo_root_path.replace("/", "_").replace("\\", "_").replace(":", "").strip("_")
    return RESUME_DIR / f"{safe}.json"


def save_partial(repo_root_path, sid, n_chunks, n_bytes, expected_sha, branch, received):
    """Save partial receive state to disk."""
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "sid": sid, "n": n_chunks, "sz": n_bytes,
        "sha": expected_sha, "ref": branch,
        "chunks": received,  # idx(str) → b64 segment
        "ts": time.time(),
    }
    p = _resume_path(repo_root_path)
    with open(p, "w") as f:
        json.dump(state, f)
    print(f"\n  Partial state saved ({len(received)}/{n_chunks} chunks).")
    print(f"  Resume with: qr_git_sync.py receive --repo {repo_root_path}")


def load_partial(repo_root_path):
    """Load partial receive state. Returns dict or None."""
    p = _resume_path(repo_root_path)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            state = json.load(f)
        age = time.time() - state.get("ts", 0)
        if age > 86400:  # stale after 24h
            try:
                p.unlink()
            except OSError:
                pass
            return None
        # Convert string keys back to int
        state["chunks"] = {int(k): v for k, v in state["chunks"].items()}
        return state
    except (json.JSONDecodeError, KeyError):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return None


def clear_partial(repo_root_path):
    p = _resume_path(repo_root_path)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# SEND
# ---------------------------------------------------------------------------

def cmd_send(args):
    root = repo_root(args.repo)
    branch = current_branch(root)
    chunk_sz = args.chunk_size

    since = None
    if not args.full:
        since = last_sync_tag(root)
        if since:
            if not has_changes(root, since):
                print(f"No new commits since {since}. Nothing to send.")
                return
            print(f"Last sync: {since}")
        else:
            print("No previous sync tag. Sending full repo.")

    # Build bundle
    print("Creating git bundle...")
    bundle = create_bundle(root, since)
    print(f"  Bundle: {len(bundle):,} bytes")

    # Compress (level 1 — bundles are already pack-compressed internally)
    compressed = zlib.compress(bundle, 1)
    ratio = len(compressed) * 100 // len(bundle)
    print(f"  Compressed: {len(compressed):,} bytes ({ratio}%)")

    # Encode
    encoded = base64.b64encode(compressed).decode("ascii")
    sha16 = hashlib.sha256(compressed).hexdigest()[:16]

    # Chunk
    chunks = []
    for i in range(0, len(encoded), chunk_sz):
        seg = encoded[i:i + chunk_sz]
        crc = zlib.crc32(seg.encode()) & 0xFFFFFFFF
        chunks.append((seg, crc))

    n = len(chunks)
    est_sec = n * args.speed // 1000
    print(f"  Chunks: {n} ({chunk_sz} B each)")
    print(f"  Estimated time per full pass: ~{est_sec}s")

    if n > MAX_PRERENDER_CHUNKS:
        print(f"  WARNING: {n} chunks is very large. Consider sneakernet (USB).")
        print(f"  At {args.speed}ms/frame, one full pass takes ~{est_sec // 60} minutes.")
        print("  Continue? [y/N] ", end="", flush=True)
        if input().strip().lower() != "y":
            return

    print()
    print("Controls: SPACE=pause  +/-=speed  n/p=step  t=tag  q=quit")
    print("Point the receiver's camera at this screen.")
    print()

    # Lazy rendering — generate QR codes on demand with LRU cache
    # Session ID derived from content hash so same repo state = same frames
    sid = sha16[:6]
    meta_qr = render_qr(pkt_meta(sid, n, len(encoded), sha16, branch))
    end_qr = render_qr(pkt_end(sid))
    qr_cache = collections.OrderedDict()
    total = n + 2

    def get_frame(frame_idx):
        if frame_idx == 0:
            return "META", meta_qr
        elif frame_idx == total - 1:
            return "END", end_qr
        else:
            ci = frame_idx - 1
            if ci in qr_cache:
                qr_cache.move_to_end(ci)
                return f"{ci+1}/{n}", qr_cache[ci]
            seg, crc = chunks[ci]
            img = render_qr(pkt_data(sid, ci, n, seg, crc))
            qr_cache[ci] = img
            if len(qr_cache) > LAZY_CACHE_SIZE:
                qr_cache.popitem(last=False)
            return f"{ci+1}/{n}", img

    # Display carousel
    win = "QR Git Sync  [SEND]"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, WINDOW_W, WINDOW_H)
    _register_cleanup(cv2.destroyAllWindows)

    idx = 0
    paused = False
    speed = args.speed
    last_t = time.time()
    tagged = False
    info = f"sid={sid}  branch={branch}  bundle={len(bundle):,}B  chunks={n}"

    while _window_open(win):
        label, qr_img = get_frame(idx)
        disp = sender_frame(qr_img, label, info, speed, paused, idx, total)
        cv2.imshow(win, disp)

        key = cv2.waitKey(20) & 0xFF
        if key in (ord("q"), 27):
            break
        elif key == ord("t"):
            if not tagged:
                tag = set_sync_tag(root)
                tagged = True
                print(f"Sync tagged: {tag}")
                print("  Keep cycling until receiver confirms, then q to quit.")
            else:
                print("  Already tagged.")
        elif key == ord(" "):
            paused = not paused
        elif key in (ord("+"), ord("=")):
            speed = max(50, speed - 50)
        elif key == ord("-"):
            speed = min(3000, speed + 50)
        elif key == ord("n"):
            idx = (idx + 1) % total
            last_t = time.time()
        elif key == ord("p"):
            idx = (idx - 1) % total
            last_t = time.time()

        now = time.time()
        if not paused and (now - last_t) >= speed / 1000.0:
            idx = (idx + 1) % total
            last_t = now

    cv2.destroyAllWindows()

    if not tagged:
        print("Exited without tagging. Next send will include the same commits.")
        print("  (Run 'status' to check, or use 't' during send to tag.)")


# ---------------------------------------------------------------------------
# RECEIVE
# ---------------------------------------------------------------------------

def cmd_receive(args):
    root = repo_root(args.repo)

    # Check for resumable partial transfer
    partial = load_partial(root)
    if partial:
        done = len(partial["chunks"])
        total = partial["n"]
        print(f"Found partial transfer: {done}/{total} chunks from session {partial['sid']}")
        print(f"  Resume this session? [Y/n] ", end="", flush=True)
        ans = input().strip().lower()
        if ans and ans != "y":
            clear_partial(root)
            partial = None
            print("  Cleared. Starting fresh.")

    print(f"Opening camera {args.camera}...")
    # On Windows, DirectShow (CAP_DSHOW) is faster to open and more reliable.
    # On macOS, AVFoundation is default and works well.
    # On Linux, V4L2 is default.
    if _IS_WINDOWS:
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print("Error: cannot open camera.")
        if _IS_WINDOWS:
            print("  Try a different camera index: --camera 1")
            print("  Ensure no other app is using the camera.")
        elif sys.platform == "darwin":
            print("  Grant camera access: System Settings > Privacy > Camera")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"  Resolution: {w}x{h}")
    print("Point camera at sender's screen.  q/ESC to quit.")
    print()

    detector = cv2.QRCodeDetector()

    # Transfer state — restore from partial if available
    if partial:
        sid = partial["sid"]
        n_chunks = partial["n"]
        n_bytes = partial["sz"]
        expected_sha = partial["sha"]
        branch = partial.get("ref", "?")
        received = partial["chunks"]
        got_meta = True
        start_time = time.time()
        last_new = time.time()
        print(f"  Resumed: {len(received)}/{n_chunks} chunks")
    else:
        sid = None
        n_chunks = 0
        n_bytes = 0
        expected_sha = None
        branch = None
        received = {}
        got_meta = False
        start_time = None
        last_new = time.time()

    stall_warned = False
    cam_failures = 0

    win = "QR Git Sync  [RECEIVE]"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    def _recv_cleanup():
        cap.release()
        cv2.destroyAllWindows()

    _register_cleanup(_recv_cleanup)

    while _window_open(win):
        ok, frame = cap.read()
        if not ok:
            cam_failures += 1
            if cam_failures >= CAMERA_FAIL_LIMIT:
                print(f"\nCamera failed ({CAMERA_FAIL_LIMIT} consecutive read errors).")
                if got_meta and received:
                    save_partial(root, sid, n_chunks, n_bytes, expected_sha, branch, received)
                break
            continue
        cam_failures = 0

        # Detect — wrap in try/except for cv2 edge cases
        data = ""
        pts = None
        try:
            data, pts, _ = detector.detectAndDecode(frame)
        except cv2.error:
            pass

        if data:
            try:
                pkt = json.loads(data)

                # Protocol version gate
                if "v" in pkt and pkt["v"] != PROTOCOL_VERSION:
                    print(f"\n  Protocol mismatch: sender v{pkt['v']}, "
                          f"receiver v{PROTOCOL_VERSION}. Cannot continue.")
                    break

                if pkt.get("t") == "meta":
                    if not got_meta:
                        sid = pkt["s"]
                        n_chunks = pkt["n"]
                        n_bytes = pkt["sz"]
                        expected_sha = pkt["sha"]
                        branch = pkt.get("ref", "?")
                        got_meta = True
                        start_time = time.time()
                        last_new = time.time()
                        print(f"  Session {sid} | {branch} | "
                              f"{n_chunks} chunks | {n_bytes:,} bytes")
                    elif got_meta and pkt["s"] != sid:
                        # New session detected — sender restarted
                        print(f"\n  New session {pkt['s']} detected (was {sid}). Resetting.")
                        sid = pkt["s"]
                        n_chunks = pkt["n"]
                        n_bytes = pkt["sz"]
                        expected_sha = pkt["sha"]
                        branch = pkt.get("ref", "?")
                        received = {}
                        start_time = time.time()
                        last_new = time.time()
                        stall_warned = False
                        print(f"  Session {sid} | {branch} | "
                              f"{n_chunks} chunks | {n_bytes:,} bytes")

                elif pkt.get("t") == "d" and got_meta and pkt.get("s") == sid:
                    i = pkt["i"]
                    if 0 <= i < n_chunks and i not in received:
                        seg = pkt["d"]
                        crc = zlib.crc32(seg.encode()) & 0xFFFFFFFF
                        if crc == pkt["c"]:
                            received[i] = seg
                            last_new = time.time()
                            stall_warned = False
                            done = len(received)
                            pct = done * 100 // n_chunks
                            print(f"    [{done}/{n_chunks}] {pct}%  "
                                  f"chunk {i}", end="\r")

                # "end" packet is informational — completion is determined by chunk count

            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Stall detection
        if got_meta and len(received) < n_chunks:
            since_last = time.time() - last_new
            if since_last > STALL_WARN_SECS and not stall_warned:
                missing = n_chunks - len(received)
                print(f"\n  Stall: no new chunks for {int(since_last)}s. "
                      f"{missing} remaining.")
                print("  Check camera alignment and sender is still cycling.")
                stall_warned = True

        # --- Overlay (draw directly on frame, no copy needed) ---
        fh, fw = frame.shape[:2]

        if got_meta:
            done = len(received)
            pct = done / n_chunks if n_chunks else 0
            bar_x, bar_y = int(fw * 0.1), fh - 50
            bar_w = int(fw * 0.8)

            bar_h = 24
            # Draw dark background for entire bar
            cv2.rectangle(frame, (bar_x, bar_y),
                          (bar_x + bar_w, bar_y + bar_h), (40, 40, 40), -1)
            # Draw individual chunk slots — green if received, stays dark if missing
            if n_chunks > 0:
                slot_w = bar_w / n_chunks
                for ci in range(n_chunks):
                    if ci in received:
                        sx = bar_x + int(ci * slot_w)
                        ex = bar_x + int((ci + 1) * slot_w)
                        cv2.rectangle(frame, (sx, bar_y),
                                      (ex, bar_y + bar_h), (0, 220, 0), -1)
                # Draw thin separator lines when chunks are wide enough
                if slot_w >= 3:
                    for ci in range(1, n_chunks):
                        lx = bar_x + int(ci * slot_w)
                        cv2.line(frame, (lx, bar_y), (lx, bar_y + bar_h),
                                 (30, 30, 30), 1)
            cv2.putText(frame,
                        f"{done}/{n_chunks}  ({int(pct*100)}%)",
                        (bar_x, bar_y - 8), cv2.FONT_HERSHEY_SIMPLEX,
                        0.65, (0, 255, 0), 2, cv2.LINE_AA)
            # Show first 10 missing chunk numbers
            missing_ids = [i for i in range(n_chunks) if i not in received]
            if missing_ids:
                shown = missing_ids[:10]
                ellipsis = "..." if len(missing_ids) > 10 else ""
                miss_text = f"Missing: {', '.join(str(x) for x in shown)}{ellipsis}"
                # Dark background behind text for readability — above the bar
                (tw, th), _ = cv2.getTextSize(miss_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                tx, ty = bar_x, bar_y - 35
                cv2.rectangle(frame, (tx - 4, ty - th - 4),
                              (tx + tw + 4, ty + 6), (0, 0, 0), -1)
                cv2.putText(frame, miss_text,
                            (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (0, 200, 255), 2, cv2.LINE_AA)

            if pts is not None and len(pts) > 0:
                try:
                    ipts = pts.astype(int)
                    for j in range(len(ipts[0])):
                        cv2.line(frame,
                                 tuple(ipts[0][j]),
                                 tuple(ipts[0][(j + 1) % len(ipts[0])]),
                                 (0, 255, 0), 2)
                except (IndexError, ValueError):
                    pass
        else:
            cv2.putText(frame, "Waiting for sender...",
                        (20, 40), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 0, 255), 2, cv2.LINE_AA)

        cv2.imshow(win, frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            if got_meta and received and len(received) < n_chunks:
                save_partial(root, sid, n_chunks, n_bytes,
                             expected_sha, branch, received)
            else:
                print("\nCancelled.")
            break
        elif key == ord("r"):
            print("\n  Reset. Waiting for new session...")
            sid = None
            n_chunks = 0
            received = {}
            got_meta = False
            stall_warned = False
            clear_partial(root)

        # --- Completion ---
        if got_meta and len(received) == n_chunks:
            if set(received.keys()) != set(range(n_chunks)):
                print(f"\n  Index mismatch — expected 0..{n_chunks-1}.")
                print("  Transfer corrupted. Try again.")
                break

            elapsed = time.time() - start_time
            print(f"\n  All {n_chunks} chunks received in {elapsed:.1f}s")
            print("  Reconstructing...")

            encoded = "".join(received[i] for i in range(n_chunks))
            compressed = base64.b64decode(encoded)
            sha16 = hashlib.sha256(compressed).hexdigest()[:16]

            if sha16 != expected_sha:
                print(f"  SHA MISMATCH  expected={expected_sha}  got={sha16}")
                print("  Transfer corrupted. Try again.")
                break

            print(f"  SHA verified: {sha16}")
            bundle = zlib.decompress(compressed)
            print(f"  Bundle: {len(bundle):,} bytes")

            print("  Applying...")
            try:
                apply_bundle(root, bundle)
                tag = set_sync_tag(root)
                print(f"  Sync complete. Tagged: {tag}")
                clear_partial(root)
            except Exception as e:
                print(f"  Error: {e}")
                recovery = Path(root) / "received.bundle"
                with open(recovery, "wb") as f:
                    f.write(bundle)
                print(f"  Bundle saved to {recovery}")
            break

    _recv_cleanup()


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def cmd_status(args):
    root = repo_root(args.repo)
    branch = current_branch(root)
    tag = last_sync_tag(root)

    print(f"Repo:   {root}")
    print(f"Branch: {branch}")

    if tag:
        ts_str = tag.replace(SYNC_TAG_PREFIX, "").split("-")[0]  # strip random suffix
        try:
            ts_ms = int(ts_str)
            ts = ts_ms / 1000 if ts_ms > 9999999999 else ts_ms  # detect ms vs s
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except ValueError:
            when = "?"
        r = _git(["rev-list", "--count", f"{tag}..HEAD"], root, check=False)
        ahead = r.stdout.strip() if r.returncode == 0 else "?"
        print(f"Last sync: {tag}  ({when})")
        print(f"Commits since: {ahead}")
    else:
        print("Last sync: never")

    # Check for partial receive
    partial = load_partial(root)
    if partial:
        done = len(partial["chunks"])
        total = partial["n"]
        age = time.time() - partial.get("ts", 0)
        print(f"Partial receive: {done}/{total} chunks "
              f"(session {partial['sid']}, {int(age)}s ago)")


# ---------------------------------------------------------------------------
# SELFTEST — run on a new machine to verify environment before real transfer
# ---------------------------------------------------------------------------

def cmd_selftest(args):
    """Quick environment check: deps, git, camera, QR encode/decode round-trip."""
    ok = True

    def check(label, fn):
        nonlocal ok
        try:
            result = fn()
            if result is True or result is None:
                print(f"  OK    {label}")
            else:
                print(f"  OK    {label} — {result}")
        except Exception as e:
            print(f"  FAIL  {label} — {e}")
            ok = False

    print("Environment selftest\n")

    # 1. Python version
    check("Python >= 3.6", lambda: f"{sys.version_info.major}.{sys.version_info.minor}"
          if sys.version_info >= (3, 6) else (_ for _ in ()).throw(
              RuntimeError(f"Need 3.6+, got {sys.version_info}")))

    # 2. Dependencies
    check("qrcode", lambda: f"{qrcode.__version__}" if hasattr(qrcode, "__version__") else True)
    check("opencv (cv2)", lambda: cv2.__version__)
    check("numpy", lambda: np.__version__)
    check("PIL/Pillow", lambda: Image.__version__ if hasattr(Image, "__version__") else True)

    # 3. Git
    check("git on PATH", lambda: _git(["--version"], ".", check=True).stdout.strip())

    # 4. QR encode → decode round-trip
    def qr_round_trip():
        test_data = '{"t":"selftest","v":1,"msg":"hello"}'
        img = render_qr(test_data, size=400)
        detector = cv2.QRCodeDetector()
        decoded, _, _ = detector.detectAndDecode(img)
        if decoded != test_data:
            raise RuntimeError(f"Decoded '{decoded}' != expected")
        return "encode→decode OK"
    check("QR round-trip", qr_round_trip)

    # 5. Camera (optional, quick probe)
    def camera_probe():
        if _IS_WINDOWS:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        else:
            cap = cv2.VideoCapture(0)
        try:
            if not cap.isOpened():
                raise RuntimeError("Cannot open camera 0. Try --camera 1 or check permissions.")
            ret, frame = cap.read()
            if not ret or frame is None:
                raise RuntimeError("Camera opened but read failed")
            h, w = frame.shape[:2]
            return f"camera 0 — {w}x{h}"
        finally:
            cap.release()
    check("Camera", camera_probe)

    # 6. GUI window (quick open/close)
    def gui_check():
        cv2.namedWindow("selftest", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("selftest", 200, 200)
        test_img = np.zeros((200, 200, 3), dtype=np.uint8)
        cv2.putText(test_img, "OK", (60, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 255, 0), 3)
        cv2.imshow("selftest", test_img)
        cv2.waitKey(500)
        cv2.destroyAllWindows()
        return "window opened and closed"
    check("GUI/display", gui_check)

    print()
    if ok:
        print("All checks passed. Ready to sync.")
    else:
        print("Some checks failed. Fix the issues above before syncing.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# TESTQR — generate QR PNGs for phone-based receive testing
# ---------------------------------------------------------------------------

def cmd_testqr(args):
    """Generate QR code PNGs that simulate a small send session.

    Creates a test git bundle from a tiny throwaway repo, encodes it the same
    way the sender would, and writes numbered PNG files.  Open them on your
    phone and swipe through while the receiver is watching via webcam.
    """
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    # Build a tiny repo + bundle in a temp dir
    tmp = tempfile.mkdtemp(prefix="qrtestqr_")
    try:
        subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=tmp, capture_output=True)
        test_file = os.path.join(tmp, "hello.txt")
        with open(test_file, "w") as f:
            f.write("Hello from qr-git-sync test!\n")
            f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("If you can read this, the transfer worked.\n")
        subprocess.run(["git", "add", "."], cwd=tmp, capture_output=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "test commit"],
            cwd=tmp, capture_output=True, check=True,
        )
        bundle = create_bundle(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # Encode exactly like cmd_send does
    compressed = zlib.compress(bundle, 1)
    encoded = base64.b64encode(compressed).decode("ascii")
    sha16 = hashlib.sha256(compressed).hexdigest()[:16]

    chunk_sz = args.chunk_size
    chunks = []
    for i in range(0, len(encoded), chunk_sz):
        seg = encoded[i:i + chunk_sz]
        crc = zlib.crc32(seg.encode()) & 0xFFFFFFFF
        chunks.append((seg, crc))

    n = len(chunks)
    sid = _session_id()

    print(f"Test bundle: {len(bundle):,} bytes → {len(compressed):,} compressed")
    print(f"Session: {sid}  |  Chunks: {n}  |  SHA: {sha16}")
    print()

    # Generate QR PNGs: meta, data chunks, end
    frames = []
    frames.append(("00_meta", pkt_meta(sid, n, len(encoded), sha16, "main")))
    for i, (seg, crc) in enumerate(chunks):
        frames.append((f"{i+1:02d}_data", pkt_data(sid, i, n, seg, crc)))
    frames.append((f"{n+1:02d}_end", pkt_end(sid)))

    for name, payload in frames:
        qr = qrcode.QRCode(error_correction=EC_LEVEL, box_size=12, border=6)
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        fpath = out / f"qr_{name}.png"
        img.save(str(fpath))

    total = len(frames)
    print(f"Wrote {total} QR images to: {out.resolve()}")
    print()
    print("How to test:")
    print(f"  1. Open the {total} PNGs on your phone (in order)")
    print("  2. On the Windows machine, init a test repo:")
    print("       mkdir test-recv && cd test-recv && git init")
    print(f"       py qr_git_sync.py receive --repo .")
    print(f"  3. Show each QR to the camera — hold steady for ~1 second each")
    print(f"  4. Order doesn't matter, receiver will collect all {n} data chunks")
    print(f"  5. When done, check: git log && cat hello.txt")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="qr_git_sync",
        description="Transfer git changes between air-gapped machines via QR codes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python qr_git_sync.py send                  # send changes from cwd repo
  python qr_git_sync.py send --full           # send entire repo (ignore sync tags)
  python qr_git_sync.py send --chunk-size 400 # smaller QR = more reliable on bad cameras
  python qr_git_sync.py receive               # receive into cwd repo
  python qr_git_sync.py receive --camera 1    # use second camera
  python qr_git_sync.py status                # show sync state
        """,
    )
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("send", help="Display git changes as cycling QR codes")
    s.add_argument("--repo", default=".", help="Git repo path (default: cwd)")
    s.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                   help=f"Base64 bytes per QR (default: {DEFAULT_CHUNK_SIZE})")
    s.add_argument("--speed", type=int, default=DEFAULT_DISPLAY_MS,
                   help=f"Ms per QR frame (default: {DEFAULT_DISPLAY_MS})")
    s.add_argument("--full", action="store_true",
                   help="Bundle entire repo (ignore sync tags)")

    r = sub.add_parser("receive", help="Read QR codes via webcam")
    r.add_argument("--repo", default=".", help="Git repo path (default: cwd)")
    r.add_argument("--camera", type=int, default=1, help="Camera index (default: 1)")

    st = sub.add_parser("status", help="Show sync state")
    st.add_argument("--repo", default=".", help="Git repo path (default: cwd)")

    sub.add_parser("selftest", help="Verify environment (deps, git, camera, QR)")

    tq = sub.add_parser("testqr", help="Generate QR PNGs for phone-based receive test")
    tq.add_argument("--output", default="test_qrs", help="Output directory (default: test_qrs)")
    tq.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                     help=f"Base64 bytes per QR (default: {DEFAULT_CHUNK_SIZE})")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(1)

    try:
        {"send": cmd_send, "receive": cmd_receive, "status": cmd_status,
         "selftest": cmd_selftest, "testqr": cmd_testqr}[args.cmd](args)
    except KeyboardInterrupt:
        _run_cleanup()
        print("\nInterrupted.")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

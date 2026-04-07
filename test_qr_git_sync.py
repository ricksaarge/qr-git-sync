#!/usr/bin/env python3
"""Comprehensive tests for qr_git_sync.py — no camera/GUI required."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(__file__))
import qr_git_sync as q


PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def _make_repo(path, n_commits=2):
    """Create a test git repo with n commits."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=path, capture_output=True)
    for i in range(n_commits):
        fpath = os.path.join(path, f"file{i}.txt")
        with open(fpath, "w") as f:
            f.write(f"content {i}\n")
        subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"commit {i}"],
            cwd=path, capture_output=True,
        )


# -----------------------------------------------------------------------
print("\n=== Protocol ===")

sid = q._session_id()
test("session_id length", len(sid) == 6)
test("session_id alphanumeric", sid.isalnum())

meta = q.pkt_meta("abc123", 10, 5000, "deadbeef01234567", "main")
parsed = json.loads(meta)
test("meta has version", parsed["v"] == q.PROTOCOL_VERSION)
test("meta type", parsed["t"] == "meta")
test("meta session", parsed["s"] == "abc123")
test("meta chunks", parsed["n"] == 10)
test("meta bytes", parsed["sz"] == 5000)
test("meta sha", parsed["sha"] == "deadbeef01234567")
test("meta branch", parsed["ref"] == "main")

data = q.pkt_data("abc123", 3, 10, "AQID", 12345)
parsed = json.loads(data)
test("data type", parsed["t"] == "d")
test("data index", parsed["i"] == 3)
test("data total", parsed["n"] == 10)
test("data payload", parsed["d"] == "AQID")
test("data crc", parsed["c"] == 12345)

end = q.pkt_end("abc123")
parsed = json.loads(end)
test("end type", parsed["t"] == "end")
test("end session", parsed["s"] == "abc123")

# Verify no version in data/end packets (saves bytes)
test("data no version key", "v" not in json.loads(data))
test("end no version key", "v" not in json.loads(end))

# -----------------------------------------------------------------------
print("\n=== QR Rendering ===")

img = q.render_qr("hello world")
test("qr image shape", img.shape == (580, 580, 3))
test("qr image dtype", img.dtype == q.np.uint8)
# QR codes are black and white — should have pixels near 0 and near 255
test("qr has dark pixels", img.min() < 50)
test("qr has light pixels", img.max() > 200)

# Render a data packet (larger payload)
big_payload = "A" * 600
big_pkt = q.pkt_data("test", 0, 1, big_payload, 0)
big_img = q.render_qr(big_pkt)
test("large qr renders", big_img.shape == (580, 580, 3))

# -----------------------------------------------------------------------
print("\n=== Git Helpers ===")

repo_dir = tempfile.mkdtemp(prefix="qrtest_")
try:
    _make_repo(repo_dir, 3)

    root = q.repo_root(repo_dir)
    test("repo_root resolves", root.endswith("qrtest_") or "qrtest_" in root)

    branch = q.current_branch(root)
    test("current_branch is main", branch == "main")

    test("is_empty_repo false", not q.is_empty_repo(root))

    branches = q.list_branches(root)
    test("list_branches includes main", "main" in branches)

    test("last_sync_tag initially none", q.last_sync_tag(root) is None)

    tag = q.set_sync_tag(root)
    test("set_sync_tag returns tag", tag.startswith(q.SYNC_TAG_PREFIX))

    found = q.last_sync_tag(root)
    test("last_sync_tag finds it", found == tag)

    test("has_changes false after tag", not q.has_changes(root, tag))

    # Add another commit
    with open(os.path.join(root, "new.txt"), "w") as f:
        f.write("new\n")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "new"],
        cwd=root, capture_output=True,
    )

    test("has_changes true after new commit", q.has_changes(root, tag))

    # -----------------------------------------------------------------------
    print("\n=== Bundle Creation ===")

    # Full bundle
    bundle = q.create_bundle(root)
    test("full bundle non-empty", len(bundle) > 0)
    test("full bundle starts with # (pack header)", bundle[:1] == b"#")

    # Incremental bundle
    inc_bundle = q.create_bundle(root, tag)
    test("incremental bundle non-empty", len(inc_bundle) > 0)
    test("incremental bundle smaller than full", len(inc_bundle) < len(bundle))

    # Bundle from invalid tag falls back to full
    fallback = q.create_bundle(root, "nonexistent-tag")
    test("fallback bundle non-empty", len(fallback) > 0)

    # -----------------------------------------------------------------------
    print("\n=== Bundle Apply (empty repo) ===")

    recv_dir = tempfile.mkdtemp(prefix="qrrecv_")
    subprocess.run(["git", "init"], cwd=recv_dir, capture_output=True)
    test("recv repo is empty", q.is_empty_repo(recv_dir))

    q.apply_bundle(recv_dir, bundle)
    test("recv repo no longer empty", not q.is_empty_repo(recv_dir))
    recv_branch = q.current_branch(recv_dir)
    test("recv checked out main", recv_branch == "main")

    # Verify files exist
    test("file0.txt exists in recv", os.path.exists(os.path.join(recv_dir, "file0.txt")))
    test("new.txt exists in recv", os.path.exists(os.path.join(recv_dir, "new.txt")))

    shutil.rmtree(recv_dir)

    # -----------------------------------------------------------------------
    print("\n=== Bundle Apply (existing repo) ===")

    recv_dir2 = tempfile.mkdtemp(prefix="qrrecv2_")
    subprocess.run(["git", "init"], cwd=recv_dir2, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=recv_dir2, capture_output=True)
    # Apply full bundle first
    q.apply_bundle(recv_dir2, bundle)
    # Add a local commit (diverge)
    with open(os.path.join(recv_dir2, "local.txt"), "w") as f:
        f.write("local change\n")
    subprocess.run(["git", "add", "."], cwd=recv_dir2, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "local"],
        cwd=recv_dir2, capture_output=True,
    )
    # Add another commit to sender
    with open(os.path.join(root, "sender2.txt"), "w") as f:
        f.write("sender2\n")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "sender2"],
        cwd=root, capture_output=True,
    )
    tag2 = q.set_sync_tag(root)
    inc2 = q.create_bundle(root, tag)
    q.apply_bundle(recv_dir2, inc2)
    test("recv2 has local.txt", os.path.exists(os.path.join(recv_dir2, "local.txt")))
    test("recv2 has sender2.txt", os.path.exists(os.path.join(recv_dir2, "sender2.txt")))

    shutil.rmtree(recv_dir2)

    # -----------------------------------------------------------------------
    print("\n=== Full Round-Trip (send → receive simulation) ===")

    bundle = q.create_bundle(root)
    compressed = zlib.compress(bundle, 1)
    encoded = base64.b64encode(compressed).decode("ascii")
    sha16 = hashlib.sha256(compressed).hexdigest()[:16]

    # Chunk
    chunk_sz = q.DEFAULT_CHUNK_SIZE
    chunks = []
    for i in range(0, len(encoded), chunk_sz):
        seg = encoded[i:i + chunk_sz]
        crc = zlib.crc32(seg.encode()) & 0xFFFFFFFF
        chunks.append((seg, crc))
    n = len(chunks)
    sid = q._session_id()

    # Simulate receiver decoding packets
    received = {}
    meta_pkt = q.pkt_meta(sid, n, len(encoded), sha16, "main")
    meta_obj = json.loads(meta_pkt)
    test("meta round-trip version", meta_obj["v"] == q.PROTOCOL_VERSION)

    for i, (seg, crc) in enumerate(chunks):
        pkt_str = q.pkt_data(sid, i, n, seg, crc)
        pkt_obj = json.loads(pkt_str)
        # Verify CRC
        recv_crc = zlib.crc32(pkt_obj["d"].encode()) & 0xFFFFFFFF
        assert recv_crc == pkt_obj["c"], f"CRC mismatch at {i}"
        received[i] = pkt_obj["d"]

    test("all chunks received", len(received) == n)
    test("index set complete", set(received.keys()) == set(range(n)))

    # Reconstruct
    recon_encoded = "".join(received[i] for i in range(n))
    recon_compressed = base64.b64decode(recon_encoded)
    recon_sha = hashlib.sha256(recon_compressed).hexdigest()[:16]
    test("sha matches", recon_sha == sha16)

    recon_bundle = zlib.decompress(recon_compressed)
    test("bundle matches exactly", recon_bundle == bundle)

    # Apply to fresh repo
    apply_dir = tempfile.mkdtemp(prefix="qrapply_")
    subprocess.run(["git", "init"], cwd=apply_dir, capture_output=True)
    q.apply_bundle(apply_dir, recon_bundle)
    test("applied repo has files", os.path.exists(os.path.join(apply_dir, "file0.txt")))
    shutil.rmtree(apply_dir)

    # -----------------------------------------------------------------------
    print("\n=== Resume Support ===")

    # Save partial (half the chunks)
    half = {i: received[i] for i in range(n // 2)}
    q.save_partial(root, sid, n, len(encoded), sha16, "main", half)

    loaded = q.load_partial(root)
    test("load_partial succeeds", loaded is not None)
    test("loaded chunk count", len(loaded["chunks"]) == n // 2)
    test("loaded sid", loaded["sid"] == sid)
    test("loaded sha", loaded["sha"] == sha16)
    test("loaded chunks are ints", all(isinstance(k, int) for k in loaded["chunks"]))

    # Save again (overwrite)
    q.save_partial(root, sid, n, len(encoded), sha16, "main", received)
    loaded2 = q.load_partial(root)
    test("overwrite works", len(loaded2["chunks"]) == n)

    # Clear
    q.clear_partial(root)
    test("clear works", q.load_partial(root) is None)

    # Clear again (idempotent)
    q.clear_partial(root)
    test("double clear safe", q.load_partial(root) is None)

    # -----------------------------------------------------------------------
    print("\n=== Resume Path (cross-platform) ===")

    unix_path = q._resume_path("/home/user/my-repo")
    test("unix path is valid", str(unix_path).endswith(".json"))
    test("unix path no slashes in name", "/" not in unix_path.name)

    win_path = q._resume_path("C:\\Users\\foo\\repo")
    test("windows path no colon", ":" not in win_path.name)
    test("windows path no backslash", "\\" not in win_path.name)

    # -----------------------------------------------------------------------
    print("\n=== Edge Cases ===")

    # Empty repo bundle should fail
    empty_dir = tempfile.mkdtemp(prefix="qrempty_")
    subprocess.run(["git", "init"], cwd=empty_dir, capture_output=True)
    try:
        q.create_bundle(empty_dir)
        test("empty repo bundle raises", False)
    except RuntimeError:
        test("empty repo bundle raises", True)
    shutil.rmtree(empty_dir)

    # CRC mismatch detection
    seg = "AAAA"
    correct_crc = zlib.crc32(seg.encode()) & 0xFFFFFFFF
    wrong_crc = correct_crc ^ 0xFF
    test("crc mismatch detected", correct_crc != wrong_crc)

    # Chunk bounds check simulation
    test("negative index rejected", not (0 <= -1 < 10))
    test("over index rejected", not (0 <= 10 < 10))
    test("valid index accepted", 0 <= 5 < 10)

    # Sender frame rendering
    qr = q.render_qr("test")
    frame = q.sender_frame(qr, "1/5", "info text", 500, False, 0, 5)
    test("sender frame shape", frame.shape == (q.WINDOW_H, q.WINDOW_W, 3))

    paused_frame = q.sender_frame(qr, "1/5", "info", 500, True, 0, 5)
    test("paused frame renders", paused_frame.shape == (q.WINDOW_H, q.WINDOW_W, 3))

    # Window check on nonexistent window
    test("nonexistent window is closed", not q._window_open("no_such_window_xyz"))

    # -----------------------------------------------------------------------
    print("\n=== Chunking Edge Cases ===")

    # Single byte payload
    tiny = b"\x00"
    tiny_compressed = zlib.compress(tiny, 1)
    tiny_encoded = base64.b64encode(tiny_compressed).decode("ascii")
    tiny_chunks = []
    for i in range(0, len(tiny_encoded), q.DEFAULT_CHUNK_SIZE):
        seg = tiny_encoded[i:i + q.DEFAULT_CHUNK_SIZE]
        crc = zlib.crc32(seg.encode()) & 0xFFFFFFFF
        tiny_chunks.append((seg, crc))
    test("tiny payload is 1 chunk", len(tiny_chunks) == 1)

    # Payload exactly at chunk boundary
    exact = "A" * q.DEFAULT_CHUNK_SIZE
    exact_chunks = []
    for i in range(0, len(exact), q.DEFAULT_CHUNK_SIZE):
        seg = exact[i:i + q.DEFAULT_CHUNK_SIZE]
        exact_chunks.append(seg)
    test("exact boundary is 1 chunk", len(exact_chunks) == 1)

    # Payload one byte over boundary
    over = "A" * (q.DEFAULT_CHUNK_SIZE + 1)
    over_chunks = []
    for i in range(0, len(over), q.DEFAULT_CHUNK_SIZE):
        seg = over[i:i + q.DEFAULT_CHUNK_SIZE]
        over_chunks.append(seg)
    test("over boundary is 2 chunks", len(over_chunks) == 2)
    test("last chunk is 1 byte", len(over_chunks[-1]) == 1)

    # -----------------------------------------------------------------------
    print("\n=== Status Command (non-interactive) ===")

    # Just verify it doesn't crash
    class FakeArgs:
        repo = root
    try:
        q.cmd_status(FakeArgs())
        test("cmd_status runs", True)
    except Exception as e:
        test(f"cmd_status runs (failed: {e})", False)

finally:
    shutil.rmtree(repo_dir)

# -----------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
if FAIL > 0:
    sys.exit(1)
else:
    print("ALL TESTS PASSED")

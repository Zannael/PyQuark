import atexit
import os
import subprocess
import time
import threading
import tempfile
import shutil
import rarfile
import multivolumefile
import hashlib
from collections import OrderedDict
from src.vfs.core import get_rar_metadata, is_rar_multipart

STAGE_THRESHOLD = 3.5 * 1024 * 1024 * 1024  # 3.5 GiB circa
CACHE_ROOT = os.path.join(tempfile.gettempdir(), "pyquark_stage_cache")


class RarVirtualHandle:
    def __init__(self, rar_path, internal_path, mode="stream", cache_root=None,
                 force_delete_on_close=False):
        self.rar_path = rar_path
        self.internal_path = internal_path
        self.mode = mode
        self.cache_root = cache_root or CACHE_ROOT
        self.is_open = False
        # When True, close() will delete temp_dir even for stage-mode handles.
        # Used for XCI files staged from RAR archives to avoid leaving multi-GB
        # temp files on disk after the session ends.
        self.force_delete_on_close = force_delete_on_close

        self._extract_thread = None
        self._extract_proc = None
        self._stop_event = threading.Event()
        self._extraction_complete = threading.Event()
        self._error_event = threading.Event()

        if self.mode == "stage":
            try:
                st = os.stat(rar_path)
                cache_key_raw = f"{rar_path}|{st.st_mtime_ns}|{st.st_size}|{internal_path}"
            except Exception:
                cache_key_raw = f"{rar_path}|{internal_path}"

            cache_key = hashlib.sha1(cache_key_raw.encode("utf-8")).hexdigest()
            self.temp_dir = os.path.join(self.cache_root, cache_key)
        else:
            self.temp_dir = tempfile.mkdtemp(prefix="pyquark_")

        self.temp_filepath = os.path.join(self.temp_dir, internal_path.replace('/', os.sep))
        self.complete_marker = os.path.join(self.temp_dir, ".complete")

    def open(self):
        if self.is_open:
            return

        self.is_open = True
        self._stop_event.clear()
        self._error_event.clear()
        self._extraction_complete.clear()

        if self.mode == "stage":
            if os.path.exists(self.temp_filepath) and os.path.exists(self.complete_marker):
                self._extraction_complete.set()
                print(f"⚡ [VFS] Stage cache HIT: {self.internal_path}")
                return

        target = self._stage_worker if self.mode == "stage" else self._extract_worker
        self._extract_thread = threading.Thread(target=target, daemon=True)
        self._extract_thread.start()

        print(f"🔄 [VFS] Opening with {self.mode} started for: {self.internal_path}")

    def _stage_worker(self):
        try:
            os.makedirs(self.temp_dir, exist_ok=True)
            os.makedirs(os.path.dirname(self.temp_filepath), exist_ok=True)

            if os.path.exists(self.complete_marker):
                try:
                    os.remove(self.complete_marker)
                except Exception:
                    pass

            cmd = [
                "unrar", "x", "-y", "-idq",
                self.rar_path,
                self.internal_path,
                self.temp_dir
            ]

            self._extract_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            rc = self._extract_proc.wait()

            if self._stop_event.is_set():
                return

            if rc != 0:
                stderr = self._extract_proc.stderr.read().decode(errors="ignore")
                raise RuntimeError(f"unrar failed rc={rc}: {stderr}")

            if not os.path.exists(self.temp_filepath):
                raise RuntimeError("File staged not found after unrar")

            with open(self.complete_marker, "w", encoding="utf-8") as f:
                f.write("ok")

            self._extraction_complete.set()
            print(f"✅ [VFS] Stage finished: {self.internal_path}")

        except Exception as e:
            print(f"❌ [VFS] Stage error: {e}")
            self._error_event.set()
        finally:
            self._extract_proc = None

    def _extract_worker(self):
        """Thread executed function to extract file in blocks."""
        try:
            os.makedirs(os.path.dirname(self.temp_filepath), exist_ok=True)

            try:
                mvf = multivolumefile.open(self.rar_path, mode='rb')
                rf = rarfile.RarFile(mvf)
            except Exception:
                mvf = None
                rf = rarfile.RarFile(self.rar_path)

            with rf.open(self.internal_path) as source_stream:
                with open(self.temp_filepath, 'wb') as dest_file:
                    chunk_size = 4 * 1024 * 1024

                    while not self._stop_event.is_set():
                        chunk = source_stream.read(chunk_size)
                        if not chunk:
                            break

                        dest_file.write(chunk)
                        dest_file.flush()

            if not self._stop_event.is_set():
                self._extraction_complete.set()
                print(f"✅ [VFS] Extraction completed: {self.internal_path}")

        except Exception as e:
            print(f"❌ [VFS] Error during extraction: {e}")
            self._error_event.set()
        finally:
            if 'rf' in locals() and hasattr(rf, 'close'):
                rf.close()
            if 'mvf' in locals() and mvf:
                mvf.close()

    def read(self, offset, size):
        if not self.is_open:
            self.open()

        if self.mode == "stage":
            while True:
                if self._error_event.is_set():
                    return b''
                if self._extraction_complete.is_set():
                    break
                if self._stop_event.is_set():
                    return b''
                time.sleep(0.1)

            try:
                with open(self.temp_filepath, 'rb') as f:
                    f.seek(offset)
                    return f.read(size)
            except Exception as e:
                print(f"⚠️ [VFS] Error while reading staged file: {e}")
                return b''

        target_size = offset + size

        while True:
            if self._error_event.is_set():
                print("⚠️ [VFS] Impossible to read: extraction error.")
                return b''

            current_size = os.path.getsize(self.temp_filepath) if os.path.exists(self.temp_filepath) else 0

            if current_size >= target_size or self._extraction_complete.is_set():
                break

            if self._stop_event.is_set():
                return b''

            time.sleep(0.05)

        try:
            with open(self.temp_filepath, 'rb') as f:
                f.seek(offset)
                return f.read(size)
        except Exception as e:
            print(f"⚠️ [VFS] Error while reading temporary file: {e}")
            return b''

    def close(self):
        self._stop_event.set()

        if self._extract_proc and self._extract_proc.poll() is None:
            try:
                self._extract_proc.terminate()
            except Exception:
                pass

        if self._extract_thread and self._extract_thread.is_alive():
            self._extract_thread.join(timeout=1.0)

        self.is_open = False

        # Unconditional cleanup for handles that must not persist on disk
        # (e.g. staged XCI files from RAR archives, which can be multi-GB).
        if self.force_delete_on_close or self.mode == "stream":
            try:
                if os.path.exists(self.temp_dir):
                    shutil.rmtree(self.temp_dir)
                    label = "XCI staged" if self.force_delete_on_close else "stream"
                    print(f"🗑️ [VFS] Cache {label} cleaned: {self.temp_dir}")
            except Exception as e:
                print(f"⚠️ [VFS] Errore while cleaning: {e}")

    def wait_for_size(self, target_size, timeout=15.0):
        start_time = time.time()
        while True:
            if self._error_event.is_set():
                return False

            current_size = os.path.getsize(self.temp_filepath) if os.path.exists(self.temp_filepath) else 0

            if current_size >= target_size or self._extraction_complete.is_set():
                return True

            if self._stop_event.is_set():
                return False

            if time.time() - start_time > timeout:
                print(f"⚠️ [VFS] Timeout waiting buffer ({target_size} byte) for {self.internal_path}")
                return False

            time.sleep(0.1)


# Maximum number of concurrently open RAR handles.
# When the limit is reached _evict_one_handle() removes the oldest stream-mode
# entry (stage-mode entries are cheap to re-open because their disk cache stays
# intact even after eviction).
MAX_OPEN_HANDLES = 4

_OPEN_HANDLES: OrderedDict = OrderedDict()


def _evict_one_handle():
    """Close and remove the oldest stream-mode handle; fall back to stage-mode."""
    for path, handle in list(_OPEN_HANDLES.items()):
        if handle.mode == "stream":
            print(f"🔄 [VFS] Eviction stream handle: {handle.internal_path}")
            try:
                handle.close()
            except Exception:
                pass
            del _OPEN_HANDLES[path]
            return
    # No stream-mode handle found: evict the oldest stage-mode entry.
    # Its temp dir (disk cache) is intentionally left intact.
    if _OPEN_HANDLES:
        path, handle = next(iter(_OPEN_HANDLES.items()))
        print(f"🔄 [VFS] Eviction stage handle (disk-cache preserved): {handle.internal_path}")
        del _OPEN_HANDLES[path]

def vfs_start_file(virtual_path, phys_path, internal_path):
    if virtual_path in _OPEN_HANDLES:
        handle = _OPEN_HANDLES[virtual_path]

        if handle._error_event.is_set():
            print(f"♻️ [VFS] Error handle, re-create for {internal_path}")
            try:
                handle.close()
            except Exception:
                pass
            del _OPEN_HANDLES[virtual_path]
        else:
            print(f"⚡ [VFS] Smart Cache HIT: extraction reuse for {internal_path}")
            # Move to end so it is considered the most-recently-used entry.
            _OPEN_HANDLES.move_to_end(virtual_path)
            return

    # Evict if we are at capacity.
    if len(_OPEN_HANDLES) >= MAX_OPEN_HANDLES:
        _evict_one_handle()

    metadata = get_rar_metadata(phys_path)
    file_size = metadata['sizes'].get(internal_path, 0)
    multipart = is_rar_multipart(phys_path)

    mode = "stream"
    if multipart or file_size > STAGE_THRESHOLD:
        mode = "stage"

    # XCI files require random-access reads (XCIMapper seeks into arbitrary
    # offsets). Streaming is therefore never safe for them; force stage mode
    # regardless of file size or multipart status.
    is_xci = internal_path.lower().endswith('.xci')
    if is_xci:
        mode = "stage"

    handle = RarVirtualHandle(
        phys_path,
        internal_path,
        mode=mode,
        cache_root=CACHE_ROOT,
        # XCI staged files are potentially multi-GB; delete on close rather
        # than persisting in the shared cache directory.
        force_delete_on_close=is_xci,
    )
    handle.open()
    _OPEN_HANDLES[virtual_path] = handle

    print(f"📦 [VFS] Handle creato in modalità {mode} per {internal_path}")

def vfs_read_file(virtual_path, offset, size):
    if virtual_path in _OPEN_HANDLES:
        return _OPEN_HANDLES[virtual_path].read(offset, size)
    return b''

def vfs_end_file(virtual_path):
    pass

def vfs_get_handle(virtual_path):
    """Return the open RarVirtualHandle for *virtual_path*, or None."""
    return _OPEN_HANDLES.get(virtual_path)

def vfs_get_staged_path(virtual_path):
    """Return the absolute temp_filepath for *virtual_path*, or None."""
    handle = _OPEN_HANDLES.get(virtual_path)
    return handle.temp_filepath if handle is not None else None

def cleanup_all():
    print("\n🛑 [VFS] Serve shutting off: cache clean...")
    for path, handle in list(_OPEN_HANDLES.items()):
        handle.close()

atexit.register(cleanup_all)
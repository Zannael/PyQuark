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
from src.vfs.core import get_rar_metadata, is_rar_multipart

STAGE_THRESHOLD = 3.5 * 1024 * 1024 * 1024  # 3.5 GiB circa
CACHE_ROOT = os.path.join(tempfile.gettempdir(), "pyquark_stage_cache")


class RarVirtualHandle:
    def __init__(self, rar_path, internal_path, mode="stream", cache_root=None):
        self.rar_path = rar_path
        self.internal_path = internal_path
        self.mode = mode
        self.cache_root = cache_root or CACHE_ROOT
        self.is_open = False

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

        print(f"🔄 [VFS] Apertura {self.mode} avviata per: {self.internal_path}")

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
                raise RuntimeError("File staged non trovato dopo unrar")

            with open(self.complete_marker, "w", encoding="utf-8") as f:
                f.write("ok")

            self._extraction_complete.set()
            print(f"✅ [VFS] Stage completato: {self.internal_path}")

        except Exception as e:
            print(f"❌ [VFS] Errore stage: {e}")
            self._error_event.set()
        finally:
            self._extract_proc = None

    def _extract_worker(self):
        """Funzione eseguita dal thread: estrae il file a blocchi."""
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
                print(f"✅ [VFS] Estrazione completata: {self.internal_path}")

        except Exception as e:
            print(f"❌ [VFS] Errore critico durante l'estrazione: {e}")
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
                print(f"⚠️ [VFS] Errore lettura file staged: {e}")
                return b''

        target_size = offset + size

        while True:
            if self._error_event.is_set():
                print("⚠️ [VFS] Impossibile leggere: estrazione in errore.")
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
            print(f"⚠️ [VFS] Errore lettura file temporaneo: {e}")
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

        if self.mode == "stream":
            try:
                if os.path.exists(self.temp_dir):
                    shutil.rmtree(self.temp_dir)
                    print(f"🗑️ [VFS] Cache stream pulita: {self.temp_dir}")
            except Exception as e:
                print(f"⚠️ [VFS] Errore durante la pulizia: {e}")

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
                print(f"⚠️ [VFS] Timeout attesa buffer ({target_size} byte) per {self.internal_path}")
                return False

            time.sleep(0.1)


# --- MANAGER DEI FILE APERTI (Invariato nella logica, usa la nuova classe) ---

_OPEN_HANDLES = {}
_CURRENT_ACTIVE_PATH = None

def vfs_start_file(virtual_path, phys_path, internal_path):
    global _CURRENT_ACTIVE_PATH

    if virtual_path in _OPEN_HANDLES:
        handle = _OPEN_HANDLES[virtual_path]

        if handle._error_event.is_set():
            print(f"♻️ [VFS] Handle in errore, ricreazione per {internal_path}")
            try:
                handle.close()
            except Exception:
                pass
            del _OPEN_HANDLES[virtual_path]
        else:
            print(f"⚡ [VFS] Smart Cache HIT: riutilizzo l'estrazione per {internal_path}")
            _CURRENT_ACTIVE_PATH = virtual_path
            return

    metadata = get_rar_metadata(phys_path)
    file_size = metadata['sizes'].get(internal_path, 0)
    multipart = is_rar_multipart(phys_path)

    mode = "stream"
    if multipart or file_size > STAGE_THRESHOLD:
        mode = "stage"

    handle = RarVirtualHandle(
        phys_path,
        internal_path,
        mode=mode,
        cache_root=CACHE_ROOT
    )
    handle.open()
    _OPEN_HANDLES[virtual_path] = handle
    _CURRENT_ACTIVE_PATH = virtual_path

    print(f"📦 [VFS] Handle creato in modalità {mode} per {internal_path}")

def vfs_read_file(virtual_path, offset, size):
    if virtual_path in _OPEN_HANDLES:
        return _OPEN_HANDLES[virtual_path].read(offset, size)
    return b''

def vfs_end_file(virtual_path):
    # TRUCCO MAGICO: Ignoriamo la richiesta di chiusura di Goldleaf!
    # Il file continuerà a estrarsi in background e resterà pronto sul disco.
    pass

# Sistema di sicurezza: puliamo la cartella temporanea quando spegniamo il server (Ctrl+C)
def cleanup_all():
    print("\n🛑 [VFS] Spegnimento server: pulizia cache in corso...")
    for path, handle in list(_OPEN_HANDLES.items()):
        handle.close()

atexit.register(cleanup_all)
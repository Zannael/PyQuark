import usb.core
import struct
import os
from src.vfs.core import vfs_get_dirs, vfs_get_files, vfs_stat, parse_virtual_path
from src.vfs.rar_stream import (
    vfs_start_file, vfs_read_file, vfs_end_file,
    vfs_get_handle, vfs_get_staged_path,
)
from src.xci_virtualizer import XCIMapper, VirtualNSPBuilder
from src.session import SessionState

BLOCK_SIZE = 0x1000
INPUT_MAGIC = 0x49434C47
OUTPUT_MAGIC = 0x4F434C47
RESULT_SUCCESS = 0

# --- TUTTI I COMANDI ---
CMD_GET_DRIVE_COUNT = 1
CMD_GET_DRIVE_INFO = 2
CMD_STAT_PATH = 3
CMD_GET_FILE_COUNT = 4
CMD_GET_FILE = 5
CMD_GET_DIRECTORY_COUNT = 6
CMD_GET_DIRECTORY = 7
CMD_START_FILE = 8
CMD_READ_FILE  = 9
CMD_END_FILE   = 11
CMD_DELETE = 13
CMD_GET_SPECIAL_PATH_COUNT = 15

PATH_TYPE_INVALID = 0
PATH_TYPE_FILE = 1
PATH_TYPE_DIRECTORY = 2


class CommandBlockBuilder:
    def __init__(self):
        self.buffer = bytearray(BLOCK_SIZE)
        self.offset = 0

    def write32(self, val):
        struct.pack_into('<I', self.buffer, self.offset, val)
        self.offset += 4

    def write64(self, val):
        struct.pack_into('<Q', self.buffer, self.offset, val)
        self.offset += 8

    def write_string(self, val):
        raw = val.encode('utf-8')
        self.write32(len(raw))
        self.buffer[self.offset: self.offset + len(raw)] = raw
        self.offset += len(raw)

    def response_start(self):
        self.write32(OUTPUT_MAGIC)
        self.write32(RESULT_SUCCESS)

    def get_block(self):
        return bytes(self.buffer)


# --- FUNZIONI UTILI ---
def read_string(data, offset):
    """Legge una stringa dal pacchetto Goldleaf"""
    str_len = struct.unpack_from('<I', data, offset)[0]
    raw_bytes = bytes(data[offset+4 : offset+4+str_len])
    # Aggiungiamo .rstrip('\x00') per pulire eventuali byte nulli finali
    val = raw_bytes.decode('utf-8').rstrip('\x00')
    return val, offset + 4 + str_len

def get_dirs(path):
    return vfs_get_dirs(path)

def get_files(path):
    return vfs_get_files(path)


def clean_path(raw_path):
    """Pulisce le stranezze di formattazione che Goldleaf aggiunge ai percorsi"""
    # 1. Rimuove il ':/' finale usato per i finti mount point
    cleaned = raw_path.replace(':/', '/')
    # 2. Rimuove eventuali doppi slash '//'
    cleaned = cleaned.replace('//', '/')
    # 3. Chiede al sistema operativo di normalizzare il percorso finale
    cleaned = os.path.normpath(cleaned)

    # 4. INIEZIONE XCI: Togliamo la maschera. Se Goldleaf chiede un .xci.nsp,
    # noi lavoreremo internamente con il vero .xci
    if cleaned.lower().endswith('.xci.nsp'):
        cleaned = cleaned[:-4]  # Rimuove esattamente la stringa ".nsp"

    return cleaned


def read_virtual_xci(virtualizer, phys_path, offset, size):
    """
    Reads data by mixing the in-RAM PFS0 header with raw NCA bytes from the
    physical XCI file on disk.

    Parameters
    ----------
    virtualizer : VirtualNSPBuilder
        Holds header_bytes and virtual_map built from the XCI's secure partition.
    phys_path : str
        Absolute path to the .xci file on disk.
    offset : int
        Byte offset requested by Goldleaf (virtual coordinate).
    size : int
        Number of bytes requested.
    """
    header_size = len(virtualizer.header_bytes)
    result = bytearray()
    bytes_left = size
    current_offset = offset

    # 1. LETTURA DALLA RAM (Finto Header PFS0)
    if current_offset < header_size:
        read_len = min(bytes_left, header_size - current_offset)
        result += virtualizer.header_bytes[current_offset: current_offset + read_len]
        current_offset += read_len
        bytes_left -= read_len

    # 2. LETTURA DAL DISCO (File XCI fisico)
    if bytes_left > 0:
        try:
            with open(phys_path, 'rb') as xci_file:
                for mapping in virtualizer.virtual_map:
                    if current_offset >= mapping['virtual_start'] and current_offset < mapping['virtual_end']:
                        read_len = min(bytes_left, mapping['virtual_end'] - current_offset)

                        local_offset = current_offset - mapping['virtual_start']
                        physical_read_offset = mapping['physical_start'] + local_offset

                        xci_file.seek(physical_read_offset)
                        chunk = xci_file.read(read_len)
                        result += chunk

                        current_offset += len(chunk)
                        bytes_left -= len(chunk)

                        if bytes_left <= 0:
                            break
        except Exception as e:
            print(f"⚠️ Errore durante la lettura ibrida dell'XCI: {e}")

    return bytes(result)


# --- HANDLER FUNCTIONS (one per opcode) ---

def _handle_get_drive_count(data, resp, dev, ep_out, base_folder, session):
    print("📥 CMD: GetDriveCount")
    resp.response_start()
    resp.write32(1)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_get_drive_info(data, resp, dev, ep_out, base_folder, session):
    drive_idx = struct.unpack_from('<I', data, 8)[0]
    print(f"📥 CMD: GetDriveInfo ({drive_idx})")
    resp.response_start()
    if drive_idx == 0:
        resp.write_string("PyQuark Root")
        resp.write_string(base_folder)
        resp.write64(0)
        resp.write64(0)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_get_special_path_count(data, resp, dev, ep_out, base_folder, session):
    print("📥 CMD: GetSpecialPathCount")
    resp.response_start()
    resp.write32(0)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_get_directory_count(data, resp, dev, ep_out, base_folder, session):
    raw_path, _ = read_string(data, 8)
    path = clean_path(raw_path)
    count = len(get_dirs(path))
    print(f"📁 CMD: GetDirectoryCount({path}) -> {count}")
    resp.response_start()
    resp.write32(count)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_get_directory(data, resp, dev, ep_out, base_folder, session):
    raw_path, next_offset = read_string(data, 8)
    path = clean_path(raw_path)
    idx = struct.unpack_from('<I', data, next_offset)[0]
    dirs = get_dirs(path)
    dir_name = dirs[idx] if idx < len(dirs) else ""
    resp.response_start()
    resp.write_string(dir_name)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_get_file_count(data, resp, dev, ep_out, base_folder, session):
    raw_path, _ = read_string(data, 8)
    path = clean_path(raw_path)
    count = len(get_files(path))
    print(f"📄 CMD: GetFileCount({path}) -> {count}")
    resp.response_start()
    resp.write32(count)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_get_file(data, resp, dev, ep_out, base_folder, session):
    raw_path, next_offset = read_string(data, 8)
    path = clean_path(raw_path)
    idx = struct.unpack_from('<I', data, next_offset)[0]
    files = get_files(path)
    file_name = files[idx] if idx < len(files) else ""
    resp.response_start()
    resp.write_string(file_name)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_stat_path(data, resp, dev, ep_out, base_folder, session):
    raw_path, _ = read_string(data, 8)
    path = clean_path(raw_path)

    # Preleviamo il p_type PRIMA della magia
    p_type, phys_path, internal_path = parse_virtual_path(path)
    path_type, file_size = vfs_stat(path)

    # INIEZIONE XCI: Calcoliamo la dimensione virtuale al volo SOLO per file fisici
    if path.lower().endswith('.xci') and p_type == 'PHYSICAL_FILE':
        # Cache the builder: CMD_START_FILE will reuse it if the key matches.
        if session.active_xci_key != phys_path:
            print(f"🪄 Generazione header virtuale per {os.path.basename(path)}...")
            mapper = XCIMapper(phys_path)
            builder = VirtualNSPBuilder(mapper.secure_files)
            session.active_xci_virtualizer = builder
            session.active_xci_phys_path = phys_path
            session.active_xci_key = phys_path
        else:
            print(f"⚡ [XCI] Cache HIT: riutilizzo builder per {os.path.basename(path)}")
            builder = session.active_xci_virtualizer
        file_size = builder.total_virtual_size
        path_type = PATH_TYPE_FILE  # Assicuriamoci che venga visto come file
    elif path.lower().endswith('.xci') and p_type == 'VIRTUAL_FILE':
        # XCI inside a RAR: kick off (or reuse) background staging, then build
        # the virtual NSP map as soon as the HFS0 header is readable.
        if session.active_xci_key != path:
            print(f"🪄 [VFS+XCI] Avvio staging per XCI nel RAR: {os.path.basename(path)} ...")
            vfs_start_file(path, phys_path, internal_path)  # vfs_start_file forces stage mode
            handle = vfs_get_handle(path)
            if handle is None:
                print("❌ [VFS+XCI] Impossibile ottenere handle VFS, rispondo con dimensione 0")
                path_type = PATH_TYPE_FILE
                file_size = 0
            else:
                ready = handle.wait_for_size(512 * 1024, timeout=60.0)
                if not ready:
                    print("❌ [VFS+XCI] Timeout attesa header XCI, rispondo con dimensione 0")
                    path_type = PATH_TYPE_FILE
                    file_size = 0
                else:
                    staged_path = vfs_get_staged_path(path)
                    print(f"🗺️ [VFS+XCI] Attesa dinamica estrazione partizione secure per: {staged_path}")

                    mapper = None
                    import time

                    # Loop di retry: diamo tempo a unrar di arrivare all'offset giusto (max ~60 secondi)
                    for _ in range(120):
                        if handle._error_event.is_set():
                            print("❌ [VFS+XCI] L'estrazione in background è fallita.")
                            break

                        try:
                            # Proviamo a parsare. Se i dati non ci sono ancora, struct.unpack lancerà un errore
                            mapper = XCIMapper(staged_path)
                            break  # Se non ci sono eccezioni, abbiamo i dati! Usciamo dal loop.
                        except struct.error:
                            # L'errore 'requires a buffer of X bytes' viene intercettato qui.
                            # Dormiamo mezzo secondo e lasciamo lavorare unrar.
                            time.sleep(0.5)

                    if mapper is None:
                        print("❌ [VFS+XCI] Timeout: partizione secure non raggiunta in tempo.")
                        path_type = PATH_TYPE_FILE
                        file_size = 0
                    else:
                        print("✅ [VFS+XCI] Partizione secure mappata con successo!")
                        builder = VirtualNSPBuilder(mapper.secure_files)
                        session.active_xci_virtualizer = builder
                        session.active_xci_phys_path = staged_path
                        session.active_xci_staged_path = staged_path
                        session.active_xci_key = path
                        file_size = builder.total_virtual_size
                        path_type = PATH_TYPE_FILE
        else:
            print(f"⚡ [VFS+XCI] StatPath cache HIT per {os.path.basename(path)}")
            file_size  = session.active_xci_virtualizer.total_virtual_size
            path_type  = PATH_TYPE_FILE

    print(f"🔍 CMD: StatPath({os.path.basename(path)}) -> Tipo: {path_type}, Size: {file_size}")
    resp.response_start()
    resp.write32(path_type)
    resp.write64(file_size)
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_read_file(data, resp, dev, ep_out, base_folder, session):
    raw_path, next_offset = read_string(data, 8)
    path = clean_path(raw_path)
    offset, size = struct.unpack_from('<QQ', data, next_offset)
    print(f"📖 CMD: ReadFile({os.path.basename(path)} | Offset: {offset}, Size: {size})")

    p_type, phys_path, internal_path = parse_virtual_path(path)
    read_data = b''

    # INIEZIONE XCI: Flusso di lettura ibrido (RAM + Disco) per XCI fisico o staged da RAR.
    # session.active_xci_phys_path holds the correct path in both cases (physical file or
    # staged temp_filepath), so read_virtual_xci() works identically for both.
    if path.lower().endswith('.xci') and p_type in ('PHYSICAL_FILE', 'VIRTUAL_FILE') and session.active_xci_virtualizer:
        read_data = read_virtual_xci(session.active_xci_virtualizer, session.active_xci_phys_path, offset, size)

    elif p_type == 'PHYSICAL_FILE':
        try:
            with open(phys_path, 'rb') as f:
                f.seek(offset)
                read_data = f.read(size)
        except Exception as e:
            print(f"⚠️ Errore file fisico: {e}")

    elif p_type == 'VIRTUAL_FILE':
        # FIX: Goldleaf sta sbirciando l'header senza chiamare StartFile!
        if session.active_virtual_path != path:
            print("⚡ Apertura VFS automatica (StartFile saltato da Goldleaf)...")
            vfs_start_file(path, phys_path, internal_path)
            session.active_virtual_path = path

        read_data = vfs_read_file(path, offset, size)

        # Controllo di sicurezza
        if len(read_data) == 0:
            print("⚠️ [VFS] Letti 0 byte! Controlla di avere 'unrar' installato sul sistema.")

    # FASE 1: Inviamo l'header con la dimensione effettiva letta
    resp.response_start()
    resp.write64(len(read_data))
    dev.write(ep_out.bEndpointAddress, resp.get_block())

    # FASE 2: Inviamo i RAW DATA direttamente sul bus USB
    if len(read_data) > 0:
        dev.write(ep_out.bEndpointAddress, read_data)


def _handle_start_file(data, resp, dev, ep_out, base_folder, session):
    raw_path, next_offset = read_string(data, 8)
    path = clean_path(raw_path)
    mode = struct.unpack_from('<I', data, next_offset)[0]  # noqa: F841 – sent by Goldleaf, not used server-side
    print(f"▶️ CMD: StartFile({os.path.basename(path)})")

    p_type, phys_path, internal_path = parse_virtual_path(path)

    # INIEZIONE XCI: Inizializziamo il virtualizzatore in RAM SOLO per file fisici
    if path.lower().endswith('.xci') and p_type == 'PHYSICAL_FILE':
        # Reuse the builder cached by CMD_STAT_PATH when available.
        if session.active_xci_key == phys_path and session.active_xci_virtualizer is not None:
            print(f"⚡ [XCI] StartFile cache HIT: riutilizzo builder per {os.path.basename(path)}")
        else:
            mapper = XCIMapper(phys_path)
            session.active_xci_virtualizer = VirtualNSPBuilder(mapper.secure_files)
            session.active_xci_phys_path = phys_path
            session.active_xci_staged_path = None
            session.active_xci_key = phys_path
        session.active_virtual_path = None
    elif path.lower().endswith('.xci') and p_type == 'VIRTUAL_FILE':
        # Staging was already started (and XCIMapper already run) by CMD_STAT_PATH.
        # Reuse the cached session state; do not re-start extraction.
        if session.active_xci_key == path and session.active_xci_virtualizer is not None:
            print(f"⚡ [VFS+XCI] StartFile cache HIT: builder pronto per {os.path.basename(path)}")
        else:
            # Edge case: StartFile arrived without a preceding CMD_STAT_PATH.
            print(f"🪄 [VFS+XCI] StartFile senza StatPath precedente, avvio staging...")
            vfs_start_file(path, phys_path, internal_path)
            handle = vfs_get_handle(path)
            if handle is not None:
                ready = handle.wait_for_size(512 * 1024, timeout=60.0)
                if ready:
                    staged_path = vfs_get_staged_path(path)
                    mapper = XCIMapper(staged_path)
                    builder = VirtualNSPBuilder(mapper.secure_files)
                    session.active_xci_virtualizer  = builder
                    session.active_xci_phys_path    = staged_path
                    session.active_xci_staged_path  = staged_path
                    session.active_xci_key          = path
        session.active_virtual_path = None
    elif p_type == 'VIRTUAL_FILE':
        vfs_start_file(path, phys_path, internal_path)
        session.active_virtual_path = path
        session.active_xci_virtualizer = None
        session.active_xci_staged_path = None
        session.active_xci_key = None
    else:
        session.active_virtual_path = None
        session.active_xci_virtualizer = None
        session.active_xci_staged_path = None
        session.active_xci_key = None

    resp.response_start()
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_end_file(data, resp, dev, ep_out, base_folder, session):
    mode = struct.unpack_from('<I', data, 8)[0]  # noqa: F841 – sent by Goldleaf, not used server-side
    print("⏹️ CMD: EndFile")

    # Chiudiamo il flusso se stavamo leggendo un RAR
    if session.active_virtual_path:
        vfs_end_file(session.active_virtual_path)

    resp.response_start()
    dev.write(ep_out.bEndpointAddress, resp.get_block())


def _handle_delete(data, resp, dev, ep_out, base_folder, session):
    raw_path, _ = read_string(data, 8)
    path = clean_path(raw_path)
    print(f"🗑️ CMD: Delete({os.path.basename(path)})")

    # Cancelliamo SOLO se è un file fisico, ignoriamo i file nei RAR
    p_type, phys_path, _ = parse_virtual_path(path)
    if p_type in ('PHYSICAL_FILE', 'PHYSICAL_DIR'):
        try:
            if os.path.isfile(phys_path):
                os.remove(phys_path)
            elif os.path.isdir(phys_path):
                os.rmdir(phys_path)
        except Exception as e:
            print(f"⚠️ Errore durante l'eliminazione: {e}")

    resp.response_start()
    dev.write(ep_out.bEndpointAddress, resp.get_block())


# Dispatch table: maps each Goldleaf opcode to its handler function.
_COMMAND_HANDLERS = {
    CMD_GET_DRIVE_COUNT:       _handle_get_drive_count,
    CMD_GET_DRIVE_INFO:        _handle_get_drive_info,
    CMD_GET_SPECIAL_PATH_COUNT: _handle_get_special_path_count,
    CMD_GET_DIRECTORY_COUNT:   _handle_get_directory_count,
    CMD_GET_DIRECTORY:         _handle_get_directory,
    CMD_GET_FILE_COUNT:        _handle_get_file_count,
    CMD_GET_FILE:              _handle_get_file,
    CMD_STAT_PATH:             _handle_stat_path,
    CMD_READ_FILE:             _handle_read_file,
    CMD_START_FILE:            _handle_start_file,
    CMD_END_FILE:              _handle_end_file,
    CMD_DELETE:                _handle_delete,
}


# --- LOOP PRINCIPALE ---
def listen_for_commands(dev, ep_out, ep_in, base_folder):
    session = SessionState()
    print("👂 In ascolto di comandi dalla Switch...")

    while True:
        try:
            data = dev.read(ep_in.bEndpointAddress, BLOCK_SIZE, timeout=1000)

            if len(data) >= 8:
                magic, cmd_id = struct.unpack_from('<II', data, 0)

                if magic == INPUT_MAGIC:
                    handler = _COMMAND_HANDLERS.get(cmd_id)
                    if handler:
                        resp = CommandBlockBuilder()
                        handler(data, resp, dev, ep_out, base_folder, session)
                    else:
                        print(f"⚠️ ATTENZIONE: Ricevuto comando non gestito: {cmd_id}")

                else:
                    print(f"⚠️ Magic sconosciuto: {hex(magic)}")

        except usb.core.USBError as e:
            if e.errno == 110:
                continue
            print(f"❌ Errore USB: {e}")
            break
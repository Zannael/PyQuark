import usb.core
import struct
import os
from src.vfs.core import vfs_get_dirs, vfs_get_files, vfs_stat, parse_virtual_path
from src.vfs.rar_stream import vfs_start_file, vfs_read_file, vfs_end_file
from src.xci_virtualizer import XCIMapper, VirtualNSPBuilder

_ACTIVE_XCI_VIRTUALIZER = None
_ACTIVE_XCI_PHYS_PATH = None
_ACTIVE_VIRTUAL_PATH = None
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


def read_virtual_xci(virtualizer, data_provider, offset, size):
    """
    Legge dati miscelando l'header PFS0 in RAM e i dati NCA richiesti
    tramite la funzione 'data_provider' (che maschera se leggiamo dal disco o dal RAR).
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

    # 2. LETTURA DAL PROVIDER (Disco Fisico o AsyncRarHandle)
    if bytes_left > 0:
        try:
            for mapping in virtualizer.virtual_map:
                if current_offset >= mapping['virtual_start'] and current_offset < mapping['virtual_end']:
                    read_len = min(bytes_left, mapping['virtual_end'] - current_offset)

                    local_offset = current_offset - mapping['virtual_start']
                    physical_read_offset = mapping['physical_start'] + local_offset

                    # ECCO LA MAGIA: Chiamiamo la funzione che ci è stata passata!
                    chunk = data_provider(physical_read_offset, read_len)
                    result += chunk

                    current_offset += len(chunk)
                    bytes_left -= len(chunk)

                    if bytes_left <= 0:
                        break
        except Exception as e:
            print(f"⚠️ Errore durante la lettura ibrida dell'XCI: {e}")

    return bytes(result)


# --- LOOP PRINCIPALE ---
def listen_for_commands(dev, ep_out, ep_in, base_folder):
    # Aggiungi le nuove variabili globali qui
    global _ACTIVE_VIRTUAL_PATH
    global _ACTIVE_XCI_VIRTUALIZER
    global _ACTIVE_XCI_PHYS_PATH
    print("👂 In ascolto di comandi dalla Switch...")

    while True:
        try:
            data = dev.read(ep_in.bEndpointAddress, BLOCK_SIZE, timeout=1000)

            if len(data) >= 8:
                magic, cmd_id = struct.unpack_from('<II', data, 0)

                if magic == INPUT_MAGIC:
                    resp = CommandBlockBuilder()

                    if cmd_id == CMD_GET_DRIVE_COUNT:
                        print(f"📥 CMD: GetDriveCount")
                        resp.response_start()
                        resp.write32(1)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_GET_DRIVE_INFO:
                        drive_idx = struct.unpack_from('<I', data, 8)[0]
                        print(f"📥 CMD: GetDriveInfo ({drive_idx})")
                        resp.response_start()
                        if drive_idx == 0:
                            resp.write_string("PyQuark Root")
                            resp.write_string(base_folder)
                            resp.write64(0)
                            resp.write64(0)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_GET_SPECIAL_PATH_COUNT:
                        print(f"📥 CMD: GetSpecialPathCount")
                        resp.response_start()
                        resp.write32(0)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    # --- NUOVI COMANDI DI ESPLORAZIONE ---

                    elif cmd_id == CMD_GET_DIRECTORY_COUNT:
                        raw_path, _ = read_string(data, 8)
                        path = clean_path(raw_path)
                        count = len(get_dirs(path))
                        print(f"📁 CMD: GetDirectoryCount({path}) -> {count}")
                        resp.response_start()
                        resp.write32(count)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_GET_DIRECTORY:
                        raw_path, next_offset = read_string(data, 8)
                        path = clean_path(raw_path)
                        idx = struct.unpack_from('<I', data, next_offset)[0]
                        dirs = get_dirs(path)
                        dir_name = dirs[idx] if idx < len(dirs) else ""
                        resp.response_start()
                        resp.write_string(dir_name)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_GET_FILE_COUNT:
                        raw_path, _ = read_string(data, 8)
                        path = clean_path(raw_path)
                        count = len(get_files(path))
                        print(f"📄 CMD: GetFileCount({path}) -> {count}")
                        resp.response_start()
                        resp.write32(count)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_GET_FILE:
                        raw_path, next_offset = read_string(data, 8)
                        path = clean_path(raw_path)
                        idx = struct.unpack_from('<I', data, next_offset)[0]
                        files = get_files(path)
                        file_name = files[idx] if idx < len(files) else ""
                        resp.response_start()
                        resp.write_string(file_name)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_STAT_PATH:
                        raw_path, _ = read_string(data, 8)
                        path = clean_path(raw_path)

                        # Preleviamo il p_type PRIMA della magia
                        p_type, phys_path, internal_path = parse_virtual_path(path)
                        path_type, file_size = vfs_stat(path)

                        # INIEZIONE XCI: Calcoliamo la dimensione virtuale al volo SOLO per file fisici
                        if path.lower().endswith('.xci') and p_type == 'PHYSICAL_FILE':
                            print(f"🪄 Generazione header virtuale per {os.path.basename(path)}...")
                            mapper = XCIMapper(phys_path)
                            builder = VirtualNSPBuilder(mapper.secure_files)
                            file_size = builder.total_virtual_size
                            path_type = PATH_TYPE_FILE  # Assicuriamoci che venga visto come file
                        elif path.lower().endswith('.xci') and p_type == 'VIRTUAL_FILE':
                            print(f"⚠️ Salto XCI in RAR: {os.path.basename(path)} (Non ancora supportato)")

                        print(f"🔍 CMD: StatPath({os.path.basename(path)}) -> Tipo: {path_type}, Size: {file_size}")
                        resp.response_start()
                        resp.write32(path_type)
                        resp.write64(file_size)
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    # --- NUOVI COMANDI DI LETTURA FILE ---

                    elif cmd_id == CMD_READ_FILE:
                        raw_path, next_offset = read_string(data, 8)
                        path = clean_path(raw_path)
                        offset, size = struct.unpack_from('<QQ', data, next_offset)
                        print(f"📖 CMD: ReadFile({os.path.basename(path)} | Offset: {offset}, Size: {size})")

                        p_type, phys_path, internal_path = parse_virtual_path(path)
                        read_data = b''

                        # INIEZIONE XCI: Flusso di lettura ibrido (RAM + Disco) SOLO su disco fisico
                        if path.lower().endswith('.xci') and p_type == 'PHYSICAL_FILE' and _ACTIVE_XCI_VIRTUALIZER:
                            read_data = read_virtual_xci(_ACTIVE_XCI_VIRTUALIZER, _ACTIVE_XCI_PHYS_PATH, offset, size)

                        elif p_type == 'PHYSICAL_FILE':
                            try:
                                with open(phys_path, 'rb') as f:
                                    f.seek(offset)
                                    read_data = f.read(size)
                            except Exception as e:
                                print(f"⚠️ Errore file fisico: {e}")

                        elif p_type == 'VIRTUAL_FILE':
                            # FIX: Goldleaf sta sbirciando l'header senza chiamare StartFile!
                            if _ACTIVE_VIRTUAL_PATH != path:
                                print("⚡ Apertura VFS automatica (StartFile saltato da Goldleaf)...")
                                vfs_start_file(path, phys_path, internal_path)
                                _ACTIVE_VIRTUAL_PATH = path

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

                    elif cmd_id == CMD_START_FILE:
                        raw_path, next_offset = read_string(data, 8)
                        path = clean_path(raw_path)
                        mode = struct.unpack_from('<I', data, next_offset)[0]
                        print(f"▶️ CMD: StartFile({os.path.basename(path)})")

                        p_type, phys_path, internal_path = parse_virtual_path(path)

                        # INIEZIONE XCI: Inizializziamo il virtualizzatore in RAM SOLO per file fisici
                        if path.lower().endswith('.xci') and p_type == 'PHYSICAL_FILE':
                            mapper = XCIMapper(phys_path)
                            _ACTIVE_XCI_VIRTUALIZER = VirtualNSPBuilder(mapper.secure_files)
                            _ACTIVE_XCI_PHYS_PATH = phys_path
                            _ACTIVE_VIRTUAL_PATH = None
                        elif p_type == 'VIRTUAL_FILE':
                            vfs_start_file(path, phys_path, internal_path)
                            _ACTIVE_VIRTUAL_PATH = path
                            _ACTIVE_XCI_VIRTUALIZER = None
                        else:
                            _ACTIVE_VIRTUAL_PATH = None
                            _ACTIVE_XCI_VIRTUALIZER = None

                        resp.response_start()
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_END_FILE:
                        # RIMOSSO: global _ACTIVE_VIRTUAL_PATH
                        mode = struct.unpack_from('<I', data, 8)[0]
                        print(f"⏹️ CMD: EndFile")

                        # Chiudiamo il flusso se stavamo leggendo un RAR
                        if _ACTIVE_VIRTUAL_PATH:
                            vfs_end_file(_ACTIVE_VIRTUAL_PATH)

                        resp.response_start()
                        dev.write(ep_out.bEndpointAddress, resp.get_block())

                    elif cmd_id == CMD_DELETE:
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

                    else:
                        print(f"⚠️ ATTENZIONE: Ricevuto comando non gestito: {cmd_id}")

                else:
                    print(f"⚠️ Magic sconosciuto: {hex(magic)}")

        except usb.core.USBError as e:
            if e.errno == 110:
                continue
            print(f"❌ Errore USB: {e}")
            break
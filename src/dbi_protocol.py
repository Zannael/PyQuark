import struct
import os
import usb.core
from src.vfs.core import vfs_get_dirs, vfs_get_files, parse_virtual_path
from src.vfs.rar_stream import vfs_start_file, vfs_read_file

# --- COSTANTI DBI ---
CMD_ID_EXIT = 0
CMD_ID_FILE_RANGE = 2
CMD_ID_LIST = 3

CMD_TYPE_REQUEST = 0
CMD_TYPE_RESPONSE = 1
CMD_TYPE_ACK = 2

BUFFER_SEGMENT_DATA_SIZE = 0x100000  # Chunk da 1MB


def build_dbi_file_map(base_folder):
    """
    Usa il VFS di PyQuark per esplorare cartelle e RAR.
    Restituisce un dict: { "Giochi/Zelda.xci": "/percorso/reale/o/virtuale/Zelda.xci" }
    """
    file_map = {}

    def scan_dir(current_vpath, rel_path=""):
        # Cerca i file
        for f in vfs_get_files(current_vpath):
            # Rimuoviamo la maschera .nsp di Goldleaf se presente
            real_name = f[:-4] if f.lower().endswith('.xci.nsp') else f

            if real_name.lower().endswith(('.nsp', '.xci', '.nsz', '.xcz')):
                # 🎯 FIX CRITICO: Costruiamo il full_vpath usando real_name, non la f mascherata!
                full_vpath = os.path.join(current_vpath, real_name).replace('\\', '/')
                display_name = os.path.join(rel_path, real_name).replace('\\', '/')
                file_map[display_name] = full_vpath

        # Esplora le sottocartelle e i file RAR
        for d in vfs_get_dirs(current_vpath):
            scan_dir(os.path.join(current_vpath, d), os.path.join(rel_path, d))

    scan_dir(base_folder)
    return file_map


def process_list_command(dev, ep_in, ep_out, file_map):
    print("📋 [DBI] Richiesta lista file ricevuta...")

    nsp_path_list = "\n".join(file_map.keys()) + "\n" if file_map else ""
    nsp_path_list_bytes = nsp_path_list.encode('utf-8')
    nsp_path_list_len = len(nsp_path_list_bytes)

    dev.write(ep_out.bEndpointAddress,
              struct.pack('<4sIII', b'DBI0', CMD_TYPE_RESPONSE, CMD_ID_LIST, nsp_path_list_len))

    if nsp_path_list_len > 0:
        dev.read(ep_in.bEndpointAddress, 16, timeout=2000)  # Attende ACK
        dev.write(ep_out.bEndpointAddress, nsp_path_list_bytes)


def process_file_range_command(dev, ep_in, ep_out, data_size, file_map):
    dev.write(ep_out.bEndpointAddress, struct.pack('<4sIII', b'DBI0', CMD_TYPE_ACK, CMD_ID_FILE_RANGE, data_size))

    file_range_header = dev.read(ep_in.bEndpointAddress, data_size, timeout=2000)
    range_size = struct.unpack('<I', file_range_header[:4])[0]
    range_offset = struct.unpack('<Q', file_range_header[4:12])[0]
    nsp_name = bytes(file_range_header[16:]).decode('utf-8').rstrip('\x00')

    print(f"📖 [DBI] Lettura: {os.path.basename(nsp_name)} (Offset: {range_offset}, Dim: {range_size})")

    response_bytes = struct.pack('<4sIII', b'DBI0', CMD_TYPE_RESPONSE, CMD_ID_FILE_RANGE, range_size)
    dev.write(ep_out.bEndpointAddress, response_bytes)
    dev.read(ep_in.bEndpointAddress, 16, timeout=2000)  # Attende ACK

    vpath = file_map.get(nsp_name)
    if not vpath:
        print(f"❌ [DBI] Errore: '{nsp_name}' non trovato.")
        return

    p_type, phys_path, internal_path = parse_virtual_path(vpath)

    # Se il file è dentro un RAR, vfs_start_file scatena lo staging in background
    if p_type == 'VIRTUAL_FILE':
        vfs_start_file(vpath, phys_path, internal_path)

    curr_off = range_offset
    end_off = range_offset + range_size

    # Loop di streaming ultra-veloce a blocchi da 1MB
    while curr_off < end_off:
        read_size = min(BUFFER_SEGMENT_DATA_SIZE, end_off - curr_off)

        if p_type == 'PHYSICAL_FILE':
            with open(phys_path, 'rb') as f:
                f.seek(curr_off)
                buf = f.read(read_size)
        elif p_type == 'VIRTUAL_FILE':
            buf = vfs_read_file(vpath, curr_off, read_size)
        else:
            break

        if not buf:
            print("⚠️ [DBI] Attenzione: buffer vuoto, interrompo lo stream.")
            break

        dev.write(ep_out.bEndpointAddress, data=buf, timeout=0)
        curr_off += len(buf)
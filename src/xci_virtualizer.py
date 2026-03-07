import struct


class XCIMapper:
    """
    Legge un file XCI e mappa gli offset fisici assoluti
    di tutti i file contenuti nella partizione 'secure'.
    """

    def __init__(self, filepath):
        self.filepath = filepath
        # Conterrà tuple: (nome_file, offset_fisico_assoluto, dimensione)
        self.secure_files = []
        self._parse()

    def _parse(self):
        with open(self.filepath, 'rb') as f:
            # 1. Trova il Root HFS0 (metodo euristico ultra-rapido)
            f.seek(0)
            # Leggiamo i primi 512KB (l'header root è sempre all'inizio)
            header_chunk = f.read(1024 * 512)
            root_hfs0_offset = header_chunk.find(b'HFS0')

            if root_hfs0_offset == -1:
                raise ValueError("Partizione Root HFS0 non trovata! File XCI corrotto o non supportato.")

            # 2. Leggi il Root HFS0 per trovare la partizione 'secure'
            secure_offset = self._find_partition_offset(f, root_hfs0_offset, "secure")
            if not secure_offset:
                raise ValueError("Partizione 'secure' non trovata nell'XCI!")

            # 3. Mappa il contenuto del Secure HFS0
            self.secure_files = self._parse_hfs0(f, secure_offset)

    def _find_partition_offset(self, f, hfs0_offset, target_name):
        files = self._parse_hfs0(f, hfs0_offset)
        for name, absolute_offset, size in files:
            if name == target_name:
                return absolute_offset
        return None

    def _parse_hfs0(self, f, hfs0_offset):
        f.seek(hfs0_offset)

        # --- PARSING HEADER ---
        # 4s = Stringa di 4 byte (HFS0)
        # I  = uint32 (numero di file)
        # I  = uint32 (dimensione string table)
        # I  = uint32 (reserved/padding)
        header = f.read(16)
        magic, num_files, string_table_size, _ = struct.unpack('<4sIII', header)

        if magic != b'HFS0':
            return []

        # --- LETTURA ENTRIES E STRING TABLE ---
        entries_size = num_files * 64
        entries_data = f.read(entries_size)
        string_table = f.read(string_table_size)

        # L'area dei dati veri e propri inizia rigorosamente dopo la string table
        data_start_offset = hfs0_offset + 16 + entries_size + string_table_size

        files = []
        for i in range(num_files):
            entry_offset = i * 64
            entry = entries_data[entry_offset: entry_offset + 64]

            # Decodifichiamo i primi 20 byte dell'entry per avere offset, size e puntatore al nome
            # Q = uint64 (data_offset)
            # Q = uint64 (size)
            # I = uint32 (name_offset)
            data_offset, size, name_offset = struct.unpack('<QQI', entry[:20])

            # Estrai il nome dalla string table (leggiamo fino al byte nullo \x00)
            name_end = string_table.find(b'\x00', name_offset)
            if name_end == -1:
                name_end = len(string_table)
            name = string_table[name_offset:name_end].decode('utf-8')

            # Calcolo finale: l'offset all'interno del file XCI globale sul disco
            absolute_offset = data_start_offset + data_offset
            files.append((name, absolute_offset, size))

        return files


class VirtualNSPBuilder:
    """
    Costruisce un header PFS0 (NSP) finto in memoria partendo
    dalla lista dei file estratti dalla partizione secure dell'XCI.
    """

    def __init__(self, secure_files):
        # secure_files = lista di tuple: (nome_file, offset_fisico, dimensione)
        self.secure_files = secure_files
        self.header_bytes = b""
        self.total_virtual_size = 0
        self.virtual_map = []  # Fondamentale per la Fase C!

        self._build()

    def _build(self):
        file_count = len(self.secure_files)

        # 1. Costruiamo la String Table (nomi dei file separati da \x00)
        string_table = b""
        name_offsets = []
        for name, _, _ in self.secure_files:
            name_offsets.append(len(string_table))
            string_table += name.encode('utf-8') + b'\x00'

        # Gli header Switch amano l'allineamento a 16 byte. Aggiungiamo padding se serve.
        padding_len = (16 - (len(string_table) % 16)) % 16
        string_table += b'\x00' * padding_len
        string_table_size = len(string_table)

        # 2. Calcoliamo la dimensione totale dell'header finto
        # 16 byte (Magic) + (24 byte * numero file) + String Table
        header_size = 16 + (24 * file_count) + string_table_size

        # 3. Assembliamo l'header!
        header = bytearray()
        header += b'PFS0'  # Magic
        header += struct.pack('<I', file_count)  # Num file
        header += struct.pack('<I', string_table_size)  # Size string table
        header += struct.pack('<I', 0)  # Padding

        current_data_offset = 0

        # 4. Compiliamo le File Entries (24 byte l'una)
        for i, (name, phys_offset, size) in enumerate(self.secure_files):
            header += struct.pack('<Q', current_data_offset)  # Offset dati (relativo all'inizio dei dati)
            header += struct.pack('<Q', size)  # Dimensione file
            header += struct.pack('<I', name_offsets[i])  # Offset del nome nella string table
            header += struct.pack('<I', 0)  # Padding

            # --- LA MAPPA VIRTUALE ---
            # Salviamo esattamente dove inizia e finisce questo file nel nostro finto NSP.
            # Questo ci salverà la vita quando Goldleaf farà richieste di lettura casuali!
            virtual_start = header_size + current_data_offset
            self.virtual_map.append({
                'name': name,
                'virtual_start': virtual_start,
                'virtual_end': virtual_start + size,
                'physical_start': phys_offset,
                'size': size
            })

            current_data_offset += size

        header += string_table

        self.header_bytes = bytes(header)
        # La dimensione magica che daremo a Goldleaf in StatPath!
        self.total_virtual_size = header_size + current_data_offset


# --- TEST DELLA FASE B ---
if __name__ == "__main__":
    # Simulo i dati che hai appena estratto da Zelda
    test_files = [
        ("9d827368992a2faafa9062aa5458b7ec.nca", 0x17018200, 17149362176),
        ("491f62797fd5bc9b6c218d00c8b6f8a0.nca", 0x415288200, 1540096),
        ("e4051ab2cf9d8df3798b17bd754abc9c.nca", 0x415400200, 230400),
        ("ce6fe3b3db29b6853ce8963bda188e97.cnmt.nca", 0x415438a00, 1024)  # Approssimato per il test
    ]

    builder = VirtualNSPBuilder(test_files)
    print("🪄 Illusione creata!")
    print(f"📦 Dimensione Header in RAM: {len(builder.header_bytes)} bytes")
    print(
        f"🌐 Dimensione Totale Finto NSP: {builder.total_virtual_size} bytes ({builder.total_virtual_size / (1024 ** 3):.2f} GB)")
    print("\n🗺️ Tabella di Routing Virtuale (Fase C):")
    for mapping in builder.virtual_map:
        print(
            f"   -> Se Goldleaf chiede da {mapping['virtual_start']} a {mapping['virtual_end']}, leggi XCI da {hex(mapping['physical_start'])}")
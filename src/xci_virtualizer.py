import struct


class XCIMapper:
    """
    Reads an XCI file and maps the absolute physical offsets
    of all files contained in the 'secure' partition.
    """

    def __init__(self, filepath):
        self.filepath = filepath
        # Will contain tuples: (file_name, absolute_physical_offset, size)
        self.secure_files = []
        self._parse()

    def _parse(self):
        with open(self.filepath, 'rb') as f:
            # 1. Find the Root HFS0 (ultra-fast heuristic method)
            f.seek(0)
            # We read the first 512KB (the root header is always at the beginning)
            header_chunk = f.read(1024 * 512)
            root_hfs0_offset = header_chunk.find(b'HFS0')

            if root_hfs0_offset == -1:
                raise ValueError("Root HFS0 partition not found! Corrupted or unsupported XCI file.")

            # 2. Read the Root HFS0 to find the 'secure' partition
            secure_offset = self._find_partition_offset(f, root_hfs0_offset, "secure")
            if not secure_offset:
                raise ValueError("'secure' partition not found in the XCI!")

            # 3. Map the contents of the Secure HFS0
            self.secure_files = self._parse_hfs0(f, secure_offset)

    def _find_partition_offset(self, f, hfs0_offset, target_name):
        files = self._parse_hfs0(f, hfs0_offset)
        for name, absolute_offset, size in files:
            if name == target_name:
                return absolute_offset
        return None

    def _parse_hfs0(self, f, hfs0_offset):
        f.seek(hfs0_offset)

        # --- HEADER PARSING ---
        # 4s = 4-byte string (HFS0)
        # I  = uint32 (number of files)
        # I  = uint32 (string table size)
        # I  = uint32 (reserved/padding)
        header = f.read(16)
        magic, num_files, string_table_size, _ = struct.unpack('<4sIII', header)

        if magic != b'HFS0':
            return []

        # --- READ ENTRIES AND STRING TABLE ---
        entries_size = num_files * 64
        entries_data = f.read(entries_size)
        string_table = f.read(string_table_size)

        # The actual data area starts strictly after the string table
        data_start_offset = hfs0_offset + 16 + entries_size + string_table_size

        files = []
        for i in range(num_files):
            entry_offset = i * 64
            entry = entries_data[entry_offset: entry_offset + 64]

            # We decode the first 20 bytes of the entry to get offset, size, and name pointer
            # Q = uint64 (data_offset)
            # Q = uint64 (size)
            # I = uint32 (name_offset)
            data_offset, size, name_offset = struct.unpack('<QQI', entry[:20])

            # Extract the name from the string table (we read until the null byte \x00)
            name_end = string_table.find(b'\x00', name_offset)
            if name_end == -1:
                name_end = len(string_table)
            name = string_table[name_offset:name_end].decode('utf-8')

            # Final calculation: the offset inside the global XCI file on disk
            absolute_offset = data_start_offset + data_offset
            files.append((name, absolute_offset, size))

        return files


class VirtualNSPBuilder:
    """
    Builds a fake PFS0 (NSP) header in memory starting
    from the list of files extracted from the XCI secure partition.
    """

    def __init__(self, secure_files):
        # secure_files = list of tuples: (file_name, physical_offset, size)
        self.secure_files = secure_files
        self.header_bytes = b""
        self.total_virtual_size = 0
        self.virtual_map = []  # Essential for Phase C!

        self._build()

    def _build(self):
        file_count = len(self.secure_files)

        # 1. Build the String Table (file names separated by \x00)
        string_table = b""
        name_offsets = []
        for name, _, _ in self.secure_files:
            name_offsets.append(len(string_table))
            string_table += name.encode('utf-8') + b'\x00'

        # Switch headers like 16-byte alignment. Add padding if needed.
        padding_len = (16 - (len(string_table) % 16)) % 16
        string_table += b'\x00' * padding_len
        string_table_size = len(string_table)

        # 2. Calculate the total size of the fake header
        # 16 bytes (Magic) + (24 bytes * number of files) + String Table
        header_size = 16 + (24 * file_count) + string_table_size

        # 3. Assemble the header!
        header = bytearray()
        header += b'PFS0'  # Magic
        header += struct.pack('<I', file_count)  # Num file
        header += struct.pack('<I', string_table_size)  # String table size
        header += struct.pack('<I', 0)  # Padding

        current_data_offset = 0

        # 4. Fill the File Entries (24 bytes each)
        for i, (name, phys_offset, size) in enumerate(self.secure_files):
            header += struct.pack('<Q', current_data_offset)  # Data offset (relative to the start of the data)
            header += struct.pack('<Q', size)  # File size
            header += struct.pack('<I', name_offsets[i])  # Name offset in the string table
            header += struct.pack('<I', 0)  # Padding

            # --- THE VIRTUAL MAP ---
            # We save exactly where this file starts and ends in our fake NSP.
            # This will save our life when Goldleaf makes random read requests!
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
        # The magic size we will give to Goldleaf in StatPath!
        self.total_virtual_size = header_size + current_data_offset
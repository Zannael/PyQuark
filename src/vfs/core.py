import os
import rarfile
import re

PATH_TYPE_INVALID = 0
PATH_TYPE_FILE = 1
PATH_TYPE_DIRECTORY = 2

_RAR_METADATA_CACHE = {}


def is_primary_rar(filename):
    """
    Determina se un file RAR è il volume principale da mostrare.
    Nasconde le parti secondarie come .part02.rar, .part03.rar, .r00, .r01, ecc.
    """
    lower_name = filename.lower()

    # Se non è un rar, lo scartiamo
    if not lower_name.endswith('.rar'):
        # Gestiamo anche il vecchio formato .r00, .r01 che vanno nascosti
        if re.search(r'\.r\d{2}$', lower_name):
            return False
        return False

    # Controllo per il formato .partXX.rar
    match = re.search(r'\.part(\d+)\.rar$', lower_name)
    if match:
        part_num = int(match.group(1))
        # È il file primario solo se è la parte 1
        return part_num == 1

    return True  # Se è un semplice .rar, è il primario


def get_rar_metadata(rar_path):
    # [Resto del codice invariato...]
    if rar_path in _RAR_METADATA_CACHE:
        return _RAR_METADATA_CACHE[rar_path]

    try:
        # rarfile, se configurato bene, gestirà il multi-volume dal primo file
        with rarfile.RarFile(rar_path) as rf:
            info_list = rf.infolist()
            files = [f.filename for f in info_list if not f.isdir()]
            sizes = {f.filename: f.file_size for f in info_list if not f.isdir()}

            _RAR_METADATA_CACHE[rar_path] = {'files': files, 'sizes': sizes}
            return _RAR_METADATA_CACHE[rar_path]
    except Exception as e:
        print(f"⚠️ Errore durante l'analisi dell'archivio {rar_path}: {e}")
        return {'files': [], 'sizes': {}}


def parse_virtual_path(path):
    # [Resto del codice invariato...]
    if os.path.exists(path):
        if os.path.isdir(path):
            return ('PHYSICAL_DIR', path, None)
        elif os.path.isfile(path):
            if is_primary_rar(path):
                return ('RAR_AS_DIR', path, None)
            else:
                return ('PHYSICAL_FILE', path, None)

    parts = path.split(os.sep)
    for i in range(len(parts)):
        potential_rar = os.sep.join(parts[:i + 1])
        if is_primary_rar(potential_rar) and os.path.isfile(potential_rar):
            internal_path = '/'.join(parts[i + 1:])
            return ('VIRTUAL_FILE', potential_rar, internal_path)

    return ('INVALID', None, None)


def vfs_get_dirs(path):
    path_type, phys_path, _ = parse_virtual_path(path)

    if path_type == 'PHYSICAL_DIR':
        try:
            items = os.listdir(phys_path)
            dirs = [d for d in items if os.path.isdir(os.path.join(phys_path, d))]
            # FILTRO APPLICATO QUI: Solo i file .rar "master" diventano cartelle
            rars = [f for f in items if os.path.isfile(os.path.join(phys_path, f)) and is_primary_rar(f)]
            return sorted(dirs + rars)
        except Exception:
            return []

    return []


def vfs_get_files(path):
    """Restituisce file fisici (esclusi i .rar) o i file contenuti in un RAR."""
    path_type, phys_path, _ = parse_virtual_path(path)

    if path_type == 'PHYSICAL_DIR':
        try:
            items = os.listdir(phys_path)
            files = []
            for f in items:
                if os.path.isfile(os.path.join(phys_path, f)) and not f.lower().endswith('.rar'):
                    # INIEZIONE XCI: Camuffiamo il file per accontentare Goldleaf
                    if f.lower().endswith('.xci'):
                        files.append(f + '.nsp')
                    else:
                        files.append(f)
            return sorted(files)
        except Exception:
            return []

    elif path_type == 'RAR_AS_DIR':
        # Goldleaf è "entrato" nel RAR, mostriamo il contenuto virtuale
        metadata = get_rar_metadata(phys_path)
        return sorted(metadata['files'])

    return []


def vfs_stat(path):
    """Restituisce il tipo di percorso e la sua dimensione reale (decompressa)."""
    path_type, phys_path, internal_path = parse_virtual_path(path)

    if path_type in ('PHYSICAL_DIR', 'RAR_AS_DIR'):
        return PATH_TYPE_DIRECTORY, 0

    elif path_type == 'PHYSICAL_FILE':
        return PATH_TYPE_FILE, os.path.getsize(phys_path)

    elif path_type == 'VIRTUAL_FILE':
        metadata = get_rar_metadata(phys_path)
        size = metadata['sizes'].get(internal_path, 0)
        return PATH_TYPE_FILE, size

    return PATH_TYPE_INVALID, 0


def is_rar_multipart(rar_path):
    lower = rar_path.lower()

    # Caso .part1.rar
    if re.search(r'\.part0*1\.rar$', lower):
        return True

    # Controllo old-style .rar + .r00/.r01 accanto
    folder = os.path.dirname(rar_path)
    base = os.path.splitext(os.path.basename(rar_path))[0].lower()

    try:
        for name in os.listdir(folder):
            lname = name.lower()
            if re.fullmatch(re.escape(base) + r'\.r\d{2}', lname):
                return True
    except Exception:
        pass

    return False
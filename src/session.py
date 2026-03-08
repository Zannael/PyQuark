"""
Session state for a single PyQuark command-serve loop.

Instead of scattered module-level globals in protocol.py, all per-session
mutable state is collected here.  One SessionState instance is created at
the start of listen_for_commands() and lives for its entire duration.

Fields
------
active_xci_virtualizer : VirtualNSPBuilder | None
    The in-memory PFS0 header builder for the XCI file currently being served.
    Set on CMD_START_FILE (XCI branch) or cached from CMD_STAT_PATH.

active_xci_phys_path : str | None
    Absolute path to the .xci file used by read_virtual_xci() to read NCA
    data.  For a physical XCI this is the on-disk path; for an XCI inside a
    RAR this is the temp_filepath of the RarVirtualHandle (i.e. the staged
    extraction path).

active_xci_staged_path : str | None
    Absolute path to the staged temporary file when an XCI was extracted
    from inside a RAR archive.  Mirrors active_xci_phys_path in that case.
    None when serving a physical XCI directly.

active_xci_key : str | None
    Virtual path key (Goldleaf-visible) for which active_xci_virtualizer was
    most recently built.  Allows CMD_STAT_PATH to cache the VirtualNSPBuilder
    so that the immediately-following CMD_START_FILE can reuse it.

active_virtual_path : str | None
    The virtual path (i.e. the path as seen by Goldleaf) of the RAR-backed
    file currently being served.  Used by CMD_READ_FILE to detect when
    Goldleaf reads without a preceding CMD_START_FILE, and by CMD_END_FILE
    to delegate to vfs_end_file().
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionState:
    active_xci_virtualizer: Optional[object] = field(default=None)
    active_xci_phys_path: Optional[str] = field(default=None)
    active_xci_staged_path: Optional[str] = field(default=None)
    active_xci_key: Optional[str] = field(default=None)
    active_virtual_path: Optional[str] = field(default=None)

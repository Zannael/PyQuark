# PyQuark

**PyQuark** is a remote file server for the Nintendo Switch, designed to communicate with both **[Goldleaf](https://github.com/XorTroll/Goldleaf)** (via the **Quark USB protocol**) and **[DBI](https://github.com/rashevskyv/dbi)** (via the **DBI0 protocol**).

Unlike standard servers, PyQuark features a **Virtual File System (VFS)** that allows you to install games directly from compressed archives and cartridge dumps (`.xci`). This completely bypasses the need to manually extract or convert files on your computer before installation.

---

## Key Features

### **Dual Protocol Support**

PyQuark automatically detects the connected client and adapts its behavior accordingly:

* **Goldleaf Support**: uses the classic **Quark protocol** for NSP files and virtualized XCI installation.
* **DBI Support**: implements the **DBI0 protocol** for high-speed, native installation of both XCI and NSP files, without metadata spoofing.

This makes PyQuark a flexible USB installation server that can work seamlessly with two of the most widely used Nintendo Switch homebrew installers.

### **XCI Virtualization & Native Streaming**

PyQuark supports `.xci` files in two different ways depending on the client.

#### **For Goldleaf**

`.xci` files are **virtually transformed into installable `.nsp` files on the fly**.
The system builds a fake **PFS0 header** in memory and maps the **NCA** data by reading directly from the physical offsets within the original XCI file.

This allows Goldleaf to install the base game **without ever rewriting or converting the file to disk**.

#### **For DBI**

`.xci` files are streamed **natively**, so the console recognizes them as proper **GameCard installs**.

This avoids the metadata rewriting tricks required for Goldleaf and provides a more robust installation path for cartridge dumps.

### **Native RAR Support**

PyQuark treats `.rar` archives — including **multi-volume archives** such as `.part1.rar`, `.part2.rar`, etc. — as if they were standard, navigable folders.

From the console's perspective, these archives appear as normal directories that can be browsed directly, allowing the user to install files stored inside them **without manual extraction**.

### **Hybrid Streaming and Staging**

PyQuark uses two different strategies depending on file type and access needs.

#### **Streaming**

Lightweight files are read and extracted in chunks, then sent directly over the USB bus.

#### **Staging**

Large files — or files that require random access, such as `.xci` images — are extracted in the background to a temporary cache.

This system includes:

* dynamic wait times for read operations
* smart cache management
* automatic cleanup of multi-gigabyte temporary files when the session closes

This hybrid approach makes it possible to balance memory usage, responsiveness, and compatibility.

### **Stateful Session Management**

PyQuark keeps track of active paths, archive handles, and virtualization cache state during a session.

This improves stability when the console performs fragmented reads or repeatedly requests data from complex virtualized sources.

## Technical Architecture

The project is structured into modular layers, each responsible for a specific part of the pipeline:

| Component            | Responsibility                                                                                |
| -------------------- | --------------------------------------------------------------------------------------------- |
| `transport.py`       | Low-level USB communication via **PyUSB**                                                     |
| `protocol.py`        | Dispatcher that identifies the connected client (**Goldleaf** vs **DBI**) and routes commands |
| `dbi_protocol.py`    | Handles **DBI-specific** handshakes and 1 MB chunk streaming                                  |
| `core.py`            | Virtual File System layer, path abstraction, and file extension masking                       |
| `xci_virtualizer.py` | Parses **HFS0** and generates on-the-fly virtual **NSP headers** for Goldleaf                 |
| `rar_stream.py`      | Manages background threads for asynchronous RAR extraction, staging, and cache cleanup        |

## Requirements

To run **PyQuark**, make sure you have the following:

* **Python 3.10+**
* The required Python libraries:

  * `pyusb`
  * `rarfile`
  * `multivolumefile`
* The **unrar** system utility installed and accessible from your system `PATH`
* A **Nintendo Switch** with **Goldleaf** or **DBI** installed
* A **good USB-C cable**

### **Windows only**

* **Zadig** (or an equivalent tool) may be required to install compatible **WinUSB/libusb** drivers for the Nintendo Switch

> **Important:** `unrar` is required for the archive extraction and staging engine to function correctly.

## Installation

Clone the repository and install the required dependencies:

```
git clone https://github.com/Zannael/PyQuark.git
cd PyQuark
pip install -r requirements.txt
```

Make sure the `unrar` executable is installed and available in your `PATH`.

## AppImage (Linux GUI)

PyQuark can be packaged as a GUI-only AppImage (`quark_gui.py`) using the local virtual environment.

### Requirements

* Linux `x86_64`
* `unrar` installed on the target system (not bundled in AppImage)
* Build dependencies: `curl` or `wget`, `git`

### Build (local machine)

```bash
scripts/build-appimage.sh
```

### Build (compatibility mode via Docker)

To improve portability across distributions, build in a controlled Ubuntu container:

```bash
scripts/build-appimage-compat.sh
```

The generated file is written under `dist/` as `PyQuark-<version>-x86_64.AppImage`.

### Run

```bash
chmod +x dist/PyQuark-*.AppImage
./dist/PyQuark-*.AppImage
```

## Usage

1. Configure the folder you want to share inside `main.py`
2. Connect your Nintendo Switch via USB
3. Launch **Goldleaf** or **DBI** on the console
4. Start the server:

   python main.py

### **Using DBI**

In **DBI**, select:

```
Install via DBI backend
```

And only then start the program! Your `.rar`, `.nsp`, and `.xci` files should appear directly in the file browser.

### **Using Goldleaf**

In **Goldleaf**, browse the shared USB content as usual. As long as you opened the homebrew app, you are free to launch `main.py` whenever you like.
Virtualized `.xci` files will be exposed as installable content through the Quark-compatible layer.

## Compatibility & Critical Notes

PyQuark was originally developed around the Goldleaf workflow, but now also supports DBI for a more native and robust installation for XCI files.

### **⚠️ Important: XCI Installation Warning**

While PyQuark supports both clients, the behavior differs significantly depending on the installer used:

* **Goldleaf**: installing an XCI as a **virtualized NSP** works for the **base game**
* however, installing **subsequent NSP updates** may cause the game to fail to launch due to **metadata/signature conflicts** (for example, Digital Ticketless mismatch)

### **Recommendation**

For `.xci` installations, **DBI is strongly recommended**.

DBI handles XCI files **natively**, which helps prevent later issues such as:

* update incompatibility
* launch failures
* “corrupted data” errors after installing NSP updates

If you only need to install the base content and accept the limitations, Goldleaf virtualization remains available.
If you want the safest and most future-proof workflow, use **DBI**.

## Performance Notes

Installation speed depends on:

* your computer hardware
* USB transfer speed
* archive size
* whether the file can be streamed directly or must be staged first

On older or low-power systems, on-the-fly extraction from archives may be slow. During these operations, the console interface may temporarily freeze while waiting for data.

As long as the terminal does not show an error, the transfer may still be progressing normally.

## Windows Notes

Running the project natively on Windows may require a few extra adjustments.

### **1. `unrar` system calls**

The `subprocess` module expects the `unrar` command to be available.

On Windows, you may need to specify the absolute path to `UnRAR.exe` if it is not exposed through your system `PATH`.

### **2. USB drivers**

The `pyusb` library typically requires compatible **libusb** or **WinUSB** drivers in order to communicate with the Nintendo Switch.

This is commonly configured using **Zadig**.

### **3. Kernel driver detachment**

The `dev.detach_kernel_driver()` call used in the transport layer is generally **Linux-specific**.

On Windows, this may raise an exception and should be bypassed or conditionally handled.

## Linux USB Permissions (udev)

If PyQuark cannot open the Nintendo Switch USB device without `sudo`, add a udev rule.

Create `/etc/udev/rules.d/99-nintendo-switch.rules` with:

```bash
SUBSYSTEM=="usb", ATTR{idVendor}=="057e", ATTR{idProduct}=="3000", MODE="0666", GROUP="plugdev"
```

Then reload udev rules and reconnect the console.

## Tested Environment

This project was developed and tested in the following environment:

* **Operating System:** Pop!_OS 22.04
* **Python:** 3.10+
* **Goldleaf:** 1.20
* **DBI:** 864-ru

Additional testing may be needed to validate behavior across different operating systems, USB driver setups, and client versions.

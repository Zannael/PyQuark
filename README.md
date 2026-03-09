# PyQuark

**PyQuark** is a remote file server for the Nintendo Switch, designed to communicate with the **Goldleaf** homebrew application using the **Quark USB protocol**.

Unlike standard servers, PyQuark features a **Virtual File System (VFS)** that allows you to install games directly from compressed archives and cartridge dumps (XCI). This completely bypasses the need to manually extract or convert files on your computer before installation.

---

## Key Features

### **XCI-to-NSP Injection**

Virtually transforms `.xci` files into installable `.nsp` files on the fly. The system builds a fake **PFS0 header** in memory and maps the **NCA** data by reading directly from the physical offsets within the original XCI file.

This allows Goldleaf to install the game **without ever rewriting the file to disk**.

### **Native RAR Support**

Treats `.rar` archives — including multi-volume formats such as `.part1.rar` — as if they were standard, navigable folders.

From the console's perspective, these archives appear as normal directories that can be browsed directly.

### **Hybrid Streaming and Staging**

PyQuark uses two different strategies depending on file type and access needs:

#### **Streaming**

Lightweight files are read and extracted in chunks, then sent directly over the USB bus.

#### **Staging**

Large files and XCI files — which require random access — are extracted in the background to a temporary cache.

This includes:

* dynamic wait times for read operations
* smart cache management
* automatic cleanup of multi-gigabyte temporary files when the session closes

### **Stateful Session Management**

Keeps track of active paths and the virtualization engine's cache, ensuring stability even when the console requests highly fragmented data reads.

---

## Technical Architecture

The project is structured into distinct, modular layers to handle everything from hardware communication to virtual file manipulation.

### **Transport** (`transport.py`)

Handles low-level USB communication with the Nintendo Switch via **PyUSB**.

### **Protocol** (`protocol.py`)

Acts as the core dispatcher that intercepts, interprets, and responds to **Quark / Goldleaf** commands.

### **VFS** (`core.py`)

The file system abstraction layer. It manages file extension masking and decides how paths are presented to the console.

### **Virtualizer** (`xci_virtualizer.py`)

Responsible for:

* **HFS0 parsing**
* locating secure partition offsets
* generating virtual **NSP headers** directly in RAM

### **Storage** (`rar_stream.py`)

Manages:

* background threading for asynchronous archive extraction
* temporary cache lifecycle

---

## Requirements

To run **PyQuark**, make sure you have the following:

* **Python 3.10** or newer
* The Python libraries listed in the project requirements:

  * `pyusb`
  * `rarfile`
  * `multivolumefile`
* The **unrar** system utility installed and accessible from your operating system's `PATH`
* A **Nintendo Switch** with **Goldleaf** installed
* A **good USB-C cable**

> **Important:** `unrar` is strictly required for the background staging engine to function correctly.

---

## Installation

Clone the official repository and install the required dependencies:

```bash
git clone https://github.com/Zannael/PyQuark.git
cd PyQuark
pip install -r requirements.txt
```

---

## Usage
Run Goldleaf on your Nintendo Switch and connect it via USB to your computer.
To start the server, configure the folder you want to share inside `main.py`, then run it from your IDE or with ```python main.py``` in your terminal.

---

## Compatibility and Technical Notes

This project was developed and tested in the following environment:

* **Operating System:** Pop!_OS 22.04 (Linux-based)
* **Python:** 3.10+
* **Goldleaf:** 1.20

I will run other tests to maximize system compatibility.

### **Note for Windows Users**

Running the project natively on Windows may present some issues and could require minor manual tweaks.

#### **1. System calls to `unrar`**

The `subprocess` module looks for the `unrar` command.

On Windows, you may need to specify the absolute path to `UnRAR.exe` if it is not available in your system `PATH`.

#### **2. USB drivers**

The `pyusb` library on Windows generally requires compatible **libusb** or **WinUSB** drivers in order to communicate with the Nintendo Switch.

This is often configured using tools such as **Zadig**.

#### **3. Kernel drivers**

The `dev.detach_kernel_driver()` method used in the transport layer is **Linux-specific**.

On Windows, this line may throw an error and should be bypassed or conditionally handled.

import os
import sys
from src.transport import connect_switch
from src.protocol import listen_for_commands


def main():
    # Definiamo la cartella da inviare (percorso assoluto per evitare problemi)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_folder = os.path.join(current_dir, "test_folder")

    import shutil

    if shutil.which("unrar") is None:
        print("⚠️ 'unrar' non trovato nel PATH. La modalità staging RAR non sarà disponibile.")

    print("🚀 Avvio PyQuark MITM...")
    try:
        dev, ep_out, ep_in = connect_switch()
        print("✅ Switch connessa correttamente!")

        # Avviamo il loop passando la cartella di test
        listen_for_commands(dev, ep_out, ep_in, test_folder)

    except ConnectionError as e:
        print(f"❌ {e}")
    except Exception as e:
        print(f"⚠️ Errore imprevisto: {e}")
    finally:
        print("🛑 Chiusura.")


if __name__ == "__main__":
    main()
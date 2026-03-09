import os
import sys
from src.transport import connect_switch
from src.protocol import listen_for_commands


def main():
    # The folder sent to the NS
    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_folder = os.path.join(current_dir, "test_folder")

    import shutil

    if shutil.which("unrar") is None:
        print("'unrar' not found in PATH. RAR staging mode won't be available.")

    print("🚀 Running PyQuark MITM...")
    try:
        dev, ep_out, ep_in = connect_switch()
        print("✅ Switch connected!")

        listen_for_commands(dev, ep_out, ep_in, test_folder)

    except ConnectionError as e:
        print(f"❌ {e}")
    except Exception as e:
        print(f"⚠️ Unhandled error: {e}")
    finally:
        print("🛑 Exit.")


if __name__ == "__main__":
    main()
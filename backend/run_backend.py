"""Entry point for the frozen (PyInstaller) backend shipped inside the installer."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from jarvis.server import main
    main()

"""
Nine Acres Bot - Entry Point
============================
Usage: python main.py
"""
from config import DEFAULT_ADB, HAS_CV2, HAS_PIL
from gui import GUI


def main():
    print(f"ADB: {DEFAULT_ADB}")
    if not HAS_CV2:
        print("WARN: pip install opencv-python")
    if not HAS_PIL:
        print("WARN: pip install pillow")
    GUI().mainloop()


if __name__ == "__main__":
    main()

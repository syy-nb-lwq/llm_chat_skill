"""主入口"""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        # Web 模式
        import subprocess
        subprocess.run(["streamlit", "run", "ui/app.py"])
    else:
        # CLI 模式
        from ui.cli import main
        main()

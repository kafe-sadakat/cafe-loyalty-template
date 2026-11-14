"""
Tailwind CSS'i derler (CDN yerine kendi sunucumuzdan sunulur): app/static/css/app.css

Gereksinim: Node.js. İlk çalıştırmada tools/ altına tailwindcss@3.4.17 kurulur.
Kullanım:  python tools/build_css.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
OUT = TOOLS.parent / "app" / "static" / "css" / "app.css"
EXTENSIONS = TOOLS / "panel_extensions.css"


def main() -> int:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        print("Node.js/npm bulunamadı.")
        return 1
    if not (TOOLS / "node_modules" / "tailwindcss").exists():
        subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--prefix", str(TOOLS),
             "tailwindcss@3.4.17", "@tailwindcss/forms@0.5.7", "@tailwindcss/container-queries@0.1.1"],
            check=True,
        )
    cli = TOOLS / "node_modules" / "tailwindcss" / "lib" / "cli.js"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    subprocess.run(
        [node, str(cli), "-c", str(TOOLS / "tailwind.config.js"), "-i", str(TOOLS / "tailwind.input.css"),
         "-o", str(OUT), "--minify"],
        check=True, cwd=str(TOOLS),
    )
    # Stitch tasarım katmanı (uzantılar) Tailwind çıktısının üzerine eklenir.
    if EXTENSIONS.exists():
        with open(OUT, "a", encoding="utf-8") as f:
            f.write("\n" + EXTENSIONS.read_text(encoding="utf-8"))
    print(f"Üretildi: {OUT} ({OUT.stat().st_size} bayt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

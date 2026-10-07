"""Valida que el VPS tenga todo lo necesario para el pipeline NinjaTrader + Python.
Correr directamente en el VPS Windows: python validate_environment.py
"""
import importlib.metadata as md
import os
import platform
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

REQUIRED_PIP_PACKAGES = ["anthropic", "pandas", "numpy", "python-dotenv", "pywinauto"]


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"error: {e}"
    print(f"[{'OK' if ok else 'FALTA'}] {name}: {detail}")
    return ok


def check_python_version():
    v = sys.version_info
    return v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"


def check_pip_package(pkg_name):
    try:
        return True, f"version {md.version(pkg_name)}"
    except md.PackageNotFoundError:
        return False, "no instalado (pip install " + pkg_name + ")"


def check_git():
    path = shutil.which("git")
    if not path:
        return False, "no encontrado en PATH"
    out = subprocess.run(["git", "--version"], capture_output=True, text=True)
    return True, out.stdout.strip()


def check_dotnet_framework_48():
    key_path = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            release, _ = winreg.QueryValueEx(key, "Release")
            version, _ = winreg.QueryValueEx(key, "Version")
    except FileNotFoundError:
        return False, "clave de registro de .NET Framework no encontrada"
    ok = release >= 528040  # 528040 = primer release que corresponde a .NET Framework 4.8
    return ok, f"version {version} (release {release})"


def check_visual_studio():
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.exists():
        out = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
            capture_output=True, text=True,
        )
        path = out.stdout.strip()
        if path:
            return True, path
        return False, "vswhere no encontro ninguna instalacion"
    default = Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\devenv.exe")
    if default.exists():
        return True, str(default)
    return False, "no se encontro Visual Studio (ni vswhere ni ruta default)"


def check_vscode():
    path = shutil.which("code")
    return (True, path) if path else (False, "no encontrado en PATH")


def check_ninjatrader():
    candidates = [
        Path(r"C:\Program Files\NinjaTrader 8\bin\NinjaTrader.exe"),
        Path(r"C:\Program Files (x86)\NinjaTrader 8\bin\NinjaTrader.exe"),
    ]
    for c in candidates:
        if c.exists():
            return True, str(c)
    return False, "no se encontro NinjaTrader.exe en las rutas tipicas"


def check_ninjatrader_assemblies():
    bases = [
        Path(r"C:\Program Files\NinjaTrader 8\bin"),
        Path(r"C:\Program Files (x86)\NinjaTrader 8\bin"),
    ]
    required = ["NinjaTrader.Core.dll", "NinjaTrader.Gui.dll"]
    for base in bases:
        if base.exists():
            missing = [f for f in required if not (base / f).exists()]
            if not missing:
                return True, f"encontrados en {base}"
            return False, f"faltan en {base}: {missing}"
    return False, "no se encontro la carpeta bin de NinjaTrader"


def check_strategies_folder():
    folder = Path.home() / "Documents" / "NinjaTrader 8" / "bin" / "Custom" / "Strategies"
    return folder.exists(), str(folder)


def check_env_file():
    env_path = Path(".env")
    if not env_path.exists():
        return False, ".env no encontrado en el directorio actual"
    content = env_path.read_text(encoding="utf-8", errors="ignore")
    has_key = "ANTHROPIC_API_KEY=" in content and len(
        content.split("ANTHROPIC_API_KEY=")[-1].split()[0].strip()
    ) > 5
    return has_key, "presente" if has_key else "ausente o vacia"


def check_cli_installed(command):
    path = shutil.which(command)
    if not path:
        return False, "no encontrado en PATH"
    out = subprocess.run([path, "--version"], capture_output=True, text=True)
    version = (out.stdout or out.stderr).strip()
    return True, version if version else path


def check_env_var_set(var_name):
    value = os.environ.get(var_name)
    if not value:
        return False, "no seteada como variable de entorno"
    return True, f"seteada ({len(value)} caracteres)"


def check_interactive_desktop():
    from pywinauto import Desktop
    windows = Desktop(backend="uia").windows()
    return True, f"{len(windows)} ventanas detectadas (sesion interactiva activa)"


def main():
    if platform.system() != "Windows":
        print("Este validador esta pensado para correr en el VPS Windows.")
        sys.exit(1)

    print("=== Validacion de entorno: NinjaTrader + Python Trading Platform ===\n")

    results = [
        check("Python >= 3.11", check_python_version),
        *[check(f"pip package: {p}", lambda p=p: check_pip_package(p)) for p in REQUIRED_PIP_PACKAGES],
        check("Git", check_git),
        check(".NET Framework 4.8+", check_dotnet_framework_48),
        check("Visual Studio", check_visual_studio),
        check("VS Code", check_vscode),
        check("NinjaTrader 8 instalado", check_ninjatrader),
        check("Ensamblados de NinjaTrader", check_ninjatrader_assemblies),
        check("Carpeta de Strategies", check_strategies_folder),
        check("Archivo .env con ANTHROPIC_API_KEY", check_env_file),
        check("Claude CLI instalado", lambda: check_cli_installed("claude")),
        check("ANTHROPIC_API_KEY (variable de entorno)", lambda: check_env_var_set("ANTHROPIC_API_KEY")),
        check("Gemini CLI instalado", lambda: check_cli_installed("gemini")),
        check("GEMINI_API_KEY (variable de entorno)", lambda: check_env_var_set("GEMINI_API_KEY")),
        check("Sesion de escritorio interactiva (pywinauto)", check_interactive_desktop),
    ]

    print(f"\n{sum(results)}/{len(results)} checks OK")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

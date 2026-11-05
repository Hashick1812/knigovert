# Сборка Windows-приложения: pyinstaller knigovert.spec --noconfirm
# Результат: dist/Knigovert/Knigovert.exe. Папку можно копировать целиком, интернет не нужен.

from PyInstaller.utils.hooks import collect_data_files

# Зависимости PDF-библиотеки для распознавания макета. Приложению они не нужны, а весят сотни мегабайт.
UNUSED = [
    "onnxruntime", "numpy", "networkx", "pymupdf_layout", "pymupdf4llm", "pymupdf.layout", "yaml", "tabulate",
    "psutil", "sympy", "scipy", "pandas", "matplotlib", "tkinter", "PIL.ImageTk", "pytest", "IPython",
]

analysis = Analysis(
    ["packaging/launcher.py"],
    pathex=["src"],
    datas=[
        ("src/knigovert/web", "knigovert/web"),
        ("src/knigovert/assets/fonts", "knigovert/assets/fonts"),
        *collect_data_files("pyphen"),
        *collect_data_files("docx"),
    ],
    excludes=UNUSED,
)
# Заголовочные файлы для разработки и отключённые выше части PDF-библиотеки в программе не используются.
analysis.datas = [item for item in analysis.datas if "mupdf-devel" not in item[0].replace("\\", "/")]

archive = PYZ(analysis.pure)

program = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Knigovert",
    icon="src/knigovert/assets/icon.ico",
    console=False,
    upx=False,
)

COLLECT(program, analysis.binaries, analysis.datas, name="Knigovert", upx=False)

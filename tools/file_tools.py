from pathlib import Path


def create_file(path: str, content: str) -> str:
    """Create a text file and any missing parent folders. Returns the file path."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return str(file_path)


def create_directory(path: str) -> str:
    """Create a directory and any missing parent folders. Returns the directory path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def read_file(path: str) -> str:
    """Read a text file and return its contents."""
    return Path(path).read_text(encoding="utf-8")


def update_file(path: str, content: str) -> str:
    """Replace the full contents of an existing text file. Returns the file path."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(path)
    file_path.write_text(content, encoding="utf-8")
    return str(file_path)


def append_file(path: str, content: str) -> str:
    """Add text to the end of an existing file. Returns the file path."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(path)
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return str(file_path)


def replace_in_file(path: str, old: str, new: str) -> str:
    """Replace every occurrence of old text with new text. Returns the file path."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(path)
    text = file_path.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"Text not found: {old}")
    file_path.write_text(text.replace(old, new), encoding="utf-8")
    return str(file_path)

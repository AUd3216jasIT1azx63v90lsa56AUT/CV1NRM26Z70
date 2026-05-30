from pathlib import Path
import shutil


def clone_validated_folder() -> None:
    source = Path("validated")
    destination = Path("clone_validated")

    if not source.exists() or not source.is_dir():
        raise FileNotFoundError("Source folder 'validated' was not found.")

    if destination.exists():
        shutil.rmtree(destination)

    shutil.copytree(source, destination)
    print("Copied 'validated' to 'clone_validated'.")


if __name__ == "__main__":
    clone_validated_folder()

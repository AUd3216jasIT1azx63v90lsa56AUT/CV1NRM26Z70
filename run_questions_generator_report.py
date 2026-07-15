from pathlib import Path
import sys

from questions_generator import GetQuestions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import re


def get_scope_questions_pending():
    """Return pending queue files and their entries, preserving file ownership."""
    scope_questions_pending_dir = os.environ.get("SCOPE_QUESTIONS_PENDING_DIR", "scope_questions_pending")
    pending = []

    # Ensure directory exists
    if not os.path.exists(scope_questions_pending_dir):
        print(f"Directory {scope_questions_pending_dir} does not exist")
        return pending

    # Get all JSON files in the directory
    json_files = list(Path(scope_questions_pending_dir).glob("*.json"))

    if not json_files:
        print(f"No JSON files found in {scope_questions_pending_dir}")
        return pending

    # Process each JSON file
    for json_file in json_files:
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

                entries = data if isinstance(data, list) else [data]
                entries = [item for item in entries if isinstance(item, dict) and item.get('url')]
                if not entries:
                    raise ValueError("queue file has no URL entries")
                pending.append((json_file, entries))

        except json.JSONDecodeError as e:
            print(f"Error parsing {json_file}: {e}")
        except Exception as e:
            print(f"Error processing {json_file}: {e}")

    return pending


def main():
    pending_files = get_scope_questions_pending()
    if not pending_files:
        raise RuntimeError("No pending reports to generate")

    total_urls = sum(len(entries) for _, entries in pending_files)
    print(f"Found {total_urls} URLs across {len(pending_files)} recoverable queue files")
    successful_files = 0
    saved_questions = 0
    failures = []
    report = GetQuestions(teardown=True)

    try:
        for file_index, (pending_file, entries) in enumerate(pending_files, 1):
            extracted = []
            saved_paths = []
            saved_count_for_file = 0
            try:
                for entry_index, entry in enumerate(entries, 1):
                    url = entry['url']
                    prompt = entry.get('question', '')
                    match = re.search(r"File Name:\s*([^ ]+)", prompt)
                    expected_file = match.group(1) if match else None
                    print(
                        f"[{file_index}/{len(pending_files)} file, "
                        f"{entry_index}/{len(entries)} URL] Validating: {url}"
                    )
                    extracted.append(report.get_questions(url, expected_file=expected_file))

                for questions in extracted:
                    saved_paths.extend(report.save_questions(questions))
                    saved_questions += len(questions)
                    saved_count_for_file += len(questions)

                source_file = Path(os.environ.get("SCOPE_QUESTIONS_DIR", "scope_questions")) / pending_file.name
                if not source_file.exists():
                    raise FileNotFoundError(f"tracked queue source disappeared: {source_file}")
                source_file.unlink()
                pending_file.unlink(missing_ok=True)
                successful_files += 1
                print(f"Completed and consumed {source_file}")
            except Exception as exc:
                for saved_path in saved_paths:
                    Path(saved_path).unlink(missing_ok=True)
                saved_questions -= saved_count_for_file
                failures.append(f"{pending_file.name}: {exc}")
                print(f"Preserved {pending_file.name} for retry: {exc}")
    finally:
        if report.teardown:
            report.driver.quit()

    print(
        f"\n=== Saved {saved_questions} validated questions from "
        f"{successful_files}/{len(pending_files)} queue files ==="
    )
    if failures:
        print("Deferred queue files:")
        for failure in failures:
            print(f"- {failure}")

    if saved_questions == 0:
        raise RuntimeError("Stage 3 produced zero validated questions; all source inputs were preserved")



if __name__ == '__main__':
    main()

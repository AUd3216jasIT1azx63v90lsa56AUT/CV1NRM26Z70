import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
from questions_generator import GenerateQuestions


def get_pending_scope_file(question_pending_dir="scope_pending"):
    """Get the first pending question file path if it exists."""
    try:
        pending_files = sorted(Path(question_pending_dir).glob('*.json'))
        return pending_files[0] if pending_files else None
    except Exception as e:
        print(f"Error finding pending question file: {e}")
        return None


scope_pending_dir = "scope_pending"
scope_dir = "scope"
pending_file = get_pending_scope_file()

if not pending_file:
    print("No pending question files found.")
    sys.exit(0)


def main():
    try:
        # Load questions once
        with open(pending_file, 'r', encoding='utf-8') as f:
            questions = json.load(f)

        if not isinstance(questions, list):
            raise ValueError(f"Expected a list of questions in {pending_file}, got {type(questions)}")

        total = len(questions)
        print(f"Found {total} questions in {pending_file}")

        output_file = Path("scope_questions") / pending_file.name
        completed = set()
        if output_file.exists():
            existing = json.loads(output_file.read_text(encoding="utf-8"))
            completed = {
                item.get("question")
                for item in existing
                if isinstance(item, dict) and item.get("response_text")
            }

        # Process questions
        for i, question in enumerate(questions, 1):
            if question in completed:
                print(f"[{i}/{total}] Reusing validated stored response")
                continue
            print(f"[{i}/{total}] Processing: {question[:50]}...")
            bot = GenerateQuestions(teardown=True)
            try:
                bot.ask_question(question)
            finally:
                bot.driver.quit()

            if i >= 25:  # Process maximum 25 questions
                print("Reached the limit of 25 questions")
                break

        # If we get here, processing was successful
        print(f"Successfully processed {i} questions")
        # Delete the processed file
        pending_file.unlink()
        print(f"Deleted processed file: {pending_file}")
        source_file = Path(scope_dir) / pending_file.name
        source_file.unlink()
        print(f"Deleted completed source file: {source_file}")

    except Exception as e:
        print(f"Error during processing: {e}")
        print(f"Preserved tracked source and any completed stored responses for retry")
        raise


if __name__ == '__main__':
    main()

import json
import ast
import os
import random
import shutil
import time
import uuid
from pathlib import Path

from decouple import config
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from questions import BASE_URL, question_generator
import pyperclip
import re
from typing import List


def parse_question_content(content: str) -> List[str]:
    """Parse a DeepWiki answer into the durable question-list contract."""
    text = content.strip()
    candidates = [text]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:python)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    )

    # A rendered page can contain the one-item example from the prompt and the
    # final answer. Extract each balanced list separately and prefer the last.
    for source in list(candidates):
        for assignment in re.finditer(r"questions\s*=\s*\[", source):
            start = source.find("[", assignment.start())
            depth = 0
            quote = None
            escaped = False
            for index in range(start, len(source)):
                char = source[index]
                if quote:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = None
                    continue
                if char in ("'", '"'):
                    quote = char
                elif char == "[":
                    depth += 1
                elif char == "]":
                    depth -= 1
                    if depth == 0:
                        candidates.append(source[start:index + 1])
                        break

    for candidate in reversed(candidates):
        try:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                return [item.strip() for item in parsed]
        except (SyntaxError, ValueError):
            pass

    pattern = r'["\'](\[File:.*?Proof idea:.*?)["\'](?=\s*,?\s*(?:\]|\n))'
    return [q.strip() for q in re.findall(pattern, text, flags=re.DOTALL)]


def validate_question_content(questions, expected_file=None):
    if not 35 <= len(questions) <= 60:
        raise ValueError(f"expected 35-60 questions, extracted {len(questions)}")

    required_markers = ("[File:", "[Function:", "Proof idea:")
    malformed = [q for q in questions if not all(marker in q for marker in required_markers)]
    if malformed:
        raise ValueError(f"{len(malformed)} questions are missing required audit fields")

    if expected_file:
        wrong_file = [q for q in questions if f"[File: {expected_file}]" not in q]
        if wrong_file:
            raise ValueError(f"{len(wrong_file)} questions target a file other than {expected_file}")


def wait_for_valid_response(driver, expected_file=None):
    """Wait in the creating browser session until the Deep Research answer is complete."""
    timeout = int(os.environ.get("DEEPWIKI_RESPONSE_TIMEOUT_SECONDS", "1200"))
    poll = int(os.environ.get("DEEPWIKI_RESPONSE_POLL_SECONDS", "15"))
    deadline = time.time() + timeout
    last_error = "response was not rendered"
    next_status = time.time() + 60

    while time.time() < deadline:
        candidates = [element.text for element in driver.find_elements(By.CSS_SELECTOR, "pre, code")]
        candidates.append(driver.find_element(By.TAG_NAME, "body").text)
        for candidate in reversed(candidates):
            try:
                questions = parse_question_content(candidate)
                validate_question_content(questions, expected_file)
                return questions
            except ValueError as exc:
                last_error = str(exc)
        if time.time() >= next_status:
            body_length = len(candidates[-1]) if candidates else 0
            print(f"Waiting for validated DeepWiki response: {last_error}; body_length={body_length}")
            next_status = time.time() + 60
        time.sleep(poll)

    raise TimeoutError(f"DeepWiki response did not complete within {timeout}s: {last_error}")


def submit_deep_research(driver, question_gotten):
    """Submit with React's native textarea setter and return the created session URL."""
    wait = WebDriverWait(driver, 120)
    driver.get(BASE_URL)
    form = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form")))
    textarea = form.find_element(By.CSS_SELECTOR, 'textarea[data-deepwiki-input="question"], textarea')

    mode = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[.//span[normalize-space(text())="Fast"] or normalize-space(.)="Fast"]')))
    mode.click()
    deep = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//div[@role='menuitem' and .//span[normalize-space(text())='Deep Research']]")
    ))
    deep.click()

    formatted_question = question_generator(question_gotten)
    driver.execute_script(
        "const setter=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
        "setter.call(arguments[0], arguments[1]);"
        "arguments[0].dispatchEvent(new InputEvent('input',"
        "{bubbles:true,inputType:'insertText',data:arguments[1]}));",
        textarea,
        formatted_question,
    )
    if textarea.get_attribute("value") != formatted_question:
        raise RuntimeError("DeepWiki textarea did not retain the complete prompt")

    submit_buttons = form.find_elements(By.CSS_SELECTOR, "button")
    if not submit_buttons:
        raise RuntimeError("DeepWiki submit button was not found")
    submit = submit_buttons[-1]
    wait.until(lambda _driver: submit.is_enabled())
    submit.click()
    wait.until(lambda active_driver: active_driver.current_url != BASE_URL)
    return driver.current_url


class GenerateQuestions:
    def __init__(self, teardown=False):

        s = Service(ChromeDriverManager().install())
        self.options = webdriver.ChromeOptions()
        self.options.page_load_strategy = "eager"

        # --- Add these two lines here ---
        self.options.add_argument("--headless")
        self.options.add_argument("--window-size=1920,1080")
        # ---------------------------------

        # removed headless so the browser window is visible
        # ensure window is visible and starts maximized
        self.options.add_argument('--start-maximized')
        self.teardown = teardown
        # keep chrome open after chromedriver exits
        self.options.add_experimental_option("detach", True)
        self.options.add_experimental_option(
            "excludeSwitches",
            ['enable-logging'])
        self.driver = webdriver.Chrome(
            options=self.options,
            service=s)
        self.driver.set_page_load_timeout(60)
        self.driver.implicitly_wait(50)
        self.collections_url = []
        super(GenerateQuestions, self).__init__()

    def __enter__(self):
        self.driver.get(BASE_URL)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.teardown:
            self.driver.quit()

    def toggle_deep_research(self):
        wait = WebDriverWait(self.driver, 20)

        xpath = '//button[.//span[normalize-space(text())="Fast"]]'
        btn = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
        btn.click()

        xpath_primary = "//div[@role='menuitem' and .//span[normalize-space(text())='Deep Research']]"
        menu_item = wait.until(EC.element_to_be_clickable((By.XPATH, xpath_primary)))
        menu_item.click()

    def ask_question(self, question_gotten):
        last_error = None
        attempts = int(os.environ.get("DEEPWIKI_GENERATION_ATTEMPTS", "1"))
        for attempt in range(1, attempts + 1):
            try:
                current_url = submit_deep_research(self.driver, question_gotten)
                expected_match = re.search(r"File Name:\s*([^ ]+)", question_gotten)
                expected_file = expected_match.group(1) if expected_match else None
                questions = wait_for_valid_response(self.driver, expected_file)
                response_text = json.dumps(questions, ensure_ascii=False)
                self.save_to_questions(question_gotten, current_url, response_text)
                return questions
            except Exception as exc:
                last_error = exc
                print(f"DeepWiki generation failed (attempt {attempt}/{attempts}): {exc}")
                if attempt < attempts:
                    time.sleep(10)

        raise RuntimeError(f"DeepWiki did not produce a validated response: {last_error}")

    def save_to_questions(self, question_gotten, url, response_text):
        """Save question and URL to questions.json"""
        collections_file = config("SCOPE_QUESTIONS_PATH")

        # Load existing data or start fresh
        try:
            if os.path.exists(collections_file):
                with open(collections_file, "r") as f:
                    content = f.read().strip()
                    data = json.loads(content) if content else []
            else:
                data = []
        except json.JSONDecodeError:
            print("Invalid questions.json, creating new file")
            data = []

        # Add new entry
        entry = {
            "question": question_gotten,
            "url": url,
            "response_text": response_text,
            "questions_generated": False
        }
        for index, existing in enumerate(data):
            if isinstance(existing, dict) and existing.get("question") == question_gotten:
                data[index] = entry
                break
        else:
            data.append(entry)

        # Save with proper formatting
        try:
            with open(collections_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving to collections: {e}")


class GetQuestions:
    def __init__(self, teardown=False):

        s = Service(ChromeDriverManager().install())
        self.options = webdriver.ChromeOptions()
        self.options.page_load_strategy = "eager"

        # --- Add these two lines here ---
        # self.options.add_argument("--headless")
        # self.options.add_argument("--window-size=1920,1080")
        # ---------------------------------

        # removed headless so the browser window is visible
        # ensure window is visible and starts maximized
        self.options.add_argument('--start-maximized')
        self.teardown = teardown
        # keep chrome open after chromedriver exits
        self.options.add_experimental_option("detach", True)
        self.options.add_experimental_option(
            "excludeSwitches",
            ['enable-logging'])
        self.driver = webdriver.Chrome(
            options=self.options,
            service=s)
        self.driver.set_page_load_timeout(60)
        self.driver.implicitly_wait(50)
        self.collections_url = []
        super(GetQuestions, self).__init__()

    def get_questions(self, url, expected_file=None, response_text=None, question_prompt=None):
        """Return a completed, validated DeepWiki response without consuming its queue item."""
        if response_text:
            questions = self.get_question_content(response_text)
            self.validate_questions(questions, expected_file)
            return questions

        attempts = int(os.environ.get("REPORT_READY_ATTEMPTS", "3"))
        retry_delay = int(os.environ.get("REPORT_READY_RETRY_SECONDS", "20"))
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                self.driver.get(url)
                wait = WebDriverWait(self.driver, 60)
                body = wait.until(EC.presence_of_element_located((By.TAG_NAME, "body"))).text
                if "404" in body and "page could not be found" in body.lower():
                    raise FileNotFoundError("stored DeepWiki URL is session-local and now returns 404")
                copy_button_selector = (By.CSS_SELECTOR, '[aria-label="Copy"]')
                all_copy_buttons = wait.until(
                    EC.presence_of_all_elements_located(copy_button_selector)
                )
                last_copy_button = all_copy_buttons[-1]
                wait.until(EC.element_to_be_clickable(last_copy_button)).click()

                xpath = "//div[@role='menuitem' and normalize-space(text())='Copy response']"
                el = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                el.click()

                all_questions = self.get_question_content(pyperclip.paste())
                self.validate_questions(all_questions, expected_file)
                return all_questions
            except Exception as exc:
                last_error = exc
                print(f"Response not ready or invalid (attempt {attempt}/{attempts}): {exc}")
                if isinstance(exc, FileNotFoundError):
                    break
                if attempt < attempts:
                    time.sleep(retry_delay)

        if question_prompt:
            print(f"Stored response is unavailable; regenerating from recovered prompt: {last_error}")
            submit_deep_research(self.driver, question_prompt)
            return wait_for_valid_response(self.driver, expected_file)

        raise RuntimeError(f"DeepWiki response did not become usable for {url}: {last_error}")

    def save_questions(self, questions):
        question_directory = os.environ.get('QUESTION_DIR', 'question')
        os.makedirs(question_directory, exist_ok=True)
        chunk_size = 25
        saved_paths = []

        try:
            for i in range(0, len(questions), chunk_size):
                chunk = questions[i:i + chunk_size]
                filename = f"{str(uuid.uuid4())}.json".replace("-", "")
                filepath = os.path.join(question_directory, filename)
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(chunk, f, indent=2, ensure_ascii=False)
                saved_paths.append(filepath)
                print(f"Saved {len(chunk)} questions to {filepath}")
        except Exception:
            for saved_path in saved_paths:
                Path(saved_path).unlink(missing_ok=True)
            raise

        return saved_paths

    def get_question_content(self, clip_board_content: str) -> List[str]:
        return parse_question_content(clip_board_content)

    @staticmethod
    def validate_questions(questions, expected_file=None):
        validate_question_content(questions, expected_file)


def generate_file_path_for_scope():
    # Get the directory from environment variable, or use 'questions' as default
    scope_questions_directory = os.environ.get('SCOPE_QUESTIONS_DIR', 'scope_questions')
    scope_directory = os.environ.get("QUESTION_DIR", 'scope')
    scope_pending_directory = os.environ.get("SCOPE_PENDING_DIR", 'scope_pending')

    # Create the directories if they don't exist
    os.makedirs(scope_questions_directory, exist_ok=True)
    os.makedirs(scope_directory, exist_ok=True)
    os.makedirs(scope_pending_directory, exist_ok=True)

    scope_files = sorted(Path(scope_directory).glob('*.json'))

    if not scope_files:
        raise FileNotFoundError(f"No scope files found in {scope_directory}")

    # Get the first file
    source_file = random.choice(scope_files)
    file_name = source_file.name

    # Define destination path in pending directory
    destination_file = Path(scope_pending_directory) / file_name
    questions_file = f"{source_file.stem}.json"  # Keep the same filename but ensure .json extension

    try:
        # Keep the tracked source until all of its responses are validated and committed.
        shutil.copy2(source_file, destination_file)
        print(f"Staged {file_name} in {scope_pending_directory}")
    except Exception as e:
        raise IOError(f"Failed to stage {file_name} in {scope_pending_directory}: {e}")

    # Generate file path
    file_path = os.path.join(scope_questions_directory, questions_file)

    # Create or update .env file with the file path
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    with open(env_path, 'w') as f:
        f.write(f"SCOPE_QUESTIONS_PATH={file_path}\n")

    os.environ['SCOPE_QUESTIONS_PATH'] = file_path
    print(os.environ.get('SCOPE_QUESTIONS_PATH'))

    return str(questions_file)


def generate_file_path_get_questions():
    question_directory = os.environ.get("QUESTIONS_DIR", "questions")
    scope_questions_directory = os.environ.get('SCOPE_QUESTIONS_DIR', 'scope_questions')
    scope_questions_pending_directory = os.environ.get("SCOPE_QUESTIONS_PENDING_DIR", 'scope_questions_pending')

    # Create the directories if they don't exist
    os.makedirs(question_directory, exist_ok=True)
    os.makedirs(scope_questions_directory, exist_ok=True)
    os.makedirs(scope_questions_pending_directory, exist_ok=True)

    # Get all JSON files in the questions directory
    questions_files = sorted(Path(scope_questions_directory).glob('*.json'))

    if not questions_files:
        raise FileNotFoundError("No questions files found")

    moved_files = []
    counter = 0

    # Stored-response queues are cheap to consume. Legacy URL-only queues need
    # live regeneration, so stage just one to keep each repair run bounded and
    # make partial response persistence useful.
    has_legacy_file = False
    for file_path in questions_files:
        try:
            try:
                queue_data = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Skipping unreadable queue file {file_path}: {exc}")
                continue
            entries = queue_data if isinstance(queue_data, list) else [queue_data]
            is_legacy = any(not isinstance(entry, dict) or not entry.get("response_text") for entry in entries)
            if counter >= 20 or (is_legacy and has_legacy_file) or (is_legacy and counter > 0):
                break

            # Create destination path
            dest_path = os.path.join(scope_questions_pending_directory, file_path.name)

            # Skip if file with same name already exists in destination
            if os.path.exists(dest_path):
                # Append a timestamp to make filename unique
                base_name = file_path.stem
                extension = file_path.suffix
                timestamp = int(time.time())
                dest_path = os.path.join(scope_questions_pending_directory, f"{base_name}_{timestamp}{extension}")

            shutil.copy2(str(file_path), dest_path)
            moved_files.append(dest_path)
            counter += 1
            has_legacy_file = has_legacy_file or is_legacy
            print(f"Staged {file_path} at {dest_path}")

            if is_legacy:
                break

        except Exception as e:
            print(f"Error moving {file_path}: {e}")
            continue

    if not moved_files:
        print("No files were moved")
        return None

    print(f"Successfully staged {len(moved_files)} files in {scope_questions_pending_directory}")
    return moved_files

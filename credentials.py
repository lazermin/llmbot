import os
from pathlib import Path

KEY_NAME = "llm-api-key"
REQUIRE_KEY_ENV = "LLMBOT_REQUIRE_LLM_CREDENTIAL"


def read_llm_credential() -> str | None:
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if directory:
        path = Path(directory) / KEY_NAME
        try:
            key = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            raise ValueError("Не удалось прочитать системный credential llm-api-key.") from None
        else:
            if not key:
                raise ValueError("Системный credential llm-api-key пуст.")
            return key
    if os.getenv(REQUIRE_KEY_ENV) == "1":
        raise ValueError("Systemd не передал credential llm-api-key.")
    return None

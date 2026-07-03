from pathlib import Path


STUDENT_ID = "2022-18758"
PROMPT = f"DB_{STUDENT_ID}> "
EXIT_SIGNAL = "__EXIT__"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = PROJECT_ROOT / "DB"
DB_FILE = DB_DIR / "myDB.mdb"

DATE_LITERAL_TAG = "__DATE_LITERAL__"
NULL_LITERAL_TAG = "__NULL_LITERAL__"

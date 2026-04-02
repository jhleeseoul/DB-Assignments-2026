from pathlib import Path

from lark import Lark, Transformer
from lark.exceptions import LarkError


STUDENT_ID = "2022-18758"
PROMPT = f"DB_{STUDENT_ID}> "
EXIT_SIGNAL = "__EXIT__"


class CommandTransformer(Transformer):
    """Convert a parsed tree into the command label required by the project."""

    def command(self, items):
        return items[0]

    def query(self, items):
        return items[0]

    def exit_query(self, _items):
        return EXIT_SIGNAL

    def create_table_query(self, _items):
        return "CREATE TABLE"

    def drop_table_query(self, _items):
        return "DROP TABLE"

    def explain_query(self, _items):
        return "EXPLAIN"

    def describe_query(self, _items):
        return "DESCRIBE"

    def desc_query(self, _items):
        return "DESC"

    def insert_query(self, _items):
        return "INSERT"

    def delete_query(self, _items):
        return "DELETE"

    def select_query(self, _items):
        return "SELECT"

    def show_tables_query(self, _items):
        return "SHOW TABLES"

    def update_query(self, _items):
        return "UPDATE"

    def rename_table_query(self, _items):
        return "RENAME TABLE"

    def truncate_table_query(self, _items):
        return "TRUNCATE TABLE"


def build_parser() -> Lark:
    grammar_path = Path(__file__).with_name("grammar.lark")
    with grammar_path.open("r", encoding="utf-8") as grammar_file:
        return Lark(grammar_file.read(), start="command", lexer="basic")


def extract_statements(buffer: str):
    """Split complete semicolon-terminated statements while preserving quoted text."""
    statements = []
    start = 0
    in_single = False
    in_double = False
    escaped = False

    for index, char in enumerate(buffer):
        if escaped:
            escaped = False
            continue

        if char == "\\" and (in_single or in_double):
            escaped = True
            continue

        if char == "'" and not in_double:
            in_single = not in_single
            continue

        if char == '"' and not in_single:
            in_double = not in_double
            continue

        if char == ";" and not in_single and not in_double:
            statement = buffer[start : index + 1].strip()
            if statement:
                statements.append(statement)
            start = index + 1

    return statements, buffer[start:]


def parse_statement(parser: Lark, transformer: CommandTransformer, statement: str):
    tree = parser.parse(statement)
    return transformer.transform(tree)


def main():
    parser = build_parser()
    transformer = CommandTransformer()
    buffer = ""

    while True:
        try:
            line = input(PROMPT if not buffer else "")
        except EOFError:
            break

        buffer = f"{buffer}\n{line}" if buffer else line
        statements, buffer = extract_statements(buffer)

        for statement in statements:
            try:
                command = parse_statement(parser, transformer, statement)
            except LarkError:
                print(f"{PROMPT}Syntax error")
                buffer = ""
                break

            if command == EXIT_SIGNAL:
                return

            print(f"{PROMPT}'{command}' requested")


if __name__ == "__main__":
    main()

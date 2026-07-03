from lark.exceptions import LarkError

from .commands import dispatch
from .config import DB_FILE, EXIT_SIGNAL, PROMPT
from .errors import DBError
from .executor import QueryExecutor
from .parser import CommandTransformer, build_parser, parse_statement
from .storage import DBMS


def extract_statements(buffer: str):
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


def main():
    parser = build_parser()
    transformer = CommandTransformer()
    database = DBMS(DB_FILE)
    executor = QueryExecutor(database)

    buffer = ""
    try:
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
                    if not isinstance(command, dict):
                        print(f"{PROMPT}Syntax error")
                        continue
                except (LarkError, ValueError):
                    print(f"{PROMPT}Syntax error")
                    continue

                if command["kind"] == EXIT_SIGNAL:
                    return
                if command["kind"] == "INVALID":
                    print(f"{PROMPT}Syntax error")
                    continue

                try:
                    output = dispatch(database, executor, command)
                    for out_line in output:
                        if out_line:
                            print(f"{PROMPT}{out_line}")
                except DBError as error:
                    print(f"{PROMPT}{error}")
    finally:
        database.close()

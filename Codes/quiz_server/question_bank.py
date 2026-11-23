"""Validate the entire bank before the network starts."""
import json
import re


def invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def check(condition, field):
    if not condition:
        raise ValueError(f"Invalid question bank field: {field}")


def string(obj, key, low, high, visible=True):
    value = obj.get(key)
    check(isinstance(value, str) and low <= len(value) <= high, key)
    if visible and value:
        check(value.isprintable() and bool(value.strip()), key)
    return value


def identifier(obj, key, seen):
    value = string(obj, key, 1, 32)
    check(re.fullmatch(r"[A-Za-z0-9_-]+", value) and value not in seen, key)
    seen.add(value)


def integer(obj, key, low, high):
    value = obj.get(key)
    check(type(value) is int and low <= value <= high, key)


def objects(obj, key, low, high):
    values = obj.get(key)
    check(isinstance(values, list) and low <= len(values) <= high, key)
    check(all(isinstance(item, dict) for item in values), key)
    return values


def load_and_validate_questions(path):
    with open(path, "rb") as source:
        data = source.read(1_000_001)
    check(0 < len(data) <= 1_000_000, "file size")
    bank = json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
    check(isinstance(bank, dict), "root")
    integer(bank, "schema_version", 1, 1)
    quiz_ids, question_ids = set(), set()
    for quiz in objects(bank, "quizzes", 1, 20):
        identifier(quiz, "quiz_id", quiz_ids)
        string(quiz, "title", 1, 100)
        string(quiz, "description", 0, 300)
        for question in objects(quiz, "questions", 5, 50):
            identifier(question, "question_id", question_ids)
            string(question, "text", 1, 500)
            options = question.get("options")
            check(isinstance(options, dict) and set(options) == set("ABCD"), "options")
            for option in options:
                string(options, option, 1, 200, visible=False)
            check(question.get("answer") in ("A", "B", "C", "D"), "answer")
            integer(question, "time_limit", 5, 60)
            integer(question, "points", 1, 1000)
    return bank

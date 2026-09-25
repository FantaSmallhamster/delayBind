"""Answer parsing and run limits shared by the R2 model loop."""


class V52ResourceLimit(RuntimeError):
    pass


class V52ProtocolError(RuntimeError):
    pass


def extract_boxed(text: str) -> str:
    index = text.rfind("\\boxed{")
    if index < 0:
        raise ValueError("ANSWER_BOX_MISSING")
    start, depth = index + 7, 1
    for end in range(start, len(text)):
        if text[end] == "{":
            depth += 1
        elif text[end] == "}":
            depth -= 1
        if depth == 0:
            if text[end + 1:].strip():
                raise ValueError("ANSWER_BOX_NOT_LAST")
            return text[start:end]
    raise ValueError("ANSWER_BOX_UNCLOSED")

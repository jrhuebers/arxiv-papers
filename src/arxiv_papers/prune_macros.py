#!/usr/bin/env python3
"""Conservative dead-macro elimination for a complete, flattened LaTeX file.

Only ordinary top-level preamble definitions are candidates. Redefinitions,
aliases, hooks, scoped definitions and uncertain syntax remain untouched. This
is source-level analysis, not a proof of equivalence under arbitrary TeX code.
"""
import argparse
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
import re

VERBATIM = {"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "alltt", "comment"}
FORMS = {r"\def", r"\newcommand", r"\renewcommand", r"\DeclareMathOperator", r"\let"}
PREFIXES = {r"\global", r"\long", r"\outer", r"\protected", r"\expandafter",
            r"\string", r"\noexpand", r"\meaning", r"\show"}
# These can be invoked by LaTeX/classes/packages without a literal call in the paper.
HOOKS = {
    r"\UrlFont", r"\UrlBreaks", r"\UrlBigBreaks", r"\UrlSpecials", r"\UrlNoBreaks",
    r"\eqref", r"\vec", r"\div", r"\Re", r"\Im", r"\P", r"\H", r"\L",
    r"\i", r"\j", r"\O", r"\S", r"\AA", r"\ae", r"\oe", r"\ss",
    r"\b", r"\c", r"\d", r"\r", r"\v", r"\u", r"\t", r"\k",
    r"\epsilon", r"\phi", r"\emptyset", r"\le", r"\ge", r"\cite",
    r"\arraystretch", r"\baselinestretch", r"\figurename", r"\tablename",
    r"\abstractname", r"\refname", r"\bibname", r"\contentsname",
    r"\labelitemi", r"\labelitemii", r"\labelitemiii", r"\labelitemiv",
    r"\labelenumi", r"\labelenumii", r"\labelenumiii", r"\labelenumiv",
    r"\And", r"\AND", r"\maketitle", r"\title", r"\author", r"\date",
}
UNSAFE_SYNTAX = {r"\catcode", r"\ExplSyntaxOn", r"\scantokens", r"\obeylines", r"\obeyspaces",
                 r"\@nameuse", r"\@namedef", r"\afterassignment", r"\aftergroup"}


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class Declaration:
    name: str
    form: str
    first: int
    after: int
    target: int
    removable: bool


def tokenize(text: str) -> list[Token]:
    """Tokenize ordinary TeX; comments and verbatim contents are opaque."""
    tokens = []
    i = 0
    while i < len(text):
        start = i
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "%":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        if ch == "\\":
            i += 1
            if i < len(text) and (text[i].isascii() and text[i].isalpha() or text[i] == "@"):
                while i < len(text) and (text[i].isascii() and text[i].isalpha() or text[i] == "@"):
                    i += 1
            elif i < len(text):
                i += 1
            command = text[start:i]
            if command in (r"\verb", r"\lstinline", r"\mintinline"):
                # Only the ordinary \verb form has a simple, unambiguous delimiter.
                # Unknown inline forms disable pruning below instead of being guessed.
                if command == r"\verb":
                    if i < len(text) and text[i] == "*":
                        i += 1
                    if i >= len(text) or text[i].isspace():
                        raise ValueError("malformed inline verbatim")
                    end = text.find(text[i], i + 1)
                    if end < 0:
                        raise ValueError("unterminated inline verbatim")
                    tokens.append(Token("<verbatim>", start, end + 1))
                    i = end + 1
                    continue
            if command == r"\begin":
                match = re.match(r"\s*\{([^{}]+)\}", text[i:])
                if match and match[1] in VERBATIM:
                    marker = r"\end{" + match[1] + "}"
                    end = text.find(marker, i + match.end())
                    if end < 0:
                        raise ValueError("unterminated verbatim environment")
                    i = end + len(marker)
                    tokens.append(Token("<verbatim>", start, i))
                    continue
        else:
            i += 1
        tokens.append(Token(text[start:i], start, i))
    return tokens


def group_end(tokens: list[Token], start: int, opening: str = "{") -> int:
    """Return the index after a balanced braced or optional argument."""
    closing = "}" if opening == "{" else "]"
    if start >= len(tokens) or tokens[start].value != opening:
        raise ValueError("missing argument")
    depth = 1
    i = start + 1
    while i < len(tokens):
        value = tokens[i].value
        if opening == "[" and value == "{":
            i = group_end(tokens, i)
            continue
        if value == opening and opening == "{":
            depth += 1
        elif value == closing:
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    raise ValueError("unbalanced argument")


def declaration(tokens: list[Token], first: int, removable: bool) -> Declaration:
    form = tokens[first].value
    i = first + 1
    if form != r"\def" and i < len(tokens) and tokens[i].value == "*":
        i += 1
    if i >= len(tokens):
        raise ValueError("missing macro name")
    if tokens[i].value == "{":
        after = group_end(tokens, i)
        if after != i + 3:
            raise ValueError("nonliteral macro name")
        target = i + 1
        i = after
    else:
        target = i
        i += 1
    name = tokens[target].value
    if not name.startswith("\\") or name in FORMS:
        raise ValueError("invalid macro name")
    if form == r"\let":
        if i < len(tokens) and tokens[i].value == "=":
            i += 1
        if i >= len(tokens):
            raise ValueError("missing alias value")
        after = i + 1
    else:
        if form == r"\def":
            while i < len(tokens) and tokens[i].value != "{":
                if tokens[i].value in ("}", r"\begin", r"\end"):
                    raise ValueError("invalid definition parameters")
                i += 1
        else:
            if i < len(tokens) and tokens[i].value == "[":
                i = group_end(tokens, i, "[")
                if i < len(tokens) and tokens[i].value == "[":
                    i = group_end(tokens, i, "[")
        after = group_end(tokens, i)
    return Declaration(name, form, first, after, target,
                       removable and form not in (r"\renewcommand", r"\let"))


def analyze(text: str, protected_names=()) -> tuple[list[Token], list[Declaration], set[str]]:
    tokens = tokenize(text)
    values = {t.value for t in tokens}
    opaque_refs = set()
    for token in tokens:
        if token.value != "<verbatim>":
            continue
        raw = text[token.start:token.end]
        # alltt and listings/minted escape options can execute TeX. Keeping every
        # mentioned command is conservative; dynamic names make that insufficient.
        match = re.match(r"\\begin\s*\{([^{}]+)\}", raw)
        environment = match[1] if match else ""
        if environment == "alltt" or re.search(r"\\(?:csname|@nameuse|catcode)\b", raw):
            raise ValueError("executable/dynamic verbatim content")
        if environment in ("lstlisting", "minted"):
            opaque_refs.update(re.findall(r"\\(?:[A-Za-z@]+|[^\s])", raw))
    if values & (UNSAFE_SYNTAX | {r"\lstinline", r"\mintinline"}):
        raise ValueError("unsupported tokenization/dynamic syntax")
    # Literal csname references are searchable too; computed names are not.
    dynamic_refs = set()
    for i, token in enumerate(tokens):
        if token.value != r"\csname":
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].value != r"\endcsname":
            if tokens[j].value.startswith("\\") or tokens[j].value in ("{", "}"):
                raise ValueError("computed control-sequence name")
            j += 1
        if j == len(tokens):
            raise ValueError("unterminated csname")
        dynamic_refs.add("\\" + "".join(t.value for t in tokens[i + 1:j]))
    declarations = []
    depth = conditional = environments = command_groups = 0
    in_document = False
    i = 0
    while i < len(tokens):
        value = tokens[i].value
        if value in FORMS:
            previous = tokens[i - 1].value if i else ""
            # An arbitrary preceding macro can expand to an assignment prefix.
            removable = not (in_document or depth or command_groups or conditional or environments or previous.startswith("\\"))
            item = declaration(tokens, i, removable)
            declarations.append(item)
            i = item.after
            continue
        if value in (r"\begingroup", r"\bgroup") and depth == 0:
            command_groups += 1
        elif value in (r"\endgroup", r"\egroup") and depth == 0:
            command_groups -= 1
            if command_groups < 0:
                raise ValueError("unbalanced command groups")
        elif value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced document braces")
        elif value in (r"\begin", r"\end"):
            if i + 3 < len(tokens) and tokens[i + 1].value == "{":
                end = group_end(tokens, i + 1)
                env = "".join(t.value for t in tokens[i + 2:end - 1])
                if value == r"\begin":
                    environments += 1
                    if env == "document":
                        in_document = True
                else:
                    environments = max(0, environments - 1)
        elif value.startswith(r"\if") and depth == 0:
            # Includes unknown conditionals: over-retaining is safer than guessing.
            conditional += 1
        elif value == r"\fi" and depth == 0:
            conditional = max(0, conditional - 1)
        i += 1
    if depth or command_groups:
        raise ValueError("unbalanced document groups")

    by_name = defaultdict(list)
    for item in declarations:
        by_name[item.name].append(item)
    names = set(by_name)
    # Aliases to grouping/conditional/assignment primitives defeat lexical scope.
    structural = PREFIXES | FORMS | UNSAFE_SYNTAX | {r"\begingroup", r"\endgroup", r"\bgroup", r"\egroup",
                                                   r"\begin", r"\end", r"\else", r"\fi", r"\csname"}
    for item in declarations:
        if item.form == r"\let":
            alias = tokens[item.after - 1].value
        else:
            # Literal wrappers such as \def\myglobal{\global} are aliases too.
            alias = tokens[item.after - 2].value if tokens[item.after - 3].value == "{" else ""
        if alias in structural or alias.startswith(r"\if"):
            raise ValueError("alias to structural TeX command")
    protected_names = set(protected_names)
    protected_names.update(name.split("@", 1)[0] for name in list(protected_names) if "@" in name)
    roots = (protected_names | HOOKS | dynamic_refs | opaque_refs) & names
    # External package code can construct names dynamically. Protect every name
    # matching its known literal prefix; an unknown prefix protects all names.
    for pattern in protected_names:
        if "*" in pattern:
            roots.update(name for name in names if fnmatchcase(name, pattern))
    for name, items in by_name.items():
        if (any(not item.removable for item in items) or "@" in name or
                name.startswith((r"\the", r"\end")) or len(name) == 2 and not name[-1].isalnum()):
            roots.add(name)
    # Without tracking makeatletter, a token like \foo@bar may mean either the
    # internal command or a call to \foo followed by literal text. Retain both.
    roots.update(t.value.split("@", 1)[0] for t in tokens if "@" in t.value and t.value.split("@", 1)[0] in names)
    dependencies = defaultdict(set)
    covered = set()
    for item in declarations:
        covered.update(range(item.first, item.after))
        dependencies[item.name].update(t.value for n, t in enumerate(tokens[item.first:item.after], item.first)
                                       if n != item.target and t.value in names)
    roots.update(t.value for i, t in enumerate(tokens) if i not in covered and t.value in names)
    live = set(roots)
    pending = list(roots)
    while pending:
        name = pending.pop()
        for dependency in dependencies[name] - live:
            live.add(dependency)
            pending.append(dependency)
    return tokens, declarations, live


def prune_unused_macros(text: str, mode: str = "safe", protected_names=()) -> str:
    """Remove unreachable ordinary preamble macros, or return input on uncertainty.

    No rewrite happens inside the document body, groups, conditionals or retained
    declarations. Deletions become newlines so adjacent tokens cannot concatenate.
    """
    if mode not in ("safe", "off"):
        raise ValueError(f"unknown macro-pruning mode: {mode}")
    if mode == "off":
        return text
    try:
        tokens, declarations, live = analyze(text, protected_names)
    except ValueError:
        return text
    out = []
    pos = 0
    for item in declarations:
        if item.name in live:
            continue
        start, end = tokens[item.first].start, tokens[item.after - 1].end
        out.append(text[pos:start])
        out.append("\n")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def external_macro_names(root: Path) -> set[str]:
    """Names (and conservative prefix* patterns) mentioned in local support code."""
    names = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in {".sty", ".cls", ".def", ".clo", ".cfg", ".bbl", ".bib", ".bst"}:
            continue
        # Deliberately over-approximate: even comments may document package hooks.
        text = path.read_text(encoding="utf-8", errors="replace")
        names.update(re.findall(r"\\(?:[A-Za-z@]+|[^\s])", text))
        try:
            packed = "".join(t.value for t in tokenize(text))
        except ValueError:
            names.add("*")
            continue
        for match in re.finditer(r"\\csname(.*?)\\endcsname|\\@nameuse\{([^{}]*)\}", packed):
            body = match[1] if match[1] is not None else match[2]
            if re.fullmatch(r"[A-Za-z@]+", body):
                names.add("\\" + body)
            elif re.fullmatch(r"(?:[A-Za-z@]|#[1-9])+", body):
                names.add("\\" + re.sub(r"#[1-9]", "*", body))
            else:
                prefix = re.match(r"[A-Za-z@]*", body)[0]
                names.add("\\" + prefix + "*")
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, help="new output path; never overwrites an existing file")
    parser.add_argument("--prune-macros", choices=("safe", "off"), default="safe")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a new path")
    text = args.input.read_text(encoding="utf-8")
    result = prune_unused_macros(text, args.prune_macros, external_macro_names(args.input.parent))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(result)


if __name__ == "__main__":
    main()

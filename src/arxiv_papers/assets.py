"""Materialize source assets and remap literal TeX references to corpus paths."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import sys

from .prune_macros import group_end, tokenize

IMAGE_EXTENSIONS = {".bmp", ".eps", ".gif", ".jpeg", ".jpg", ".pdf", ".png", ".ps", ".svg", ".tif", ".tiff", ".webp"}
BIB_EXTENSIONS = {".bib", ".bbl", ".bst", ".bcf"}
GRAPHICS_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg", ".ps", ".tif", ".tiff", ".bmp", ".gif", ".webp")
GRAPHICS_COMMANDS = {r"\includegraphics", r"\pgfimage", r"\includepdf", r"\includesvg"}


def copy_assets(source_root: Path, paper_dir: Path) -> dict[str, str]:
    """Return an explicit source-relative -> paper-relative map; preserve filenames."""
    mapping = {}
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source_root).as_posix()
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            destination = "figures/" + relative
        elif path.suffix.lower() in BIB_EXTENSIONS:
            destination = "bibliography/" + relative
        else:
            continue
        output = paper_dir / destination
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
        mapping[relative] = destination
    return mapping


def literal_path(value: str) -> str | None:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    value = re.sub(r"\\space\s*", " ", value)
    if not value or any(char in value for char in "\\#{}%"):
        return None
    return value


@dataclass
class Resolver:
    mapping: dict[str, str]
    cwd: str = "."

    def normalize(self, path: str) -> str | None:
        path = posixpath.normpath(path)
        if path.startswith("/") or path == ".." or path.startswith("../"):
            return None
        return path.removeprefix("./")

    def resolve(self, target: str, extensions: tuple[str, ...], directories=()) -> str | None:
        """Exact lookup first; repair latexpand import prefixes only when unique."""
        if self.normalize(posixpath.join(self.cwd, target)) is None:
            return None
        names = [target] if PurePosixPath(target).suffix else [target + ext for ext in extensions]
        bases = list(dict.fromkeys([self.cwd] + list(directories)))

        def lookup(candidates):
            # graphicx searches the extension list first, directories second.
            for name in candidates:
                for base in bases:
                    key = self.normalize(posixpath.join(base, name))
                    if key in self.mapping:
                        return self.mapping[key]
            # Root-relative layouts are a fallback, not a shadowing search path.
            for name in candidates:
                key = self.normalize(name)
                if key in self.mapping:
                    return self.mapping[key]
            return None

        exact = lookup(names)
        if exact is not None:
            return exact
        # Source lookup has priority: Figures/foo.pdf can itself be an original
        # path even when another asset's canonical destination has that spelling.
        for name in names:
            if name in self.mapping.values():
                return name
        # latexpand may prepend Sections/ to a path originally rooted at Figures/.
        # Prefer the longest matching suffix; never choose arbitrarily by basename.
        parts = PurePosixPath(target).parts
        for dropped in range(1, len(parts)):
            suffix = PurePosixPath(*parts[dropped:]).as_posix()
            suffixes = [suffix] if PurePosixPath(suffix).suffix else [suffix + ext for ext in extensions]
            repaired = lookup(suffixes)
            if repaired is not None:
                return repaired
            for name in suffixes:
                matches = [dest for source, dest in self.mapping.items()
                           if source == name or source.endswith("/" + name)]
                if len(set(matches)) > 1:
                    raise ValueError(f"ambiguous asset reference {target!r}: {', '.join(sorted(set(matches)))}")
                if matches:
                    return matches[0]
        # Unqualified filenames may live in a unique directory absent from graphicspath.
        if len(parts) == 1:
            for name in names:
                matches = [dest for source, dest in self.mapping.items() if PurePosixPath(source).name == name]
                if len(set(matches)) > 1:
                    raise ValueError(f"ambiguous asset reference {target!r}: {', '.join(sorted(set(matches)))}")
                if matches:
                    return matches[0]
        return None

    def directory(self, target: str) -> tuple[str, str] | None:
        """Resolve a graphics directory to its source and materialized prefixes."""
        for base in dict.fromkeys((self.cwd, ".")):
            key = self.normalize(posixpath.join(base, target))
            if key is None:
                continue
            prefix = "" if key == "." else key.rstrip("/") + "/"
            if any(source.startswith(prefix) and PurePosixPath(source).suffix.lower() in IMAGE_EXTENSIONS for source in self.mapping):
                return prefix, "figures/" + prefix
        if target.startswith("figures/") and any(value.startswith(target.rstrip("/") + "/") for value in self.mapping.values()):
            return target.removeprefix("figures/").rstrip("/") + "/", target.rstrip("/") + "/"
        return None


def rewrite_asset_paths(text: str, mapping: dict[str, str], *, cwd: str = ".") -> str:
    """Rewrite only parsed literal path arguments, retaining opaque/dynamic code.

    Missing literals are diagnosed but retained (some sources generate images at
    build time). Ambiguous references are errors. No source file is modified.
    """
    if not mapping or "+asset-paths-remapped" in "\n".join(text.splitlines()[:2]):
        return text
    tokens = tokenize(text)
    if any(t.value in (r"\lstinline", r"\mintinline") for t in tokens):
        print("assets: unsupported inline-code syntax; preserving TeX without path rewriting", file=sys.stderr)
        return text
    resolver = Resolver(mapping, cwd)
    changes = []
    directories = []
    dynamic_directories = False
    extensions = GRAPHICS_EXTENSIONS
    scopes = []
    environments = []
    warned = set()
    preamble_path = False
    document_start = None
    graphics_seen = False
    depth = 0
    i = 0

    def argument(index):
        after = group_end(tokens, index)
        return text[tokens[index].end:tokens[after - 1].start], after

    def rewrite_literal(raw, command):
        target = literal_path(raw)
        if target is None:
            return None
        if command in GRAPHICS_COMMANDS and dynamic_directories:
            return None  # Unknown search state must not select an arbitrary image.
        selected_extensions = extensions
        if command == r"\includesvg":
            selected_extensions = (".svg",)
        elif command in (r"\bibliography", r"\addbibresource"):
            selected_extensions = (".bib",)
        elif command == r"\bibliographystyle":
            selected_extensions = (".bst",)
        resolved = resolver.resolve(target, selected_extensions, directories if command in GRAPHICS_COMMANDS else ())
        if resolved is None:
            # Standard bibliography styles (plainnat, etc.) come from TeX, not the archive.
            if command != r"\bibliographystyle" and (command, target) not in warned:
                print(f"assets: unresolved {command} path {target!r}; leaving it unchanged", file=sys.stderr)
                warned.add((command, target))
            return None
        if command in (r"\bibliography", r"\bibliographystyle"):
            resolved = str(PurePosixPath(resolved).with_suffix(""))
        return resolved

    while i < len(tokens):
        value = tokens[i].value
        if value in ("{", r"\begingroup", r"\bgroup"):
            scopes.append((list(directories), dynamic_directories, extensions))
            depth += 1
        elif value in ("}", r"\endgroup", r"\egroup") and scopes:
            directories, dynamic_directories, extensions = scopes.pop()
            depth -= 1
        if value in (r"\begin", r"\end") and i + 1 < len(tokens) and tokens[i + 1].value == "{":
            env, _ = argument(i + 1)
            if value == r"\begin":
                environments.append((env, list(directories), dynamic_directories, extensions))
                if env == "document" and document_start is None:
                    document_start = tokens[i].start
            elif environments and environments[-1][0] == env:
                _, directories, dynamic_directories, extensions = environments.pop()
        if value not in GRAPHICS_COMMANDS | {r"\graphicspath", r"\DeclareGraphicsExtensions", r"\bibliography", r"\addbibresource", r"\bibliographystyle"}:
            i += 1
            continue
        j = i + 1
        if j < len(tokens) and tokens[j].value == "*":
            j += 1
        if j < len(tokens) and tokens[j].value == "[":
            j = group_end(tokens, j, "[")
        if j >= len(tokens) or tokens[j].value != "{":
            i += 1
            continue
        raw, after = argument(j)
        replacement = None
        if value == r"\graphicspath":
            paths = []
            source_dirs = []
            dynamic = False
            k = j + 1
            while k < after - 1 and tokens[k].value == "{":
                entry, next_k = argument(k)
                literal = literal_path(entry)
                resolved = resolver.directory(literal) if literal is not None else None
                if literal is None:
                    dynamic = True
                    material_cwd = "figures/" + (cwd.rstrip("/") + "/" if cwd != "." else "")
                    paths.extend([material_cwd + entry, entry])
                elif resolved is None:
                    paths.append(entry)
                else:
                    source_dirs.append(resolved[0])
                    paths.append(resolved[1])
                k = next_k
            if k == after - 1:
                # The root fallback also resolves old paths passed through image wrappers.
                material_cwd = "figures/" + (cwd.rstrip("/") + "/" if cwd != "." else "")
                paths = [material_cwd] + paths + ["figures/"]
                replacement = "".join("{" + path + "}" for path in dict.fromkeys(paths))
                directories = source_dirs
                dynamic_directories = dynamic
                if document_start is None and depth == 0 and not environments:
                    preamble_path = True
            else:
                # A macro may supply the entire directory list, not just one
                # entry. Do not guess filenames or overwrite that search state.
                directories = []
                dynamic_directories = True
                if document_start is None and depth == 0 and not environments:
                    preamble_path = True
                print("assets: dynamic graphicspath list; retaining associated graphics references", file=sys.stderr)
        elif value == r"\DeclareGraphicsExtensions":
            listed = tuple(item.strip() for item in raw.split(","))
            if all(re.fullmatch(r"\.[A-Za-z0-9]+", ext) for ext in listed):
                extensions = listed
        elif value == r"\bibliography":
            entries = raw.split(",")
            replacement = ",".join(rewrite_literal(entry, value) or entry for entry in entries)
        else:
            graphics_seen |= value in GRAPHICS_COMMANDS
            replacement = rewrite_literal(raw, value)
        if replacement is not None and replacement != raw:
            changes.append((tokens[j].end, tokens[after - 1].start, replacement))
        i = after

    if graphics_seen and not preamble_path and document_start is not None:
        prefixes = ["figures/" + (cwd.rstrip("/") + "/" if cwd != "." else ""), "figures/"]
        paths = "".join("{" + prefix + "}" for prefix in dict.fromkeys(prefixes))
        changes.append((document_start, document_start,
                        r"\ifdefined\graphicspath\graphicspath{" + paths + r"}\fi" + "\n"))
    result = text
    for start, end, replacement in sorted(changes, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result

"""Editing one value in a hand-written YAML file without rewriting the rest of it.

**``~/.agentjobs/dispatch.yaml`` is edited by hand far more than by a machine, and its
comments are operational documentation**: why a limit has the value it has, which
setting is conditional on a task landing, which model ids were checked and when. Until
task-198 the two writers a browser can reach -- project enablement and idle-session
enforcement -- loaded the file, changed one key, and wrote it back through
``yaml.safe_dump``. That cannot keep a comment, so one click on "Enable dispatch" deleted
every one of them and reflowed what was left. It has already caused a wrong diagnosis:
a surviving ``require_clean_tree: false`` lost the comment saying it was temporary.

**The fix edits text, not a parsed document.** PyYAML's composer reports where every
node starts and ends in the source, so a value can be replaced by splicing exactly its
own characters and a new key can be inserted after the last line of its mapping. Every
byte outside those spans is the byte that was there: comments, blank lines, quoting,
flow-style argv lists spread over two lines, line endings.

``ruamel.yaml`` round-trip mode was the other candidate and was rejected. It re-emits the
whole document from its own tree, so it keeps comments but not layout: a flow sequence
written across two lines comes back on one, indentation is normalised to one width, and
long scalars are refolded. That is a new dependency to get most of the way.

**Nothing here is trusted on its own say-so.** :func:`patch_yaml` returns the new text
only after parsing it and comparing it with the mapping the caller expects; a shape this
module does not handle -- a flow mapping it would have to grow, a key that does not start
its own line -- raises :class:`UnpatchableYaml` rather than guessing, and so does a
result that parses to anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

__all__ = ["Delete", "Edit", "Set", "UnpatchableYaml", "patch_yaml"]


class UnpatchableYaml(ValueError):
    """The edit cannot be made by splicing text, so it was not made at all."""


@dataclass(frozen=True)
class Set:
    """Make the scalar at ``path`` equal ``value``, creating missing mappings on the way."""

    path: Tuple[str, ...]
    value: Union[str, int, bool, None]


@dataclass(frozen=True)
class Delete:
    """Remove the key at ``path`` and its value. A missing key is not an error."""

    path: Tuple[str, ...]


Edit = Union[Set, Delete]


def patch_yaml(text: str, edits: Sequence[Edit], expected: Mapping[str, Any]) -> str:
    """Apply ``edits`` to ``text`` and return it, provided it then parses to ``expected``.

    ``expected`` is the caller's own account of the result, normally the same edits made
    to a ``yaml.safe_load`` of ``text``. The comparison is the guarantee that this is a
    formatting-preserving write and never a change of meaning.
    """
    for edit in edits:
        if isinstance(edit, Set):
            text = _set(text, edit.path, edit.value)
        else:
            text = _delete(text, edit.path)
    try:
        result = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UnpatchableYaml(f"the patched text no longer parses: {exc}") from exc
    if result != dict(expected):
        raise UnpatchableYaml("the patched text parses to something other than intended")
    return text


# ----- locating ---------------------------------------------------------------


def _root(text: str) -> MappingNode:
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(node, MappingNode) or node.flow_style:
        raise UnpatchableYaml("the document is not a block mapping")
    return node


def _find(node: MappingNode, key: str) -> Optional[Tuple[Node, Node]]:
    """The last pair whose key is ``key``, which is the one ``safe_load`` keeps."""
    found = None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            found = (key_node, value_node)
    return found


def _is_empty_scalar(node: Node) -> bool:
    """``key:`` with nothing after it, which PyYAML composes as a zero-width scalar."""
    return isinstance(node, ScalarNode) and node.start_mark.index == node.end_mark.index


def _content_end(node: Node) -> int:
    """Index just past the last character that belongs to ``node``.

    A block collection's own end mark sits at whatever token follows it, which may be
    several comment lines further on, so it is found through its last child instead.
    """
    if isinstance(node, MappingNode) and not node.flow_style and node.value:
        key_node, value_node = node.value[-1]
        if _is_empty_scalar(value_node):
            return int(value_node.end_mark.index)
        return max(int(key_node.end_mark.index), _content_end(value_node))
    if isinstance(node, SequenceNode) and not node.flow_style and node.value:
        return _content_end(node.value[-1])
    return node.end_mark.index


def _line_start(text: str, index: int) -> int:
    return max(text.rfind("\n", 0, index), text.rfind("\r", 0, index)) + 1


def _line_end(text: str, index: int) -> int:
    """Index of the line break that ends the line holding ``index``, or the text's end."""
    # A block scalar ends after its own line break, so its line is already over.
    if text[max(index - 2, 0) : index] == "\r\n":
        return index - 2
    if index > 0 and text[index - 1] in "\r\n":
        return index - 1
    position = index
    while position < len(text) and text[position] not in "\r\n":
        position += 1
    return position


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


# ----- rendering --------------------------------------------------------------


def _scalar(value: Any) -> str:
    rendered = yaml.safe_dump(value, default_flow_style=True, allow_unicode=False, width=10**6)
    rendered = rendered.rstrip("\n")
    if rendered.endswith("\n..."):
        rendered = rendered[: -len("\n...")]
    if "\n" in rendered:
        raise UnpatchableYaml(f"{value!r} does not render as a single-line scalar")
    return rendered


def _block(path: Sequence[str], value: Any, indent: int, step: int, newline: str) -> str:
    """``path`` as nested block keys ending in ``value``, one line each, no final break."""
    lines: List[str] = []
    for depth, key in enumerate(path):
        pad = " " * (indent + depth * step)
        if depth == len(path) - 1:
            lines.append(f"{pad}{_scalar(key)}: {_scalar(value)}")
        else:
            lines.append(f"{pad}{_scalar(key)}:")
    return newline.join(lines)


def _step(node: MappingNode, default: int = 2) -> int:
    """The indentation width this mapping's children already use, else ``default``."""
    for key_node, value_node in node.value:
        if isinstance(value_node, MappingNode) and not value_node.flow_style and value_node.value:
            width = value_node.value[0][0].start_mark.column - key_node.start_mark.column
            if width > 0:
                return int(width)
    return default


# ----- editing ----------------------------------------------------------------


def _set(text: str, path: Tuple[str, ...], value: Any) -> str:
    root = _root(text)
    node: MappingNode = root
    step = _step(root)
    newline = _newline(text)

    for depth, key in enumerate(path):
        pair = _find(node, key)
        last = depth == len(path) - 1

        if pair is None:
            return _insert_into(text, node, path[depth:], value, step, newline)

        key_node, value_node = pair
        if last:
            if _is_empty_scalar(value_node):
                at = value_node.start_mark.index
                return f"{text[:at]} {_scalar(value)}{text[at:]}"
            if not isinstance(value_node, ScalarNode):
                raise UnpatchableYaml(f"{'.'.join(path)} holds a collection, not a scalar")
            start, end = value_node.start_mark.index, value_node.end_mark.index
            if yaml.safe_load(text[start:end]) == value:
                return text  # already so; leave its quoting exactly as written
            return f"{text[:start]}{_scalar(value)}{text[end:]}"

        if isinstance(value_node, MappingNode) and not value_node.flow_style:
            step = _step(value_node, step)
            node = value_node
            continue

        # `key:` or `key: {}` -- an empty mapping written inline. Replace the inline part
        # with nothing and hang a block under the key's own line.
        if _is_empty_scalar(value_node) or (
            isinstance(value_node, MappingNode) and value_node.flow_style and not value_node.value
        ):
            start, end = value_node.start_mark.index, value_node.end_mark.index
            while start > 0 and text[start - 1] in " \t":
                start -= 1
            text = text[:start] + text[end:]
            at = _line_end(text, start)
            block = _block(
                path[depth + 1 :], value, key_node.start_mark.column + step, step, newline
            )
            return f"{text[:at]}{newline}{block}{text[at:]}"

        raise UnpatchableYaml(f"{'.'.join(path[: depth + 1])} is not a block mapping")

    raise UnpatchableYaml("an empty path names nothing")  # pragma: no cover


def _insert_into(
    text: str,
    node: MappingNode,
    path: Sequence[str],
    value: Any,
    step: int,
    newline: str,
) -> str:
    """Append ``path: value`` after the last line of the block mapping ``node``."""
    if node.flow_style or not node.value:
        raise UnpatchableYaml("cannot add a key to a flow-style mapping")
    indent = node.value[0][0].start_mark.column
    at = _line_end(text, _content_end(node))
    block = _block(path, value, indent, step, newline)
    if at == len(text) and not text.endswith(("\n", "\r")):
        return f"{text}{newline}{block}"
    return f"{text[:at]}{newline}{block}{text[at:]}"


def _delete(text: str, path: Tuple[str, ...]) -> str:
    node: MappingNode = _root(text)
    for key in path[:-1]:
        pair = _find(node, key)
        if pair is None:
            return text
        child = pair[1]
        if not isinstance(child, MappingNode):
            return text
        if child.flow_style:
            raise UnpatchableYaml(f"{key} is a flow-style mapping")
        node = child

    pair = _find(node, path[-1])
    if pair is None:
        return text
    key_node, value_node = pair
    if len(node.value) == 1:
        raise UnpatchableYaml("removing the only key would leave an empty mapping")
    begin = _line_start(text, key_node.start_mark.index)
    if text[begin : key_node.start_mark.index].strip():
        raise UnpatchableYaml(f"{'.'.join(path)} does not start its own line")
    end = _line_end(text, max(key_node.end_mark.index, _content_end(value_node)))
    if text[end : end + 2] == "\r\n":
        end += 2
    elif text[end : end + 1] in ("\n", "\r"):
        end += 1
    return text[:begin] + text[end:]

"""Preserve accessible Treeview interaction state across dashboard refreshes."""
from __future__ import annotations


def snapshot(tree) -> dict:
    """Capture only transient UI state; this never changes profile data."""
    items = []
    stack = list(tree.get_children(""))
    while stack:
        item = stack.pop()
        items.append(item)
        stack.extend(tree.get_children(item))
    return {
        "had_items": bool(items),
        "open": tuple(item for item in items if bool(tree.item(item, "open"))),
        "selection": tuple(tree.selection()),
        "focus": tree.focus(),
        "yview": tuple(tree.yview()),
    }


def restore(tree, state: dict, fallback_iid: str = "") -> None:
    """Restore still-existing nodes and fall back to the active profile once."""
    exists = tree.exists
    if state.get("had_items"):
        open_items = set(state.get("open", ()))
        stack = list(tree.get_children(""))
        while stack:
            item = stack.pop()
            if exists(item):
                tree.item(item, open=item in open_items)
                stack.extend(tree.get_children(item))

    selection = [item for item in state.get("selection", ()) if exists(item)]
    if not selection and fallback_iid and exists(fallback_iid):
        selection = [fallback_iid]
    if selection:
        tree.selection_set(*selection)
    if state.get("focus") and exists(state["focus"]):
        tree.focus(state["focus"])
    elif selection:
        tree.focus(selection[0])

    yview = state.get("yview", ())
    if yview:
        try:
            tree.yview_moveto(float(yview[0]))
        except (TypeError, ValueError):
            pass

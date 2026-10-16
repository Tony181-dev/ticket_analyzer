"""Publish to Collab (Confluence) via mcp-atlassian: create, or update on duplicate.

Duplicate handling follows the legacy Step 6 -> Step 7 logic: when create fails
because the title exists, find the existing page id (from the error text, the
parent's children, or a title lookup) and update that page instead.
"""
import html
import json
import re

from .. import mcp


def _nested(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _nested(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested(item)
    elif isinstance(value, str) and value.strip()[:1] in "{[":
        try:
            yield from _nested(json.loads(value))
        except ValueError:
            pass


def _is_error(result):
    return any(isinstance(x, dict) and x.get("isError") is True for x in _nested(result))


def _is_duplicate(result):
    text = html.unescape(mcp.result_text(result)).casefold()
    return _is_error(result) and any(m in text for m in (
        "already exists", "page with this title already exists", "duplicatepage"))


def _page_ids(result, title):
    """Page ids whose title matches exactly, then any explicit id."""
    want = html.unescape(title).strip().casefold()
    exact, loose = [], []
    for item in _nested(result):
        if not isinstance(item, dict):
            continue
        for key in ("id", "pageId", "page_id", "existingPageId"):
            value = str(item.get(key) or "").strip()
            if value.isdigit():
                (exact if html.unescape(str(item.get("title", ""))).strip().casefold() == want else loose).append(value)
    text = mcp.result_text(result) if isinstance(result, dict) else ""
    loose += re.findall(r"(?i)pageId=(\d+)", text)
    return exact + loose


def _page_url(result):
    for item in _nested(result):
        if isinstance(item, dict):
            for key in ("url", "webui", "link"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return ""


def find_existing(space_key, title, parent_id=""):
    if parent_id:
        try:
            children = mcp.call("collab", "confluence_get_page_children", {"parent_id": parent_id, "limit": 50})
            ids = _page_ids(children, title)
            if ids:
                return ids[0]
        except mcp.McpError:
            pass
    try:
        page = mcp.call("collab", "confluence_get_page",
                        {"title": title, "space_key": space_key, "include_metadata": True, "convert_to_markdown": True})
        ids = _page_ids(page, title)
        return ids[0] if ids and not _is_error(page) else ""
    except mcp.McpError:
        return ""


def publish(space_key, title, storage, parent_id="", log=print):
    args = {"space_key": space_key, "title": title, "content": storage, "content_format": "storage"}
    if parent_id:
        args["parent_id"] = parent_id
    log(f"confluence_create_page: space={space_key} title={title!r}")
    created = mcp.call("collab", "confluence_create_page", args)
    if not _is_error(created):
        ids = _page_ids(created, title)
        return {"action": "created", "page_id": ids[0] if ids else "", "url": _page_url(created), "result": created}
    if not _is_duplicate(created):
        raise RuntimeError("Collab create failed: " + mcp.result_text(created)[:1500])
    page_id = (_page_ids(created, title) or [""])[0] or find_existing(space_key, title, parent_id)
    if not page_id:
        raise RuntimeError("Collab reports the page already exists, but its page id could not be found.")
    log(f"Page exists (id {page_id}) -> confluence_update_page")
    updated = mcp.call("collab", "confluence_update_page", {
        "page_id": page_id, "title": title, "content": storage, "content_format": "storage",
        "is_minor_edit": False, "version_comment": "Updated via Ticket Analyzer",
    })
    if _is_error(updated):
        raise RuntimeError("Collab update failed: " + mcp.result_text(updated)[:1500])
    return {"action": "updated", "page_id": page_id, "url": _page_url(updated), "result": updated}

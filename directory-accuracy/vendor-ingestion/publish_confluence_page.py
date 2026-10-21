#!/usr/bin/env python3
"""Publish vendor-ingestion.md to Confluence as a child of the "Directory accuracy" page.

Converts the markdown to Confluence storage format (python-markdown: tables, fenced code),
turns fenced code blocks into Confluence code macros, and creates the page under the parent. If a page with the same title already exists under the parent, it is updated
(version + 1) instead of duplicated.

Safety: dry run by default. It writes the converted body next to this script
(`confluence-body.storage.html`) and prints the title, parent, size, and what it would do.
`--apply` performs the create or update.

Usage:
  export ATLASSIAN_EMAIL="dev.pandey@certifyos.com"
  export ATLASSIAN_TOKEN="<api token from id.atlassian.com>"
  python3 publish_confluence_page.py            # dry run
  python3 publish_confluence_page.py --apply    # create or update the page
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import markdown

SITE = "https://certifyos.atlassian.net"
PARENT_PAGE_ID = "2252603415"  # Engineering → PDM → Directory accuracy
TITLE = "CP-40437 - Design: Directory accuracy vendor ingestion"
SOURCE = Path(__file__).with_name("vendor-ingestion.md")
OUT = Path(__file__).with_name("confluence-body.storage.html")

CODE_MACRO = (
    '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">{lang}</ac:parameter>'
    "<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>"
)


def api(path, method="GET", payload=None):
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_TOKEN")
    if not email or not token:
        sys.exit("Set ATLASSIAN_EMAIL and ATLASSIAN_TOKEN environment variables first.")
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SITE + path, data=data, method=method)
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode() or "{}")


def convert(md: str) -> str:
    # Drop the H1: Confluence shows the page title.
    md = re.sub(r"^# .*\n", "", md, count=1)
    html = markdown.markdown(md, extensions=["tables", "fenced_code"])

    def to_macro(m):
        lang = m.group(1) or "text"
        body = m.group(2)
        body = (body.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&amp;", "&"))
        return CODE_MACRO.format(lang={"js": "javascript", "bash": "bash", "json": "json"}.get(lang, "text"), body=body)

    html = re.sub(r'<pre><code(?: class="language-(\w+)")?>(.*?)</code></pre>', to_macro, html, flags=re.S)
    html = html.replace("<br/>", "<br />")
    # Pipes escaped for markdown tables stay escaped inside code spans; unescape after parsing.
    html = html.replace("\\|", "|")
    # Local images become attachments: <img src="x.png"> -> ac:image on the attachment of the same name.
    html = re.sub(
        r'<img alt="([^"]*)" src="([^"/]+\.(?:png|jpg|jpeg|svg))"\s*/?>',
        lambda m: (f'<ac:image ac:alt="{m.group(1)}" ac:width="1200">'
                   f'<ri:attachment ri:filename="{m.group(2)}" /></ac:image>'),
        html,
    )
    return html


def local_images(md: str):
    return sorted(set(re.findall(r'!\[[^\]]*\]\(([^)/]+\.(?:png|jpg|jpeg|svg))\)', md)))


def upload_attachment(page_id: str, path: Path):
    """Create or update one attachment (v1 API; PUT is create-or-update by filename)."""
    email = os.environ["ATLASSIAN_EMAIL"]
    token = os.environ["ATLASSIAN_TOKEN"]
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    boundary = "----vendoringestion" + os.urandom(8).hex()
    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml"}[path.suffix[1:].lower()]
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        path.read_bytes(), b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="minorEdit"\r\n\r\ntrue\r\n',
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(f"{SITE}/wiki/rest/api/content/{page_id}/child/attachment", data=body, method="PUT")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("X-Atlassian-Token", "nocheck")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode() or "{}")
    att = (result.get("results") or [result])[0]
    return att.get("id"), att.get("version", {}).get("number")


def find_existing():
    q = urllib.parse.urlencode({"title": TITLE, "parent-id": PARENT_PAGE_ID, "limit": 5})
    # v2 pages list does not filter by parent directly; filter client-side.
    result = api(f"/wiki/api/v2/pages?title={urllib.parse.quote(TITLE)}&limit=10")
    for page in result.get("results", []):
        if str(page.get("parentId")) == PARENT_PAGE_ID:
            return page
    return None


def main():
    apply = "--apply" in sys.argv
    md = SOURCE.read_text()
    body = convert(md)
    OUT.write_text(body)
    images = [SOURCE.with_name(name) for name in local_images(md)]
    missing = [p.name for p in images if not p.exists()]
    if missing:
        sys.exit(f"image referenced in the markdown but not found next to it: {missing}")
    print(f"title:   {TITLE}")
    print(f"parent:  {PARENT_PAGE_ID}")
    print(f"body:    {len(body.encode()) / 1024:.0f} KB storage format -> {OUT.name}")
    print(f"images:  {[p.name for p in images] or 'none'} (uploaded as attachments)")

    parent = api(f"/wiki/api/v2/pages/{PARENT_PAGE_ID}")
    space_id = parent["spaceId"]
    existing = find_existing()
    if existing:
        print(f"exists:  page {existing['id']} (version {existing['version']['number']}) -> would UPDATE")
    else:
        print("exists:  no -> would CREATE")
    if not apply:
        print("dry run; re-run with --apply to write.")
        return

    # Attachments must exist before the body that references them is saved, or the
    # image shows "preview unavailable" until the page is re-saved.
    if not existing:
        existing = api("/wiki/api/v2/pages", "POST", {
            "spaceId": space_id,
            "status": "current",
            "title": TITLE,
            "parentId": PARENT_PAGE_ID,
            "body": {"representation": "storage", "value": "<p>publishing…</p>"},
        })
        print(f"created: page {existing['id']} (placeholder body)")
    for image in images:
        att_id, att_version = upload_attachment(existing["id"], image)
        print(f"attach:  {image.name} -> attachment {att_id} version {att_version}")
    result = api(f"/wiki/api/v2/pages/{existing['id']}", "PUT", {
        "id": existing["id"],
        "status": "current",
        "title": TITLE,
        "body": {"representation": "storage", "value": body},
        "version": {"number": existing["version"]["number"] + 1, "message": "Refresh from vendor-ingestion.md"},
    })
    links = result.get("_links", {})
    print(f"done:    page {result['id']} version {result['version']['number']}")
    print(f"url:     {links.get('base', SITE + '/wiki')}{links.get('webui', '')}")


if __name__ == "__main__":
    main()

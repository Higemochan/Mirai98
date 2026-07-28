#!/usr/bin/env python3

"""Syntax-check every browser script inside pc98web.py.

The pages are Python strings, so a stray quote in the JavaScript only
shows up as a blank page in a browser.  This pulls each script out and
hands it to node, which says exactly which line is wrong.

  python3 check_page.py [path/to/pc98web.py]
"""

import os
import re
import subprocess
import sys
import tempfile

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pc98web.py")

with open(path, encoding="utf-8") as f:
    src = f.read()

blocks = []
for match in re.finditer(r"<script([^>]*)>(.*?)</script>", src, re.S):
    attrs, body = match.group(1), match.group(2)
    if "src=" in attrs or not body.strip():
        continue                       # noVNC is loaded, not written here
    blocks.append(("module" if "module" in attrs else "script", body))

if not blocks:
    print("no script found in %s" % path)
    sys.exit(2)

failed = 0
for kind, js in blocks:
    suffix = ".mjs" if kind == "module" else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(js)
        name = tmp.name
    try:
        out = subprocess.run(["node", "--check", name], capture_output=True,
                             text=True)
    finally:
        os.remove(name)
    if out.returncode:
        print(out.stderr.replace(name, "pc98web.py %s" % kind).strip())
        failed += 1
    else:
        print("%s OK (%d lines)" % (kind, js.count("\n")))

sys.exit(1 if failed else 0)

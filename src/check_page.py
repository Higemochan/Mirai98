#!/usr/bin/env python3

"""Syntax-check the browser side of Mirai98.

Every .js in web/ goes to node as a module, and every inline <script> in
the pages there goes the same way, because a page with a stray quote in
it is a blank screen with nothing in the log.  Anything a page says it
loads has to be there as well.

  python3 check_page.py [path/to/web]
"""

import os
import re
import subprocess
import sys
import tempfile

SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S)
LOADS_RE = re.compile(r'(?:src|href)="/([A-Za-z0-9_.-]+)"')


def node_check(js, kind, where):
    """True if node parses this, printing what it says if it does not."""
    suffix = ".mjs" if kind == "module" else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(js)
        name = tmp.name
    try:
        out = subprocess.run(["node", "--check", name],
                             capture_output=True, text=True)
    finally:
        os.remove(name)
    if out.returncode:
        print(out.stderr.replace(name, where).strip())
        return False
    print("%-24s %s OK (%d lines)" % (where, kind, js.count("\n")))
    return True


def main(argv):
    web = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "web")
    if not os.path.isdir(web):
        print("no web/ at %s" % web)
        return 2

    names = sorted(os.listdir(web))
    failed = 0
    checked = 0

    for name in [n for n in names if n.endswith(".js")]:
        with open(os.path.join(web, name), encoding="utf-8") as f:
            js = f.read()
        checked += 1
        failed += not node_check(js, "module", name)

    for name in [n for n in names if n.endswith(".html")]:
        path = os.path.join(web, name)
        with open(path, encoding="utf-8") as f:
            html = f.read()
        for attrs, body in SCRIPT_RE.findall(html):
            if "src=" in attrs or not body.strip():
                continue               # loaded, not written here
            checked += 1
            failed += not node_check(
                body, "module" if "module" in attrs else "script", name)
        # a page that loads something missing is a blank screen too
        for wanted in LOADS_RE.findall(html):
            if not os.path.exists(os.path.join(web, wanted)):
                print("%-24s loads /%s, which is not in web/"
                      % (name, wanted))
                failed += 1

    if not checked:
        print("no script found in %s" % web)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

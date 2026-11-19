#!/usr/bin/env python3
"""SiteAudit — inspect one web response from a terminal.

python site_audit.py                 Interactive console
python site_audit.py --demo          Console with a built-in offline example
python site_audit.py URL             One-page JSON report (requires curl)
python site_audit.py URL --console   Console with the URL already selected
python site_audit.py --demo --json   Machine-readable example report
"""

import argparse
import cmd
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from email.message import Message
from email.parser import BytesHeaderParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit


MODULES = {
    "audit/page": "Inspect form parameters, link query keys and response headers.",
    "audit/forms": "Map page parameters and inspect password-field submission settings.",
    "audit/headers": "Review the headers returned by the requested page.",
}
DEMO_URL = "https://demo.invalid/account?lang=en"
DEMO_HTML = """<!doctype html>
<html lang="en">
<head><title>Account settings</title></head>
<body>
  <form action="/session" method="get">
    <label>Account <input name="account"></label>
    <label>Access code <input type="password" name="access_code"></label>
    <button>Sign in</button>
  </form>
  <form action="/profile" method="post">
    <label>Display name <input name="display_name"></label>
    <button>Save</button>
  </form>
  <a href="/help?topic=accounts">Account help</a>
</body>
</html>"""


def terminal_text(value):
    """Keep response-supplied text from becoming terminal control sequences."""
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in str(value))


def validate_url(url):
    if not url or "\\" in url or any(not c.isprintable() or c.isspace() for c in url):
        raise ValueError("Use a full HTTP(S) URL. Percent-encode spaces in the URL.")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Use an http:// or https:// URL with a hostname.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Remove embedded credentials from the URL.")
    _ = parts.port  # Validate the port while errors can still be attributed to the URL.
    return urlunsplit(parts._replace(fragment=""))


def resolve_url(base, reference):
    try:
        return validate_url(urljoin(base, reference))
    except ValueError:
        return None


def endpoint(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def query_keys(url):
    # Decode keys only. Reports never need query values.
    query = urlsplit(url).query
    return sorted({unquote_plus(pair.partition("=")[0]) for pair in query.split("&")
                   if pair.partition("=")[0]})


@dataclass
class Response:
    status: int
    headers: Message
    body: bytes


def fetch(url):
    executable = "curl.exe" if os.name == "nt" else "curl"
    curl = shutil.which(executable)
    if not curl:
        raise ValueError(f"Live mode requires {executable} on PATH. The demo runs offline.")
    with tempfile.TemporaryDirectory() as temp:
        body = Path(temp) / "body"
        headers = Path(temp) / "headers"
        result = subprocess.run(
            [curl, "--disable", "--silent", "--globoff", "--proto", "=http,https",
             "--header", "Accept-Encoding: identity",
             "--connect-timeout", "6", "--max-time", "20", "--max-filesize", "5242880",
             "--dump-header", str(headers), "--output", str(body),
             "--write-out", "%{http_code}", "--", url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        errors = {3: "invalid URL", 6: "DNS lookup failed", 7: "connection failed",
                  28: "request timed out", 35: "TLS handshake failed",
                  60: "TLS certificate verification failed", 63: "response exceeds 5 MiB"}
        if result.returncode:
            reason = errors.get(result.returncode, "transfer failed")
            raise ValueError(f"curl: {reason} (exit {result.returncode}).")
        blocks = headers.read_bytes().replace(b"\r\n", b"\n").split(b"\n\n")
        # A proxy handshake or a 1xx response may precede the origin response.
        last = next((b for b in reversed(blocks) if b.startswith(b"HTTP/")), None)
        if last is None:
            raise ValueError("The server did not return an HTTP status line.")
        message = BytesHeaderParser().parsebytes(last.partition(b"\n")[2])
        return Response(int(result.stdout), message, body.read_bytes())


def demo_response():
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Strict-Transport-Security"] = "max-age=31536000"
    headers["Referrer-Policy"] = "same-origin"
    headers["X-Content-Type-Options"] = "nosniff"
    return Response(200, headers, DEMO_HTML.encode())


@dataclass
class Control:
    name: str
    kind: str
    line: int
    disabled: bool
    submitter: bool
    method: str | None
    action: str | None


@dataclass
class Form:
    number: int
    line: int
    action: str
    method: str
    controls: list = field(default_factory=list)


@dataclass
class Element:
    tag: str = ""
    owner: Form | None = None
    disabled: bool = False
    inert: bool = False
    seen_legend: bool = False


class Page(HTMLParser):
    void = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__()
        self.stack = [Element()]
        self.base = None
        self.links = []
        self.forms = []
        self.form_ids = {}
        self.external_controls = []

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        parent = self.stack[-1]
        node = Element(tag, parent.owner, parent.disabled, parent.inert)
        line = self.getpos()[0]
        if not node.inert:
            if tag == "legend" and parent.tag == "fieldset" and not parent.seen_legend:
                parent.seen_legend = True
                node.disabled = self.stack[-2].disabled
            elif tag == "fieldset" and "disabled" in a:
                node.disabled = True
            elif tag == "base" and self.base is None and "href" in a:
                self.base = a["href"]
            elif tag == "a" and a.get("href"):
                self.links.append((a["href"], line))
            elif tag == "form":
                form = Form(len(self.forms) + 1, line, a.get("action", ""), a.get("method", ""))
                node.owner = form
                self.forms.append(form)
                if a.get("id"):
                    self.form_ids.setdefault(a["id"], form)
            elif tag in {"input", "button", "select", "textarea"}:
                kind = a.get("type", "").lower() if tag in {"input", "button"} else tag
                submitter = ((tag == "button" and kind not in {"reset", "button"})
                             or (tag == "input" and kind in {"submit", "image"}))
                control = Control(a.get("name", ""), kind, line, node.disabled or "disabled" in a,
                                  submitter, a.get("formmethod"), a.get("formaction"))
                if "form" in a:
                    self.external_controls.append((a["form"], control))
                elif node.owner:
                    node.owner.controls.append(control)
        if tag in {"template", "noscript", "style", "script", "title", "textarea", "datalist"}:
            node.inert = True
        if tag not in self.void:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def close(self):
        super().close()
        for form_id, control in self.external_controls:
            if form_id in self.form_ids:
                self.form_ids[form_id].controls.append(control)


def form_method(value):
    method = value.upper()
    return method if method in {"POST", "DIALOG"} else "GET"


def form_action(page_url, base, action):
    return resolve_url(base, action) if action else page_url


def routes(form, url, base):
    """Account for submit-button overrides before attributing a field to a route."""
    submitters = [c for c in form.controls if c.submitter]
    candidates = [c for c in submitters if not c.disabled] if submitters else [None]
    result = {}
    for button in candidates:
        method = button.method if button and button.method is not None else form.method
        action = button.action if button and button.action is not None else form.action
        target = form_action(url, base, action)
        verb = form_method(method)
        if target and verb != "DIALOG":
            override = button and (button.method is not None or button.action is not None)
            controls = result.setdefault((verb, target), [])
            if button:
                controls.append((button, bool(override)))
    return result


def finding(report, rule, level, title, target, method, evidence, fix, row=None,
            cwe=None, impact=None):
    ident = f"F{len(report['findings']) + 1:02d}"
    item = {"id": ident, "rule": rule, "severity": level, "title": title,
            "parameter": row["parameter"] if row else None, "endpoint": endpoint(target),
            "method": method, "source_line": row["line"] if row else None,
            "basis": "HTML declaration" if row else "Response header",
            "evidence": evidence, "fix": fix, "cwe": cwe, "impact": impact}
    report["findings"].append(item)
    if row:
        row["findings"].append(ident)


def parameter(report, name, source, method, url, line=None):
    if len(report["parameters"]) >= 10000:
        raise ValueError("Page exceeds the 10,000-entry parameter inventory limit.")
    row = {"parameter": name, "source": source, "method": method,
           "endpoint": endpoint(url), "line": line, "findings": []}
    report["parameters"].append(row)
    return row


def inspect_forms(report, page, url):
    base = resolve_url(url, page.base or "") or url
    for form in page.forms:
        target = form_action(url, base, form.action)
        report["forms"].append({
            "number": form.number, "line": form.line, "method": form_method(form.method),
            "action": endpoint(target) if target else None,
            "action_host": urlsplit(target).hostname if target else None,
            "action_query_parameters": query_keys(target) if target else [],
            "field_names": sorted({c.name for c in form.controls if c.name}),
        })
        source = f"form {form.number}"
        fields = [c for c in form.controls if c.name and not c.disabled and not c.submitter
                  and c.kind not in {"button", "reset"}]
        for (method, action), buttons in routes(form, url, base).items():
            override_line = next((c.line for c, override in buttons if override), None)
            for name in query_keys(action):
                parameter(report, name, source + " action query", method, action, override_line or form.line)
            for button, _ in buttons:
                if button.kind == "image":
                    prefix = button.name + "." if button.name else ""
                    for axis in ("x", "y"):
                        parameter(report, prefix + axis, source + " submit", method, action, button.line)
                elif button.name:
                    parameter(report, button.name, source + " submit", method, action, button.line)
            for control in fields:
                row = parameter(report, control.name, source, method, action, control.line)
                if control.kind != "password":
                    continue
                evidence = (f"Form {form.number} (HTML line {form.line}) has a named password "
                            f"input on line {control.line}.")
                if override_line:
                    evidence += f" A submit control overrides the form settings on line {override_line}."
                if method == "GET":
                    finding(report, "password-get", "HIGH", "Password field configured for GET",
                            action, method, evidence + " This GET route places the field in the URL query.",
                            "Set the form and its submit buttons to POST to an HTTPS endpoint.", row,
                            "CWE-598", "The submitted value can appear in browser history and request logs.")
                if urlsplit(action).scheme == "http":
                    finding(report, "password-http", "HIGH", "Password form targets an HTTP URL",
                            action, method, evidence + " The resolved form action uses http://.",
                            "Set the form action to HTTPS and serve the form over HTTPS.", row,
                            "CWE-319", "If a browser submits to this HTTP endpoint, the value travels in cleartext.")

    seen = set()
    for href, line in page.links:
        target = resolve_url(base, href)
        if not target or urlsplit(target).hostname != urlsplit(url).hostname:
            continue
        for name in query_keys(target):
            key = (endpoint(target), name)
            if key not in seen:
                parameter(report, name, "link query", "GET", target, line)
                seen.add(key)
    report["same_host_link_query_parameters"] = sorted({name for _, name in seen})


def inspect_headers(report, url, headers):
    checks = {"X-Content-Type-Options": "Review adding X-Content-Type-Options: nosniff."}
    if urlsplit(url).scheme == "https":
        checks["Strict-Transport-Security"] = "Review the HTTPS deployment and its HSTS policy."
    if report["content_type"] == "text/html":
        checks["Content-Security-Policy"] = "Check any HTML meta policy and consider an enforcing CSP response header."
        checks["Referrer-Policy"] = "Review whether the browser default meets this page's referrer requirements."
    missing = [name for name in checks if name not in headers]
    report["missing_response_headers"] = missing
    for name in missing:
        finding(report, "header-" + name.lower(), "REVIEW", f"Missing {name} header", url, "GET",
                f"The HTTP {report['status']} response has no {name} header.", checks[name])


def audit(url, module="audit/page", demo=False):
    url = validate_url(url)
    response = demo_response() if demo else fetch(url)
    mime = response.headers.get_content_type()
    report = {"source": "offline demo" if demo else "live HTTP response", "module": module,
              "host": urlsplit(url).hostname, "page": endpoint(url), "status": response.status,
              "content_type": mime, "url_query_parameters": query_keys(url),
              "forms": [], "parameters": [], "findings": [], "html_inspected": False}
    for key in report["url_query_parameters"]:
        parameter(report, key, "request query", "GET", url)
    if 300 <= response.status < 400:
        report["note"] = "3xx response. Only the supplied URL was fetched."
        location = response.headers.get("Location")
        redirected = resolve_url(url, location) if location else None
        if redirected:
            report["redirect_to"] = endpoint(redirected)
    elif module != "audit/headers":
        if mime != "text/html":
            report["note"] = f"Form inspection requires text/html. The response declares {mime}."
        elif response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            report["note"] = "The server returned encoded content. Form inspection was skipped."
        else:
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                html = response.body.decode(charset, "replace")
            except (LookupError, UnicodeError, ValueError):
                html = response.body.decode("utf-8", "replace")
                report["note"] = "Unrecognized charset. Form inspection used UTF-8."
            page = Page()
            page.feed(html)
            page.close()
            inspect_forms(report, page, url)
            report["html_inspected"] = True
    if module != "audit/forms":
        inspect_headers(report, url, response.headers)
    return report


def columns():
    return max(36, min(shutil.get_terminal_size((96, 24)).columns, 140))


def table(headings, rows):
    if not rows:
        return
    data = [headings] + [[terminal_text(v) for v in row] for row in rows]
    available = columns()
    widths = [min(available, max(len(row[i]) for row in data)) for i in range(len(headings))]
    while sum(widths) + 2 * (len(widths) - 1) > available:
        widest = widths.index(max(widths))
        widths[widest] -= 1
    for index, row in enumerate(data):
        cells = [textwrap.wrap(text, width, break_on_hyphens=False) or [""]
                 for text, width in zip(row, widths)]
        for line in range(max(map(len, cells))):
            print("  ".join((cell[line] if line < len(cell) else "").ljust(width)
                            for cell, width in zip(cells, widths)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))


def detail(label, value):
    prefix = f"{label:<11} "
    print(textwrap.fill(terminal_text(value), width=columns(),
                        initial_indent=prefix, subsequent_indent=" " * len(prefix)))


def finding_table(items):
    if not items:
        print("No findings from the selected checks on this response.")
        return
    table(["ID", "Level", "Parameter", "Finding"],
          [[f["id"], f["severity"], f["parameter"] or "—", f["title"]] for f in items])


def unquote_arg(text):
    text = text.strip()
    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        return text[1:-1]
    return text


class Console(cmd.Cmd):
    def __init__(self, url=None, demo=False, module="audit/page"):
        super().__init__()
        self.url = DEMO_URL if demo else url
        self.demo = demo
        self.module = module
        self.report = None
        supports_color = os.name != "nt" or bool(os.getenv("WT_SESSION"))
        self.color = (supports_color and sys.stdout.isatty()
                      and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb")
        self.intro = (self.paint("SiteAudit", "1;36") + "   Web review console\n"
                      + ("Offline demo selected. Type run to inspect the example."
                         if demo else "Type set URL <url> and run, or type demo for an offline example.")
                      + "\nType help for commands.")
        self.update_prompt()

    def paint(self, text, color):
        return f"\033[{color}m{text}\033[0m" if self.color else text

    def update_prompt(self):
        self.prompt = f"siteaudit ({self.module}) > " if self.module else "siteaudit > "

    def emptyline(self):
        pass  # Pressing Enter twice must not issue a second request.

    def default(self, line):
        print("Unknown command. Type help for commands.")

    def do_help(self, arg):
        """help [command] — list commands or read help for one."""
        if arg:
            return super().do_help(arg)
        print("use <module>       Select a module from show modules")
        print("set URL <url>      Select a live page")
        print("run                Inspect the selected page")
        print("show <view>        modules, options, page, forms, parameters, findings")
        print("info <ID>          Show evidence, source location and remediation")
        print("export <file>      Save this report as JSON")
        print("demo               Run the built-in offline example")
        print("back / exit        Leave the module / close the console")

    def do_use(self, arg):
        """use audit/page | audit/forms | audit/headers"""
        name = arg.strip().lower()
        if name not in MODULES:
            print("Choose a module from show modules.")
            return
        self.module, self.report = name, None
        self.update_prompt()
        print(MODULES[name])

    def complete_use(self, text, *_):
        return [name for name in MODULES if name.startswith(text)]

    def do_back(self, arg):
        """back — leave the current module."""
        self.module, self.report = None, None
        self.update_prompt()

    def do_set(self, arg):
        """set URL https://host/path — select the page that run will fetch."""
        parts = arg.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "url":
            print("Usage: set URL <http(s)://host/path>")
            return
        try:
            target = validate_url(unquote_arg(parts[1]))
        except ValueError as error:
            print(terminal_text(error))
            return
        self.url, self.demo, self.report = target, False, None
        detail("URL", endpoint(target))

    def do_demo(self, arg):
        """demo — select and inspect an offline example using audit/page."""
        self.url, self.demo, self.module = DEMO_URL, True, "audit/page"
        self.update_prompt()
        self.do_run("")

    def do_run(self, arg):
        """run — perform one inspection. Use show and info to explore the saved result."""
        if arg.strip():
            print("Usage: run")
            return
        if not self.module or not self.url:
            print("Select a module with use and a page with set URL, or type demo.")
            return
        self.report = None
        message = ("Reading the offline example" if self.demo
                   else "Fetching " + terminal_text(endpoint(self.url)))
        print(self.paint("[*]", "36"), message, flush=True)
        try:
            self.report = audit(self.url, self.module, self.demo)
        except (ValueError, OSError) as error:
            print(self.paint("[-]", "31"), terminal_text(error))
            return
        except KeyboardInterrupt:
            print("\nRequest cancelled.")
            return
        r = self.report
        print(self.paint("[+]", "32"), f"HTTP {r['status']}  {terminal_text(r['content_type'])}")
        if r["html_inspected"]:
            print(f"    {len(r['forms'])} forms   {len(r['parameters'])} parameter entries")
        if "note" in r:
            print(terminal_text(r["note"]))
        items = r["findings"]
        print()
        finding_table(items[:10])
        if items:
            prefix = f"Showing 10 of {len(items)}. " if len(items) > 10 else ""
            print(f"\n{prefix}Use info {items[0]['id']} for evidence, or show parameters for the field map.")

    def do_show(self, arg):
        """show modules | options | page | forms | parameters | findings"""
        view = arg.strip().lower()
        if view == "modules":
            table(["Module", "Checks"], list(MODULES.items()))
            return
        if view == "options":
            detail("Module", self.module or "(choose with use)")
            detail("URL", endpoint(self.url) if self.url else "(set URL <url>)")
            detail("Source", "offline demo" if self.demo else "live HTTP")
            if self.url:
                detail("Query keys", ", ".join(query_keys(self.url)) or "—")
            return
        if view not in {"page", "forms", "params", "parameters", "findings"}:
            print(self.do_show.__doc__)
            return
        if not self.report:
            print("Run an inspection first.")
            return
        r = self.report
        if view == "findings":
            finding_table(r["findings"])
        elif view in {"params", "parameters"}:
            if r["parameters"]:
                table(["Parameter", "Source", "Method", "Endpoint", "Findings"],
                      [[p["parameter"], p["source"], p["method"], p["endpoint"],
                        ", ".join(p["findings"]) or "—"] for p in r["parameters"]])
            else:
                print("No parameter entries in this inspection.")
        elif view == "forms":
            if not r["html_inspected"]:
                print("This inspection has no HTML form inventory.")
            elif not r["forms"]:
                print("No forms in the inspected HTML.")
            else:
                table(["Form", "Line", "Default", "Declared action", "Named controls"],
                      [[f["number"], f["line"], f["method"], f["action"] or "(non-HTTP or invalid)",
                        ", ".join(f["field_names"]) or "—"] for f in r["forms"]])
        else:
            detail("Page", r["page"])
            detail("Source", r["source"])
            detail("Module", r["module"])
            detail("Response", f"HTTP {r['status']}   {r['content_type']}")
            detail("HTML", "Server-returned markup inspected" if r["html_inspected"] else "Uninspected")
            if "redirect_to" in r:
                detail("Redirect", r["redirect_to"])
            if "note" in r:
                detail("Note", r["note"])
            detail("Findings", len(r["findings"]))

    def complete_show(self, text, *_):
        return [v for v in "modules options page forms parameters findings".split() if v.startswith(text)]

    def do_info(self, arg):
        """info F01 — explain an individual finding. info alone describes the module."""
        if not arg:
            print(MODULES.get(self.module, "Select a module with use."))
            return
        if not self.report:
            print("Run an inspection first.")
            return
        item = next((f for f in self.report["findings"] if f["id"] == arg.strip().upper()), None)
        if not item:
            print("Choose an ID from show findings.")
            return
        print(self.paint(f"{item['id']}  {item['severity']}", "1;33"), terminal_text(item["title"]))
        detail("Parameter", item["parameter"] or "Page-level")
        detail("Endpoint", f"{item['method']} {item['endpoint']}")
        detail("Source", self.report["page"])
        if item["source_line"] is not None:
            detail("HTML line", item["source_line"])
        detail("Basis", item["basis"])
        if item["cwe"]:
            detail("Weakness", item["cwe"])
        detail("Evidence", item["evidence"])
        if item["impact"]:
            detail("Impact", item["impact"])
        detail("Fix", item["fix"])

    def do_export(self, arg):
        """export <path> — write the current report to a new JSON file."""
        if not self.report or not arg.strip():
            print("Run an inspection, then use export <file.json>.")
            return
        path = Path(unquote_arg(arg)).expanduser()
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(self.report, output, indent=2)
                output.write("\n")
        except FileExistsError:
            print("File exists. Choose a new report filename.")
            return
        except OSError as error:
            print(terminal_text(error))
            return
        print("Saved", terminal_text(path))

    def do_exit(self, arg):
        """exit — close the console."""
        return True

    do_quit = do_exit

    def do_EOF(self, arg):
        print()
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="HTTP(S) page to inspect")
    parser.add_argument("--demo", action="store_true", help="use the built-in offline response")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print one inspection as JSON")
    mode.add_argument("--console", action="store_true", help="start the console with the selected page")
    parser.add_argument("--module", choices=MODULES, default="audit/page")
    args = parser.parse_args()
    if args.demo and args.url:
        parser.error("Choose a live URL or --demo.")
    if args.json and not (args.demo or args.url):
        parser.error("--json requires a URL or --demo.")
    try:
        url = validate_url(args.url) if args.url else None
        if args.json or (url and not args.console):
            report = audit(url or DEMO_URL, args.module, args.demo)
            print(json.dumps(report, indent=2))
        else:
            Console(url, args.demo, args.module).cmdloop()
    except (OSError, ValueError) as error:
        parser.exit(1, f"Audit failed: {terminal_text(error)}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nClosed.\n")


if __name__ == "__main__":
    main()

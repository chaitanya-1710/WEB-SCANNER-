# SiteAudit

SiteAudit is a terminal tool for reviewing one web page. It maps parameter names to forms and endpoints, checks how password fields would be submitted, and reviews HTTP response headers. Each field-level finding includes the parameter, source line, submission method, evidence, potential impact and suggested fix.

## Open on Windows

1. Install Python 3.10 or newer if needed. For the classic installer, select **Add python.exe to PATH**. [Python for Windows](https://www.python.org/downloads/windows/)
2. Right-click the project ZIP, choose **Extract All**, then open the folder containing this README.
3. Double-click **Start-SiteAudit.cmd**. At the `siteaudit (audit/page) >` prompt, enter the following commands one at a time.

```text
run
info F01
show parameters
```

The launcher starts an offline example. Its first finding, `F01`, identifies the password field `access_code` at HTML line 7. The form uses `GET /session`, so submitting it can put the value into browser history and request logs (CWE-598).

To use PowerShell instead, open it in the extracted folder and run:

```powershell
py -3 .\site_audit.py --demo
```

If Windows recognizes `python` instead of `py`, use `python .\site_audit.py --demo`. The project uses the Python standard library. Live requests also require curl. On Windows, `curl.exe --version` checks that the executable is available. On Linux or macOS, start with `python3 site_audit.py --demo`.

## Review a page

At the SiteAudit prompt, use the URL of a page you administer. Replace the example URL with the full page address.

```text
use audit/page
set URL https://your-site.example/login
run
show findings
show parameters
```

Use `info F01` with an ID from the current report. Use `export report.json` to save it to a new file in the working directory. With the Windows launcher, this is the extracted project folder. Type `exit` to close the console.

| Command | Result |
| --- | --- |
| `show modules` | List the page, form and header checks. |
| `use audit/forms` | Focus the next run on form settings and parameter names. |
| `use audit/headers` | Focus the next run on HTTP response headers. |
| `show options` | Display the selected URL, data source and module. |
| `show page` | Display HTTP status, content type and inspection coverage. |
| `show forms` | List each form's declared action, default method and named fields. |
| `demo` | Load and inspect the bundled example from any module. |

### Reading a report

A live run makes one GET request to the supplied URL. Form inspection uses server-returned HTML, including declared submit-button overrides. JavaScript-generated fields, backend input handling and the destination of a redirect are outside this inspection.

* **HIGH** identifies an observed password-field configuration. A GET form places the value in the query (CWE-598). An HTTP action can transmit it in cleartext (CWE-319). The evidence describes the declared route. It does not establish that anyone submitted a password.
* **REVIEW** identifies a missing response header. These findings apply to the page. The parameter inventory also includes ordinary fields with no finding.

JSON reports retain endpoint paths, parameter names and HTML line numbers. Query values, form values, cookie contents and raw response bodies are omitted. Check endpoint paths for private identifiers before sharing a report.

## Command-line use and tests

From the project folder in PowerShell:

```powershell
py -3 .\site_audit.py --demo --json
py -3 .\site_audit.py "https://your-site.example/login" --module audit/forms
py -3 -m unittest discover -s tests -v
```

The tests use the offline demo and a loopback HTTP server with known field-level findings. The GitHub Actions workflow runs the suite on Windows and Ubuntu. To install the optional `siteaudit` terminal command in an active Python virtual environment, run `python -m pip install .`.

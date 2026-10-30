"""The issuer's own pages (agent-auth): sign-in, device approval and what went wrong, in the console's look.

The issuer is a separate origin with a strict Content-Security-Policy, so the stylesheet is
served by it (/sign-in.css, allowed by style-src 'self') rather than inline, and the logo is
inline SVG. Everything put into a page is escaped here.
"""

from __future__ import annotations

import html

from fastapi.responses import HTMLResponse

# The console's palette (ui/src/styles.css): light and dark follow the browser.
STYLESHEET = """
:root { color-scheme: light dark; --paper: #ffffff; --ground: #f6f7f9; --ink: #16181d; --steel: #5d6470; --line: #e5e7eb;
  --line-strong: #d2d6dc; --accent: #4f46e5; --accent-ink: #4338ca; --accent-soft: #eef0fe; --bad: #b83a32; --bad-soft: #fcebea;
  --good: #1f7a4d; --live: #8a5d00; --live-soft: #fbf1da; --on-accent: #ffffff;
  --shadow-lg: 0 16px 40px -12px rgb(16 24 40 / 0.22), 0 2px 6px rgb(16 24 40 / 0.06);
  --sans: "Inter Variable", "Inter", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "Cascadia Mono", Consolas, monospace; }
@media (prefers-color-scheme: dark) {
  :root { --paper: #16181d; --ground: #0e0f13; --ink: #ebecf0; --steel: #9aa1ad; --line: #262931; --line-strong: #353943;
    --accent: #818cf8; --accent-ink: #a5b4fc; --accent-soft: #1f2242; --bad: #f08a80; --bad-soft: #3a1c1a; --good: #5cc08a;
    --live: #e5b650; --live-soft: #3a2e12; --on-accent: #0e0f1a; --shadow-lg: 0 16px 40px -12px rgb(0 0 0 / 0.7); }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--ground); color: var(--ink);
  font: 15px/1.55 var(--sans); -webkit-font-smoothing: antialiased; }
main { width: 100%; max-width: 420px; display: grid; gap: 16px; padding: 32px; background: var(--paper); border: 1px solid var(--line);
  border-radius: 16px; box-shadow: var(--shadow-lg); }
.wordmark { display: flex; align-items: center; gap: 10px; font-weight: 650; font-size: 16px; letter-spacing: -0.01em; }
.wordmark svg { width: 28px; height: 28px; flex: none; }
.wordmark rect { fill: var(--accent); }
.wordmark path { stroke: #fff; stroke-width: 3; fill: none; stroke-linecap: round; stroke-linejoin: round; }
h1 { margin: 4px 0 0; font-size: 24px; line-height: 1.25; letter-spacing: -0.02em; }
p { margin: 0; }
.subtle { color: var(--steel); font-size: 14px; }
form { display: grid; gap: 14px; }
label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
input { width: 100%; padding: 10px 12px; font: inherit; font-weight: 400; color: var(--ink); background: var(--paper);
  border: 1px solid var(--line-strong); border-radius: 8px; }
input:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
input.code { font-family: var(--mono); font-size: 18px; letter-spacing: 0.12em; text-transform: uppercase; }
.buttons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px; }
button, .button { display: inline-flex; justify-content: center; align-items: center; min-height: 40px; padding: 8px 16px;
  font: inherit; font-weight: 600; border-radius: 8px; border: 1px solid var(--line-strong); background: var(--paper); color: var(--ink);
  cursor: pointer; text-decoration: none; }
button.primary, .button.primary { flex: 1; background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
button.primary:hover, .button.primary:hover { background: var(--accent-ink); border-color: var(--accent-ink); }
button:focus-visible, .button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.notice { padding: 10px 12px; border-radius: 8px; font-size: 14px; }
.notice.bad { background: var(--bad-soft); color: var(--bad); }
.notice.warn { background: var(--live-soft); color: var(--live); }
.notice.good { background: var(--accent-soft); color: var(--accent-ink); }
a { color: var(--accent); }
footer { color: var(--steel); font-size: 12px; border-top: 1px solid var(--line); padding-top: 14px; }
"""

LOGO = ('<div class="wordmark"><svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" />'
        '<path d="M8 11h6l4 10h6" /></svg><span>AI Control Mesh</span></div>')


def document(title: str, body: str, status: int = 200) -> HTMLResponse:
    """A complete page: title is plain text, body is HTML already escaped by the caller."""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{html.escape(title)} · AI Control Mesh</title>
<link rel="stylesheet" href="/sign-in.css"></head>
<body><main>{LOGO}
{body}
<footer>Your password goes only to this sign-in service, never to the console, the API or an agent.</footer>
</main></body></html>""",
        status_code=status,
    )


def notice(text: str | None, tone: str = "bad") -> str:
    return f'<p class="notice {tone}" role="alert">{html.escape(text)}</p>' if text else ""


def sign_in(transaction: str, *, username: str = "", error: str | None = None, status: int = 200) -> HTMLResponse:
    """The console's sign-in form (the authorization code flow)."""
    body = f"""<h1>Sign in</h1>
<p class="subtle">Sign in to continue to the AI Control Mesh console.</p>
{notice(error)}
<form method="post" action="/authorize"><input type="hidden" name="transaction" value="{html.escape(transaction)}">
<label>Username <input name="username" required autocomplete="username" autocapitalize="none" spellcheck="false" value="{html.escape(username)}"{'' if username else ' autofocus'}></label>
<label>Password <input type="password" name="password" required autocomplete="current-password"{' autofocus' if username else ''}></label>
<div class="buttons"><button class="primary" name="approve" value="yes">Sign in</button></div>
</form>"""
    return document("Sign in", body, status)


def device(transaction: str, *, user_code: str = "", username: str = "", error: str | None = None, status: int = 200) -> HTMLResponse:
    """Approving a sign-in started on another device: agent login or agent-machine login."""
    body = f"""<h1>Approve a device</h1>
<p class="subtle">Enter the code that <code>agent login</code> or <code>agent-machine login</code> shows, then sign in to let that computer use your account.</p>
{notice("Only approve a code from a sign-in you started yourself. Never approve a code someone else sent you.", "warn")}
{notice(error)}
<form method="post" action="/device"><input type="hidden" name="transaction" value="{html.escape(transaction)}">
<label>Code <input class="code" name="user_code" required autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" placeholder="ABCD-EFGH" value="{html.escape(user_code)}"{'' if user_code else ' autofocus'}></label>
<label>Username <input name="username" required autocomplete="username" autocapitalize="none" spellcheck="false" value="{html.escape(username)}"></label>
<label>Password <input type="password" name="password" required autocomplete="current-password"{' autofocus' if user_code else ''}></label>
<div class="buttons"><button class="primary" name="approve" value="yes">Approve</button><button name="approve" value="no">Deny</button></div>
</form>"""
    return document("Approve a device", body, status)


def device_done(approved: bool) -> HTMLResponse:
    if approved:
        body = ('<h1>Device approved</h1>' + notice("That computer is now signed in.", "good")
                + '<p class="subtle">Go back to its terminal: it carries on by itself. You can close this page.</p>')
        return document("Device approved", body)
    body = ('<h1>Device denied</h1>' + notice("That sign-in was refused. The computer that asked can't use the code again.", "warn")
            + '<p class="subtle">If you didn\'t start it, someone may have your code: nothing was signed in. You can close this page.</p>')
    return document("Device denied", body)


# What each error means for the person looking at the page, and where to go next.
PROBLEMS = {
    "invalid_transaction": ("This page has expired", "Sign-in pages last 10 minutes and work once. Start again to get a fresh one."),
    "invalid_origin": ("This page has expired", "The form has to be sent from this sign-in page. Start again to get a fresh one."),
    "invalid_request": ("This sign-in link isn't valid", "It may be incomplete or out of date. Start signing in again from the console."),
    "rate_limit_exceeded": ("Too many attempts", "Wait a few minutes, then try again."),
    "access_denied": ("This account can't sign in", "Ask an administrator to check your account."),
    "invalid_user_code": ("That code can't be used", "It has expired or was already used. Run the login command again for a new code."),
}


def problem(error: str, status: int, *, again: str | None, again_label: str) -> HTMLResponse:
    """A page for an error that can't be fixed on the form itself, with a way to start over."""
    title, detail = PROBLEMS.get(error, ("Sign-in didn't work", "Start signing in again."))
    link = f'<div class="buttons"><a class="button primary" href="{html.escape(again)}">{html.escape(again_label)}</a></div>' if again else ""
    body = f'<h1>{html.escape(title)}</h1><p class="subtle">{html.escape(detail)}</p>{link}'
    return document(title, body, status)

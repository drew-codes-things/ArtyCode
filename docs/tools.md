# Tools

[Back to the README](../README.md)

| Tool | What it does | Asks first? |
|---|---|---|
| `read_file`, `list_files`, `search` | Read and find code in the project; a Jupyter notebook reads as numbered cells with their text output. A long file is read 250 lines at a time, headed with the lines shown and how many are above and below; the first window also lists the file's functions and classes with their line numbers so the next read can jump there. Searches stop at a size limit and say so | Only outside the project folder |
| `edit_file`, `write_file` | Change or create files, keeping each file's line endings. An existing file has to be read first, and is refused if it changed on disk since; every write is atomic (a temporary file, then a swap). `write_file` only creates new files: for an existing one it answers with how to use `edit_file` instead, unless the call sets `overwrite: true` (the question then says Overwrite). When `old_text` doesn't match exactly, `edit_file` tries again ignoring spaces at line ends, then indentation, then curly quotes and long dashes, uses a match only if it is the only one, keeps the file's indentation, and says which way it matched. A made edit answers with the changed lines and three either side, numbered; a miss answers with the closest lines in the file, numbered (or, when `new_text` is already there, says the change is already made), and counts as reading the file, so the corrected edit applies at once; from the second miss on the same file it also carries the whole file as it is now, numbered, within the result budget (for a bigger file, the function around the closest match), with advice to replace the whole function (or, for a file of up to 200 lines, rewrite it). A change whose new text adds one of ArtyCode's own placeholders (`(N characters dropped to save room)`, a `[Hidden to save room` line or a `characters cut` line) is refused and the file left alone | Yes, showing the diff (`--auto-edit` skips this inside the project, except for anything in a `.arty` or `.git` folder: that is always asked about, because those files can make programs run) |
| `multi_edit` | Several edits to one file in one step, applied in order; all or nothing, so if one edit doesn't match exactly once nothing is changed. One question with the whole diff, and one `/undo` puts it all back | Yes, like an edit |
| `notebook_edit` | Replace, insert or delete one cell of a `.ipynb` notebook (a replaced code cell's old output is cleared). Offered once the project has a notebook or a request mentions one | Yes, like an edit |
| `run_command` | Tests, builds, git (bash on Linux, PowerShell on Windows). A command is stopped after `timeout_seconds` (120 unless the model asks for more, up to 1800, or `max_command_seconds`), and the result starts by saying it was stopped and isn't running any more, then says to run a server in the background or give a long build more time, before the output it printed. The output includes stderr, and long output is trimmed to its start and end. `cwd` runs it in a folder inside the project instead of the project folder. `background: true` starts it and returns at once | Yes, except [look-only commands](permissions.md#look-only-commands) |
| `command_output`, `stop_command` | Read or stop a command started in the background (a dev server, a watcher, a long build). `command_output` waits up to `wait_seconds` (5 unless asked for more) for the job to finish, then returns what it printed since the last read; background commands end when ArtyCode does, also when its terminal is closed or it is sent SIGTERM (not after a SIGKILL) | No |
| `todo_write` | A checklist for jobs with several steps, drawn with boxes under the step each time it changes (☐ to do, ☑ done) and kept for `/todos` | No |
| `web_fetch` | Read a public web page (documentation, an issue), marked as information rather than instructions | Yes, once per site with **a** |
| `web_search` | Search the web through your Arty brain's own search (its `research__web_search` tool, so no other search key is needed); titles, links and snippets, marked as information rather than instructions. Helpers can search too | Yes, once per session with **a** (also in plan mode, since it changes nothing) |
| `view_image` | Looks at a picture in the project (PNG, JPEG, WebP, GIF) with your Arty brain's vision model and answers a question about it (below) | Only outside the project folder |
| `screenshot` | Opens a page (a local dev server, an HTML file in the project, any web address) in a headless browser, saves screenshots and reports the status, console errors and failed requests (below) | Not for this computer or a project file; other addresses yes, once per site with **a** |
| `http_check` | GET or HEAD an address, including localhost and home-network services, with an optional expected status and text (below) | Not for this computer; other addresses yes, once per site with **a** |
| `explore` | A helper with its own fresh conversation that looks through the project and reports back; up to four run at once. A helper keeps its conversation inside the window: once it is too long, older results it has read again or no longer needs are dropped | It can only look |
| `helper` | Hands a job to one of your own [helpers](helpers.md) | Depends on the helper's tools |
| `skill` | Loads one of your [skills](skills.md) when the job matches it | No |
| `mcp__<server>__<tool>` | Tools from your [MCP servers](mcp-servers.md) | The first use of each server asks; after that, tools the server marks read-only don't, the others do |

## How much of a result the model sees

Every tool result the model sees is cut to about a quarter of the room the conversation has before it is summarised (about 16,000 characters on a 24,576-token window); `read_file` says where to read on, so a long file is read in windows. An `@file` attachment is read the same way.

## Pictures, pages and live checks

Three look-only tools let the model see what it built and check that a service answers. None of them changes the
project or sends anything but a plain request, and plan mode and helpers can use them too. `http_check` is always
offered; `view_image` and `screenshot` are offered once the project has web pages or pictures (HTML, CSS, JSX, Vue,
Svelte, PNG, JPEG and the like) or a request mentions a page, a picture, a screenshot, the layout or the like. A tool
offered once stays offered for the rest of the session, and the list is only decided between requests, never in the
middle of one, so the brain can keep reusing what it already read.

- `view_image` reads a picture in the project and asks the brain's vision model about it directly (the
  `X-Arty-Direct-Vision` path, so the answer is about the picture, not a chat). Pictures over 1280 pixels on a side,
  over 5 MB, or GIFs (first frame) are first redrawn as a smaller PNG in the headless browser below, so a local 9B
  vision model gets a sensible size; without a browser, a picture under 5 MB is sent as it is. The answer is marked
  as information, not instructions.
- `screenshot` opens an address (`localhost:8000` works without `http://`) or an HTML file in the project in headless
  Chrome, Edge or Chromium, at 1280 by 800 and at a 390 by 844 phone size unless `width` (and `height`) is given.
  The pictures go to `.arty/screenshots/` (that folder ignores itself in git), and the result gives the HTTP status,
  the title, console errors, uncaught errors, failed or 4xx/5xx requests and the start of the visible text. With
  `question`, each screenshot also goes through `view_image`. The browser runs with a throwaway profile in the temp
  folder, never your own, with no extensions or sound, and the profile is deleted when the shot is done. An HTML file
  is opened with every network request sent nowhere, this computer's addresses included (the result lists them as
  failed requests), so a project page can't send anything out; a page from a local server loads what it asks for,
  as in a normal browser.
  The browser
  is found on the PATH (Chrome, `chromium`, `microsoft-edge` and others), where Windows and macOS install Chrome,
  Edge and Chromium, or at `ARTY_BROWSER`; without one, `screenshot` isn't offered.
- `http_check` sends a GET or HEAD (no request body, no saved headers or keys) and returns the status, the time,
  the main headers and the start of the body. `expect_status` and `expect_text` add a PASS or FAIL line, redirects
  are reported instead of followed, and `verify_tls: false` accepts a home server's self-signed certificate.
  Unlike `web_fetch`, it reaches home-network addresses.

Addresses on this computer (`localhost`, `127.0.0.1`, `::1`) and files in the project need no OK. Home-network and
public addresses ask like `web_fetch` (**a** covers that site for that tool until you quit, **s** saves a rule such
as `http_check(http://your-arty-host:8000/*)`), and a file outside the project asks too. Deny rules are matched
against the full address, so `screenshot(http://localhost:9000/*)` also stops `localhost:9000/admin`.

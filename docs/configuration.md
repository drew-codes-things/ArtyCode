# Configuration

[Back to the README](../README.md)

These environment variables override the file: `ARTY_URL`, `ARTY_REMOTE_URL`, `ARTY_API_KEY`,
`ARTY_ACCESS_CLIENT_ID`, `ARTY_ACCESS_CLIENT_SECRET`, `ARTY_OUTSIDE_URL`, `ARTY_OUTSIDE_KEY` (an OpenAI-compatible
server's address and key, `outside_url` and `outside_key` in the file), `ARTY_CLI_MODEL` and `ARTY_CLI_FINISH_CHECK`
(`on` or `off`, the [finish check](long-jobs.md#long-jobs)). Two optional limits can be set the same way,
as `context_tokens` and `max_command_seconds` in the file or `ARTY_CLI_CONTEXT_TOKENS` and
`ARTY_CLI_MAX_COMMAND_SECONDS`: the model's context window (unless set, the size the brain's model list reports for the model in use, else the smallest it lists, or 12288 tokens for a brain that doesn't say) and the
longest one command may run (1800 seconds unless set). Anything but a whole number above 0 stops ArtyCode at the start
with a message saying where to fix it. `notify` (or `ARTY_CLI_NOTIFY`) set to `off` stops the bell and desktop notice
of the terminal interface; they are on unless set. `update_notice` (or `ARTY_CLI_UPDATE_NOTICE`) set to `off` stops
the [check for a newer release](checking-and-updating.md) at the start of a chat; it is on unless set.

When something goes wrong the message says what to do next. A refused key says to run `arty-cli login` again; a
model the brain doesn't have says so and points at `/model` (or `--model` for one run); neither suggests waiting.
A brain that can't be reached, or a connection that drops, names the address and asks you to try again once it's
back. For an `outside/` model the messages name that server instead: a refused key says to run `arty-cli login
outside`, a model it lacks points at `/model`, and a busy or rate-limited server asks you to try again in a minute.
The context window for an `outside/` model is `context_tokens` when set, otherwise 12288 tokens. `/model` marks a model the
server lists as `"available": false` with "(offline)" (Arty's models too, when Arty's brain says one is offline), and a chat with a model the server answers with 503
`model_unavailable` says the model is offline right now instead of waiting and retrying.

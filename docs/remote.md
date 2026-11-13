# Remote control

[Back to the README](../README.md)

`/remote` (or starting with `arty --remote`) shares the chat through your Arty brain, so the Arty app on Android or
iPhone (**More > Computers**) and starfire.ai's admin **Arty CLI** page can follow it and steer it. The computer opens
no port: it sends what happens to the brain and asks the brain for anything sent back. `/remote off` stops sharing,
and so does leaving ArtyCode.

Sharing sends the whole conversation so far first (a chat you resumed, or one you had been working in for a while),
so the other device opens on everything up to now and then follows live. When the conversation is summarised to
free up room, by `/compact` or automatically, the other device shows a "Conversation compacted" marker that opens
to the summary Arty carries on from, and the chat keeps working. The brain keeps the newest 2000 events of a shared
chat (about 2 MB of text); anything older than that is dropped there, never on the computer.

Several terminals can be shared at once. Each one is listed under its own name: the project folder, with a number
added when another shared terminal already has that name (`greeter`, `greeter 2`). `/remote name <new name>` gives
this terminal a name of your choice.

Turning it on says the name the chat is shared as and where to open it, and reminds you that a phone's device key
needs its **Arty CLI** switch on. While it is on, the bar under the prompt ends with `· remote` (the plain prompt reads `default · remote ›`) and `/status` says
`Remote control: on as "<name>"`. A request sent from afar shows as `From <device> (remote control): ...`; a question
adds `Or answer Allow or Deny on another device.`, and an answer given there shows as `Allowed on <device> (remote
control).` or `Refused on ...`. If the brain turns sharing down, or a shared session ends, the message says why and
what to do next: log in again for a refused key, update a brain that has no remote control, end another shared
session when too many are open, or `/remote` again when the brain is back.

From another device you can:

- follow the chat as it happens: your requests, Arty's answers, its tool calls and their results;
- send a request. It runs once the current one is finished, always as a plain request: a `/command`, `!command` or
  `# note` sent from afar goes to the model as text and is never run here;
- answer a question with Allow or Deny. The question still shows here as well, the first answer wins, and the other
  side is told who answered. Allow from afar means yes once: never always, never a saved rule. For a plan it approves
  the plan and goes back to the default mode, which asks before changes;
- stop the current request, the same as Ctrl-C here.

The mode can't be changed from afar; the other devices see it when you change it here. Only Arty's own key, or a
device key whose **Arty CLI** switch is on (off by default; switched on starfire's Arty devices page or the iPhone
app's device list), can see or steer a shared chat. The brain forgets a shared chat it hasn't heard from for 10
minutes.

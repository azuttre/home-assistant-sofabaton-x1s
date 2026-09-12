# Your first command and your first remote press

Start with **sofabaton-x-server** when building an integration. It manages
the hub connection and exposes HTTP and WebSocket interfaces that you can
use from any language. The examples here are small Python clients of that
server; they do not connect directly to a hub.

This guide gets two things working:

1. **Send a command:** find a device and its command IDs, then send one.
2. **Receive a press:** assign a callback command to a remote button and
   receive a `press` message when someone pushes it.

**Receiving presses requires setup.** The hub does not report ordinary IR
or Bluetooth presses. We create a *callback device*: a virtual device whose
commands call the server. You assign its commands to buttons on the remote.
The server receives those calls and forwards them over WebSocket.

```text
Send:     your application --HTTP--> server --> hub --> equipment
Receive:  remote --> hub --HTTP callback--> server --WebSocket--> your application
```

The server runs the callback listener for you. Your application only needs
the server's API address; it does not need to expose an HTTP listener.

[Set up the server](#1-set-up-the-server) · [Send a command](#2-send-your-first-command) ·
[Receive a press](#3-receive-your-first-remote-press) · [Troubleshooting](#if-something-does-not-work)

## 1. Set up the server

The current server is **unreleased development software**. These instructions
use the checkout version of both packages, with Python 3.11 or later.
Run the server on a host on the hub's LAN. Use one server/proxy owner for a
hub; if Home Assistant or another proxy already manages it, disable that
hub there before starting this server.

From a checkout's repository root:

```sh
python -m pip install . ./sofabaton-x-server
sofabaton-x-server --hub 192.168.1.50
```

Replace `192.168.1.50` with your **physical hub's IP address**. Keep this
terminal running. Close the official Sofabaton app while connecting,
reading catalogs, sending commands or setting up button assignments: an
attached app owns the hub and blocks server control.

The `--hub` flag seeds registration only when `hubs.json` does not exist.
If you already have server data, use `POST /api/v1/hubs` with
`{"host":"192.168.1.50"}` to register another hub. You can submit this in
the interactive API docs at `http://localhost:8480/api/v1/docs`.

For Docker, use the [Linux host-network deployment instructions](../README.md#docker).
Keep this LAN service private; it has no built-in authentication.

### Find the registered hub ID

Open a second terminal at the repository root, using the same Python
environment, and run the [starter example](../examples/starter.py):

```sh
python sofabaton-x-server/examples/starter.py hubs
```

It calls `GET /api/v1/hubs` and prints the records. Look for your hub and
copy its `hub_id`. Relevant fields look like this (other fields omitted):

```json
[
  {
    "hub_id": "e26a44861b45",
    "enabled": true,
    "status": {"controllable": true, "catalog_ready": true}
  }
]
```

Wait for `controllable` and `catalog_ready` to become true. An ID initially
based on the IP address can change to the hub's MAC when the connection
banner arrives; run `hubs` again and use the MAC ID once available.

All commands below use `e26a44861b45` as an **example**. Replace it with your
registered hub ID. The scripts wait briefly for readiness before reads and
writes. A timeout reports the last status instead of repeatedly sending a
write.

If your client runs on another machine, add `--server http://192.168.1.10:8480`
**before the action**, replacing that IP with the **server host's** address:

```sh
python sofabaton-x-server/examples/starter.py --server http://192.168.1.10:8480 hubs
```

`--server` takes a server base URL without `/api/v1`. A reverse-proxy path
prefix is allowed. The examples default to `http://localhost:8480`.

## 2. Send your first command

### Find the device

```sh
python sofabaton-x-server/examples/starter.py --hub-id e26a44861b45 devices
```

Find the intended device by its `name`, and copy its `device_id`. Suppose
your television is device `7`. These IDs come from your hub; they are not
universal model numbers.

### Find a command on that device

```sh
python sofabaton-x-server/examples/starter.py --hub-id e26a44861b45 commands --device 7
```

The response lists that device's commands, for example:

```json
[
  {"command_id": 3, "label": "Volume Up"},
  {"command_id": 4, "label": "Volume Down"}
]
```

Choose a command whose physical effect you want to test. Always keep its
**device ID and command ID together**: command `3` on another device can
mean something entirely different.

### Send the selected command

If your listing really identified device `7`, command `3` as the command
you want, send it:

```sh
python sofabaton-x-server/examples/starter.py --hub-id e26a44861b45 send --device 7 --command 3
```

The control response is:

```json
{"accepted": true, "mode": "control"}
```

This confirms acceptance for sending, not proof that the equipment acted.
Check the physical result. Sending is an immediate request; there is no
background job to poll and no whole-hub snapshot or backup to prepare.

These are the same calls to implement in your own client:

| Step | HTTP request |
| --- | --- |
| Read readiness | `GET /api/v1/hubs/{hub_id}/status` |
| List devices | `GET /api/v1/hubs/{hub_id}/devices` |
| List commands | `GET /api/v1/hubs/{hub_id}/devices/{device_id}/commands` |
| Send | `POST /api/v1/hubs/{hub_id}/send` with `{"entity_id":7,"command_id":3}` |

In this device-command workflow, `entity_id` is the selected `device_id`.
The more general API can also address activity buttons/macros; see
[read and control](platform-integration.md#4-read-and-control) after this first test.

## 3. Receive your first remote press

This setup uses one existing activity and one button you choose on it.
It creates the callback device if the server has none, then assigns its
first slot's short and long commands to that button. The server persists
the callback record, so setup is not required on each application launch.

### Choose an activity and a button

```sh
python sofabaton-x-server/examples/starter.py --hub-id e26a44861b45 activities
```

Copy an `activity_id` from the returned list. Suppose you choose `101` and
the `PLAY` button. **The next command replaces that activity button's
existing short and long assignments.** Choose a button you are comfortable
reassigning. It does not change that button in other activities.

```sh
python sofabaton-x-server/examples/starter.py --hub-id e26a44861b45 setup-presses --activity 101 --button PLAY
```

The example waits for each setup job to finish. `202 Accepted` during setup
means a job has started; it is only successful when its status is `done`.
When finished, the example prints the callback device ID, labels and
destination. A new device uses `Demo` and `Demo Long`. If a callback device
already exists, the example reuses its first slot and its current labels;
it does not replace the existing specification.

The server's callback port is **8060** by default; the API/WebSocket port
is **8480**. The hub must reach port 8060 on the server's LAN address.
These are different connections: your application connects to 8480.

### Listen, then press the button

The listener uses `websockets`, included when you install the server above.
On a separate client machine, install that dependency with
`python -m pip install "websockets>=12"`; the other example actions use
only Python's standard library.

```sh
python sofabaton-x-server/examples/starter.py --hub-id e26a44861b45 listen
```

Wait for `Connected to server instance …`. Select activity `101` on the
physical remote, allow any remote configuration sync to finish, and press
`PLAY`. You should see a message of this shape, followed by `PRESS: Demo
(short)` (IDs, sequence, address and timestamp are illustrative):

```json
{
  "type": "press",
  "seq": 1,
  "hub_id": "e26a44861b45",
  "device_id": 12,
  "command_id": 1,
  "slot": 1,
  "label": "Demo",
  "press_type": "short",
  "resolution": "deployed",
  "transport": "http",
  "source": "192.168.1.50",
  "received_at": "2026-09-13T12:00:00+00:00"
}
```

Holding the same button uses command `11` and `press_type: "long"` for
the first slot. In `listen()`, replace the marked dispatch comment with
your application action. Match `hub_id`, `device_id`, `command_id` and
`press_type`; labels are display text and can change. The example dispatches
only `resolution: "deployed"`, while printing other press records for diagnosis.

The listener subscribes to `ws://localhost:8480/api/v1/events?hub_id=<hub_id>`
(`wss://` with HTTPS). A `hello` message confirms the subscription.
General `hub_event` messages describe hub state; **remote callbacks arrive
as `type: "press"`**. The server already provides the hub-facing HTTP listener.

For a client in another language, the setup and receive sequence is:

| Step | Call |
| --- | --- |
| Reuse an existing callback device | `GET /api/v1/hubs/{hub_id}/callback-device` |
| If specifically `404 callback_device_not_found`, deploy | `POST /api/v1/hubs/{hub_id}/callback-device` with `{"name":"Starter callbacks","slots":[{"label":"Demo","long_label":"Demo Long"}]}`; follow its job to `done`, then GET the record |
| Check callback listener | `GET /api/v1/server/callback-listener`; check `bound` |
| Read activity detail for binding | `POST /api/v1/hubs/{hub_id}/snapshot/refresh` with `{"activity_id":101}`; follow its job to `done` |
| Get the edit revision | `GET /api/v1/hubs/{hub_id}/snapshot`; keep `snapshot_id` |
| Bind the selected button | `PUT /api/v1/hubs/{hub_id}/activities/101/buttons/PLAY` with `{"device_id":12,"command_id":1,"long_press":{"device_id":12,"command_id":11}}` and quoted `snapshot_id` as `If-Match`; follow its job to `done` |
| Receive | Open `/api/v1/events?hub_id=<hub_id>` as a WebSocket; handle `press` messages |

Replace device `12` in the binding body with the returned callback device
ID. Poll setup jobs at `GET /api/v1/hubs/{hub_id}/jobs/{job_id}` and stop on
`failed` or `cancelled`. A request timeout does not mean nothing happened;
inspect the job and callback record before repeating setup.

## If something does not work

| Symptom | First check |
| --- | --- |
| `hubs` returns an empty list | `--hub` only seeds a new data directory. Register the hub through `POST /api/v1/hubs`. |
| `hub_not_found` | Run `hubs` again; the ID may have changed from an IP address to the MAC. |
| Not ready, `hub_busy` or `send_refused` | Close the official app; check that no other integration owns the hub and that it can connect back to the server. |
| Send accepted, no equipment response | Verify the selected device/command pair and check the equipment's usual IR/network reachability. |
| Callback device exists but listener is not bound | Inspect `GET /api/v1/server/callback-listener`; another service may own port 8060. Resolve the conflict, then use `POST /api/v1/server/callback-listener/retry`. |
| Listening, but no press arrives | Confirm the selected activity/button, the deployed record's `target`, the hub's access to TCP 8060, and completion of the remote's configuration sync. |
| Ordinary IR button produces no event | Expected: only commands assigned to call the callback device produce these press messages. |
| Binding job fails | Read its `error` and partial `result`; keep the deployed callback device and inspect the activity before trying another edit. |

For callbacks, the X1 always uses port 8060. Network/firewall details are
in the [networking guide](../../docs/networking.md); deployment options are
in the [server README](../README.md#docker).

The starter listener ends when its connection closes and does not reconnect
or replay missed presses automatically. For a production integration, add
reconnect/backoff and press-history reconciliation, using `(instance_id, seq)`
to avoid duplicate delivery. Continue with the
[platform integration guide](platform-integration.md#10-button-events) for
that lifecycle and callback management. Configuration editors, full-document
writes and restore are separate, advanced workflows.

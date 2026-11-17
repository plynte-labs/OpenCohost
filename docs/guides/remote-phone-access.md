# Talk to Kira from your phone

This guide sets up the remote voice gateway (see `docs/adr/ADR-067-remote-voice-gateway.md`). It is off by default. You do the tunnel setup yourself; nothing in the repository starts a tunnel.

How it works: your phone turns speech into text in the browser, sends the text to your PC through your own domain, gets Kira's final reply and reads it aloud with the phone's voices. Kira also speaks on the PC.

Note: on Chrome for Android, speech recognition sends your audio to Google. Typing avoids that.

## 1. Enable the gateway

Set these in the environment of the process that starts the backend, then restart the app:

- `OPENCOHOST_REMOTE_GATEWAY=1`
- optional: `OPENCOHOST_REMOTE_GATEWAY_PORT` (default `8780`)

With `pnpm tauri:debug`, the Tauri app spawns `python -m uvicorn` itself (`OpenCohost_UI/src-tauri/src/backend/supervisor.rs:123-136`). It does not clear the environment, so the backend inherits the variables of the terminal (or the Windows user environment) that launched the app. Set them there before launching. With `run-api.bat`, set them in that shell.

Check: `http://127.0.0.1:8780/status` in a browser on the PC should answer.

## 2. Install cloudflared and create a tunnel

Install `cloudflared`, log in, and create a named tunnel. Add a public hostname on your domain that points to `http://127.0.0.1:8780`.

**Never** point a tunnel at 8765 or 8770. Those are the main API and are not safe to expose.

Sample `config.yml`:

```yaml
tunnel: <tunnel-id>
credentials-file: <path-to-credentials>.json

ingress:
  - hostname: kira.<your-domain>
    service: http://127.0.0.1:8780
  - service: http_status:404
```

The final `http_status:404` rule makes every other hostname return 404.

## 3. Put Cloudflare Access in front

In Cloudflare Zero Trust, create a self-hosted Access application for `kira.<your-domain>`. Add a policy that allows only your own email, using the email one-time PIN login. This blocks scanners and anyone trying to guess pairing codes before they reach the gateway.

## 4. Pair the phone

1. In the desktop app, open Controles and find the "Link phone" panel in the Voice pane, right after the PTT card.
2. Set the public URL (`https://kira.<your-domain>`).
3. Generate a code. It lasts 120 seconds, works once, and is burned after 5 wrong attempts.
4. Open the link on the phone, pass the Access login, and name the device.

## 5. Use it

Hold the button to talk, or type in the text box. The phone shows the reply and reads it with its own voices. Kira also speaks it on the PC.

## 6. Revoke a lost phone

In the "Link phone" panel, revoke the device. Its token stops working at once.

## 7. Troubleshooting

- **No microphone on the phone:** browsers need HTTPS for the mic. A LAN address over plain `http` will not get the mic on Chrome. Use the tunnel hostname.
- **iOS Safari does not speak:** iOS needs a first tap on the page to unlock speech.
- **Error 409:** the gateway is off or the engine is not ready yet. Check the flag and wait for the app to finish loading.
- **"No reply":** a guardrail dropped the reply, or it timed out (about 90 seconds).
- **Port in use:** the gateway logs a warning and the main app keeps running. Change `OPENCOHOST_REMOTE_GATEWAY_PORT` and the tunnel target together.
- **Known v0 limit:** if you type a direct message on the PC at the very same moment, the phone may receive that reply.

## 8. Manual phone test checklist

1. Pair from the link; the code disappears from the address bar.
2. Reload; the phone stays linked.
3. Hold to talk: listening, sending, waiting, speaking; the reply is shown and read aloud.
4. The text box works the same way.
5. Stop voice works.
6. Revoke on the PC returns the phone to pairing.
7. The not-ready and rate-limit messages appear.
8. Unlink, then a wrong code shows the "generate a new one" message.
9. iOS Safari: the first-tap unlock works.
10. 360 px light and dark layout.
11. Kira (the v3 model, always, about 20.7 MB) appears after pairing, with a percentage while the model loads. To use another file, set `OPENCOHOST_PHONE_VRM` to its path.
12. She idles and blinks.
13. Her mouth moves while the phone voice speaks.
14. The show/hide avatar button keeps its choice after a reload.
15. If the avatar does not appear, the page still works by voice.
16. If the avatar is slow: the first load is about 20 MB, so use Wi-Fi.

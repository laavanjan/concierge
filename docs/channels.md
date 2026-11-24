# Channel setup

All channels call the same `ConciergeRuntime.handle()`; only the adapters differ. Expose the gateway publicly for
webhooks (for example `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`) and set
`PUBLIC_BASE_URL` to that URL.

## Web chat

Built in: `http://localhost:8000/`. API: `POST /v1/chat`, `POST /v1/chat/stream` (SSE), `GET /v1/sessions/{id}/messages`.

## WhatsApp: Twilio sandbox

1. Twilio console → Messaging → Try WhatsApp; join the sandbox from your phone.
2. Sandbox settings → "When a message comes in": `POST {PUBLIC_BASE_URL}/channels/whatsapp/twilio`.
3. `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`.

Webhooks are rejected unless `X-Twilio-Signature` matches. Replies go back as TwiML; asynchronous updates (approvals,
operator replies) use the Messages API.

## WhatsApp: Meta Cloud API test number

1. Meta developer app → WhatsApp → API setup (test number). Webhook URL `{PUBLIC_BASE_URL}/channels/whatsapp/meta`,
   verify token = `META_VERIFY_TOKEN`; subscribe to `messages`.
2. `.env`: `META_VERIFY_TOKEN`, `META_APP_SECRET` (signature check), `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`.

## Email

- Polling: set `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` (an app password) and `SMTP_*`; the gateway (or
  `python -m channels.worker`) polls every `IMAP_POLL_S` seconds, strips quoted history and signatures, and replies in
  the same thread.
- Webhook: point an inbound-parse service at `POST /channels/email/inbound` (JSON `from_address`, `subject`, `text`).
- Without SMTP settings, replies are logged (dry run), which is handy for demos.

```bash
curl -s localhost:8000/channels/email/inbound -H "content-type: application/json" \
  -d '{"from_address":"jo@example.com","name":"Jo","subject":"Parking","text":"Do you have parking?"}'
```

## Voice

- Browser: click the mic in the chat widget (Web Speech API for speech-to-text, `speechSynthesis` for speech out).
- Server speech-to-text: `pip install .[voice]`, `STT_PROVIDER=faster-whisper`, then `POST /v1/voice/audio` with an audio file.
- Phone: Twilio number → Voice webhook `POST {PUBLIC_BASE_URL}/channels/voice/twilio`; the call loops `<Gather
  input="speech">` → agent → `<Say>`.

Voice replies are shortened by the agents (channel note) and cleaned of citation markers and lists by `for_voice()`.

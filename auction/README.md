# auction

[![Deploy on Enclavia](https://beta.enclavia.io/deploy-button.svg)](https://beta.enclavia.io/deploy?manifest=https://raw.githubusercontent.com/EnclaviaIO/enclavia-samples/main/auction/enclavia.json)

A sealed-bid auction that runs inside an enclave. Bids go end to end into
the enclave through the client SDK, and nobody can read them while the
round is open: not the other bidders, not the operator, not the admin.
Closing the round publishes the full list.

The rules are [`app.py`](app.py). The enclave's PCRs pin that file, so a
client that verified the attestation knows the rules it is bidding under.
No endpoint returns a bid before the close, and the workload logs that a
bid arrived, never what it was.

The sample ships a web app ([`web/index.html`](web/index.html)) that
talks to the enclave through
[`@enclavia/client-wasm`](https://www.npmjs.com/package/@enclavia/client-wasm),
so the encrypted channel ends in your browser. It has editable PCR fields
(with a "flip a byte" button to watch the attestation fail) and a debug
console that shows every request and response.

## Deploy

One click: the button above opens the create form prefilled from
[`enclavia.json`](enclavia.json). It asks for one secret, `ADMIN_TOKEN`:
any password you like. You type it into the web app to close a round.

With the CLI:

```sh
enclavia enclave create \
    --container-port 8080 \
    --name auction \
    --storage-size-bytes 268435456 \
    --upgradable
# Enclave created:
#   ID:     1d2c3b4a-...

enclavia secret set 1d2c3b4a ADMIN_TOKEN=pick-something

docker build -t auction:v1 .
enclavia push auction:v1 1d2c3b4a
enclavia enclave status 1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d   # wait for running, copy the PCRs
```

The storage volume keeps bids across restarts. Without it they live in
tmpfs and are lost when the enclave stops. `--upgradable` is optional; it
lets you demo a staged upgrade later (see [Workshop](#workshop)).

Enclaves are debug (QEMU) by default. Add `--production` for real Nitro
hardware. Production needs a paid plan.

## Open the web app

Two ways, depending on who you want to trust for the page itself:

- **Served by the enclave**, through the hosted proxy:
  `https://<enclave-id>.enclaves.beta.enclavia.io/proxy/`. Nothing to
  install. The page arrives through the proxy, so you trust the proxy
  for the JavaScript. The bids then travel over the SDK's own attested
  channel.
- **From your own machine**: `python3 -m http.server -d web 8000`, then
  open `http://localhost:8000`. Now the page is yours. It loads the SDK
  from jsDelivr (pinned to a version), falling back to unpkg.

Then:

1. Paste the enclave ID and the three PCRs from `enclavia enclave status`.
   "Get PCRs from proxy" fills them from the proxy's `X-Enclavia-PCR*`
   headers. That is handy, but you are trusting the proxy for those values.
2. Tick **Debug mode** for a debug (QEMU) enclave. Leave it off for
   production.
3. **Connect and verify.** The SDK does the Noise handshake, checks the
   attestation against your pins, and only then sends anything.

**Copy share link** puts the enclave ID and PCRs in the URL
(`?enclave=…&pcr0=…&pcr1=…&pcr2=…&debug=1`), so you can hand the room
one link or a QR code. Add `&connect=1` to connect on load.

## What you can verify

Every accepted bid gets a receipt and extends a hash chain:

```
head_0 = sha256(round header)
head_n = sha256(head_{n-1} || sha256(receipt_n))
```

The current head is public while the round is open (`GET /status`).
Receipts carry a random salt, so the head leaks nothing about the bids.
After the reveal, **Verify the list** in the web app recomputes the chain
from the published bids and checks that:

- every receipt hashes to what was published;
- the chain ends at the final head;
- your own bids are in the list, unchanged, at the position you got;
- every head your browser saw while the round was open is on the chain.

The attestation already tells you the code. The chain is an extra check
that does not depend on it.

## API

| Method | Path | What it does |
|--------|------|--------------|
| `GET` | `/status` | Round number, title, phase, bid count, commitment head. Never bids. |
| `POST` | `/bid` | `{"bidder": "alice", "amount_sats": 21000, "message": "gm"}`. Returns the receipt and the head after it. Every bid counts; bids can't be withdrawn. |
| `GET` | `/bids?offset=0` | `403` while the round is open. After the close: one page of bids, the winner (highest bid, earliest on a tie) and the best bid from anyone else (the second-price amount). Follow `next_offset` until it is `null`. Each bid is `receipt_canonical` (the exact JSON that was hashed: round, seq, bidder, amount, message, time, salt), `receipt_hash` and `head_after`. |
| `GET` | `/rounds/<n>?offset=0` | The same for an earlier round. |
| `POST` | `/admin/close` | Close the round and publish. Needs `Authorization: Bearer <ADMIN_TOKEN>`. Returns the round status. |
| `POST` | `/admin/open` | `{"title": "...", "closes_in_minutes": 10}`. Start the next round (the current one must be closed). The deadline is optional; the enclave closes the round itself when it passes. |
| `GET` | `/health` | Liveness. |
| `GET` | `/` | The web app. |

Limits: names up to 32 characters, messages up to 140, 5000 bids per
round.

Why pages: an SDK `fetch` response travels as a single Noise message,
which caps it at about 32 KB of HTTP response. A bigger one drops the
client's channel, so every reply here stays under 16 KB. The web page
itself (about 36 KB) is fine through the hosted proxy, which streams it
in chunks. Don't fetch `/` through the SDK's raw request box.

## Known platform issues (SDK 0.2.0)

- **Large responses drop the channel.** See above. The server sends a
  `fetch` reply as one Noise message, and the payload's CBOR encoding
  roughly doubles it.
- **"Follow the signed upgrade chain" fails from a browser.** The SDK's
  `trustUpgrades` reads `GET /enclaves/{id}`, which needs a login, so it
  gets a 404. The chain itself (`/enclaves/{id}/upgrade-chain`) is
  public. Pin the current PCRs instead.
- **Two running enclaves per account.** Stop one before deploying a
  third.

## Workshop

A 70-minute hands-on outline. The presenter runs one **production**
enclave (`--production --upgradable --min-upgrade-delay 24h`); attendees
need only a browser for the first part. The delay is baked into the
image and also holds back your own fixes, so pick it deliberately: a
longer one makes the rug-pull demo stronger and a mistake costlier.

1. **Bid (15 min).** Share the link with the presenter's enclave ID and
   PCRs. Everyone connects and bids. Watch the bid count and the head
   move on the projector. Nobody, including the presenter, can see the
   amounts.
2. **Break the pin (5 min).** Flip a byte of any PCR and reconnect. The
   SDK refuses before a single byte of the bid leaves the laptop. Undo
   the flip and reconnect.
3. **Reveal (5 min).** The presenter closes the round. Everyone clicks
   **Verify the list**.
4. **Fork the rules (25 min).** Deploy your own copy with the button
   (debug mode, free). Change the rules in `app.py`: first price instead
   of second, a reserve price, a cap on bids per person, a deadline.
   Push it and look at the new PCRs. A neighbour who pinned your old
   PCRs is refused, so changing the rules changes the identity.
5. **The rug-pull demo (presenter, 10 min).** Push a v2 that adds
   `GET /peek`, which returns the sealed bids. Show that it only stages:
   the upgrade can't activate before the minimum delay, the upgrade chain
   records it, and a client pinned to v1 refuses v2. Run
   `enclavia reproduce` on v1 to show the code people bid against.

Points worth raising in the discussion:

- **Debug vs production.** A debug enclave's attestation is a stub (nonce
  binding and PCR equality, no hardware signature). The "nobody can read
  the bids" claim holds only on production Nitro.
- **The proxy.** The page served through `/proxy/` is only as trustworthy
  as the proxy. A page from your own disk isn't.
- **Rolling back the disk.** The operator could restore an old copy of the
  encrypted volume (for example to drop late bids). Anti-rollback storage
  (`--anti-rollback`) is the defence.
- **The admin can close early.** That is in the rules too, and anyone can
  read it.

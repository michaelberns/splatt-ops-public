---
name: splatt-email
description: Write emails for the operator of Splatt Engineering, in the operator's own style. Use this skill whenever the operator asks to draft, write or compose an email to a client, supplier or colleague: quotes, follow-ups, requests for information, delivery updates, pricing questions to suppliers and internal messages. Trigger on requests such as "draft an email", "write to [contact]", "reply to [person]" or "send this to [name]". The email is written into the chat for the operator to send; the agent never sends it and never leaves a draft in Gmail.
---

# Splatt email

This skill sets how emails are written for the operator: the structure, the tone, the signature, and where the email goes.

## Where the email goes

**Rule: the email goes in the chat, never into Gmail.** Do not call a Gmail create-draft tool and never send anything. The operator copies the email from the chat and sends it.

Give the whole email in one block that can be copied in one go:

1. **To** and **Cc**.
2. **Subject.**
3. The full body.
4. The signature.

Say which Gmail thread it replies to, so the operator pastes it into that thread rather than starting a new one. If you rewrite an email, output the full new version, not a list of changes.

Before an email to a client that mentions any price or figure, run the `price-leak-guard` playbook (see `splatt-supplier-filing`). Do not hand the email over until the guard says `send_authorized: true`.

For a client flagged sensitive in PocketBase (`critical` is true), do not write an email at all. Report the thread and let the operator write it.

## Before writing to a supplier: check for earlier contact

For a supplier that is new, or has not been contacted for a while:

1. Search every mailbox connected for the operator, including shared colleague archives, for earlier threads with that supplier.
2. If there is an existing relationship, open with the contact's name and carry on from it. Treating an established supplier as a cold contact reads badly.
3. Never name a colleague or a referrer ("X pointed us your way") unless the operator includes it.
4. First contact goes through the `engage-new-supplier` playbook.

## Style

### Greeting

- Always `Hi [First name],`.
- Never "Dear", "Hello", full names, or "To whom it may concern".
- Use the name the contact signs with (for example "Nigel", not "Nigel Grant").

### The first sentence starts in lower case

The first sentence of the body, straight after `Hi [Name],`, starts with a lower-case letter: `please see the quote attached.` or `could you please...` or `thanks for that.` It follows on from the comma after the greeting.

Every sentence after the first uses normal sentence case:

```
please see the quote attached. The freight has been split with another shipment, so that line is half the usual cost.
```

Do not "correct" the lower-case opener, and do not lower-case any later sentence.

### Tone and length

- Direct and warm. Get to the point in the first sentence.
- Acknowledge the other person's point quickly: `you are right, that does look odd.`
- Prefer `could you please` to a bare `please`.
- Two to five sentences is normal.
- No bullet points, headings or bold text in the body.
- A short social opener (`I hope all is well.`) is fine now and then, when the relationship suits it. One line at most.
- Attachments: `please see the quote attached` or `please find the file attached`.

### Words and habits to avoid

These make an email read as machine written. Never use them:

- Em dashes. Use a comma or a full stop, or rewrite the sentence.
- Ellipses for effect.
- Smooth transitions: "That said,", "With that in mind,", "To that end,".
- Filler: "Absolutely!", "Great question!", "Certainly!".
- Hedging: "It's worth noting that", "It's important to mention".
- "Please don't hesitate to reach out if you have any questions."
- "I wanted to follow up on", "I am reaching out regarding", "I am writing to", "just circling back", "as per my previous email".
- "I hope this email finds you well."
- Summarising the email at the end of a short email.

### Subject lines

- Specific: client or company name plus the topic, for example `Clearwater Bottling - filler spare parts` or `Orchard Lane - capper change parts delivery`.
- In a reply, keep the existing `Re:` subject.

### Signature

Every email ends with this block. Sign-off is always `Kind regards,` with a comma.

```
Kind regards,
[Operator Name]
[Role]
Splatt Engineering
m: +64 21 000 0000
w: splatt.example.co.nz   e: operator@splatt.example.co.nz
```

Never "Best", "Cheers", "Thanks", "Warm regards" or "Regards" on its own.

## Templates

Replace `[signature]` with the signature block above.

**Sending a quote to a client**

```
Hi [Name],

please see the quote attached.

[signature]
```

**Asking a client for information**

```
Hi [Name],

could you please [the specific information needed]?

[signature]
```

**Acknowledging and promising a follow-up**

```
Hi [Name],

thanks for that. I will get back to you as soon as I have more information.

[signature]
```

If the email promises a date, the promise needs a Todoist task that owns the chase (see `splatt-project-files`, the rule on client promises).

**Raising a pricing question with a supplier**

```
Hi [Name],

could you please check the [quote / pricing]? It looks like [the specific observation]. Could you please confirm [the specific question]?

[signature]
```

**Forwarding or escalating internally**

```
Hi [Name],

[one sentence of context].

[signature]
```

**Following up an order or a delivery**

```
Hi [Name],

I hope all is well. Could you please give me an update on the expected delivery date for [the order]?

[signature]
```

## Finding the address

Do not keep addresses in this skill. Xero holds the current contact details and PocketBase holds who handles what (see `splatt-ss-agent`, `references/contacts.md`). Check special handling rules for the client or supplier in `splatt-clients` and `splatt-suppliers` before writing (for example a supplier that wants two people copied).

## Checklist before handing the email over

- `Hi [First name],` and a lower-case first sentence, normal case after it.
- Two to five sentences, no bullets, no bold, no em dashes.
- The full signature with `Kind regards,`.
- To, Cc, Subject and the thread it belongs to are stated.
- The price leak guard has passed if a client will see any figure.

# Registration and invites

How new users get into the panel is set in **Settings → Registration**.

| Mode | What happens |
|---|---|
| By invitation only (default) | The page **Create an account** opens only from an invite link. |
| Open, approved automatically | Anyone can register and gets the **default plan** from the settings right away. Without a default plan the user has no plan until an administrator assigns one. |
| Open, approved by an administrator | A new user signs in but waits: there are no keys and the portal says the account waits for approval. Administrators with an email get a letter. |

The queue is in **Admin → Registration**: **Approve** gives the chosen plan and opens access,
**Reject** blocks the account.

## Invites

An invite is a link `https://<panel>/accounts/invite/<code>/` created in **Admin → Registration**.
It has a plan, the number of registrations and, optionally, an end date. An invite works in every
mode: the user gets the invite plan at once (it wins over the default plan) and skips the
administrator queue; the email is confirmed as in open registration. An expired or used-up invite does not open
registration.

A moderator creates invites in **Registration** too and sees only their own. A user who comes by a
moderator's invite joins the moderator's [zone](Security.md#moderators-zone) at once; with the
moderator's user quota used up the invite says "This invite cannot take new users now."

## Email confirmation

With **Email confirmation** on and mail set up (an SMTP server in the settings or
`LAMNEZIA_EMAIL_URL` other than the console), every registration, by invite too, sends a
confirmation link: the account stays inactive until the link is opened. In the approval mode the administrators are told
only after the email is confirmed. Without mail no confirmation is asked.

## Limit per address

Open registration accepts at most `LAMNEZIA_REGISTRATION_RATE_LIMIT` accounts an hour from one IP
address (`5` by default, `0` turns the limit off). Registrations by invite are not counted.

## Sign-in by email link

When mail is set up, the login page has **Sign in by email link**: the panel sends a link that
signs in without a password. The link lives `LAMNEZIA_MAGIC_LINK_MINUTES` minutes (`15` by default)
and works once. The answer is the same whether or not an account with that email exists.

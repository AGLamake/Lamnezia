# Guides and releases

What users see before they have a key: install guides for every platform, Amnezia app downloads
from the latest release, the FAQ and a list of useful materials.

## Install guides

`/guides/` lists the platforms: Windows, macOS, Linux, Android and iOS. The visitor's device is
detected from the browser and marked as "your device".

A platform page (`/guides/windows/`, `/guides/android/` and so on) has download buttons from the
latest Amnezia release on GitHub, a link to all downloads and three steps: install the app, get a
key, add it to Amnezia. Release caching and the GitHub token are described in
[Amnezia app downloads](#amnezia-app-downloads).

- **Android.** The visitor can choose the Android version (9–10 or 11+) and the processor. The
  default is Android 11+ and `arm64-v8a`, which fits most phones. The version is taken from the
  browser when it reports one (Chrome always says "Android 10", so 11+ stays for it). A Google Play
  link is next to it.
- **iOS.** The app is only in the App Store; the page links there.
- While the panel has never received a release (right after installation or without access to
  GitHub), the page offers the GitHub releases page instead.

A link to a chosen APK can be sent to someone: `/guides/android/?android=9-10&abi=armeabi-v7a`.

## FAQ

The question list is at `/guides/faq/`, an article at `/guides/faq/<address>/`.

Articles are edited in the Django admin: **Guides → FAQ articles**.

A starter set (installing on each platform, adding a key, "cannot connect", "the protocol is
blocked", "a port below 10000", "no internet", "slow", "slow upload through the VPN", "key not
accepted", app updates) is built in: it appears on the first start, see
[Built-in articles](#built-in-articles).

- Text is Markdown: `##` headings, lists, links, bold, code. HTML inside the text is removed.
  **Preview** under each text shows it as the public page will, without saving.
- An article has two languages. If the title or text is empty in the visitor's language, the other
  language is shown. Filling in one is enough.
- "position" sets the order (lower comes first); unticking "published" hides the article.
- "address" (slug) is part of the article link; it is filled in from the English title and can be
  edited.

## Useful materials

The `/guides/materials/` page lists articles on other sites by topic: installation, speed,
blocking, security, devices. The text of those articles is not copied: each material has only a
title, link, language, source and our own short description in Russian and English.

Materials are edited in the Django admin: **Guides → Useful materials**. The starter set (Amnezia
documentation, articles on AmneziaWG and getting around blocking, device setup, speed
troubleshooting) is built in, like the FAQ.

### Built-in articles

On every start, right after the migrations, the `web` container runs `manage.py load_guides`. It
adds the built-in FAQ articles and useful materials this panel has never had, matched by address
(slug):

- a fresh install gets the whole set on the first start;
- after an upgrade, articles new in that version are added;
- articles you edited stay as you left them, and articles you deleted do not come back: the panel
  remembers every built-in address it has offered once;
- restarts add nothing twice.

Deleted built-in entries are listed in the admin under **Guides → Built-in entries**. Select them
and run **Restore the selected entries**: they come back with the text of the current version. An
entry whose address is taken again, or one this version no longer ships, is skipped.

To bring back the original text of a built-in article you edited, load the full set by hand; it overwrites
the built-in articles by address and leaves the ones you added alone:

```bash
docker compose exec web python manage.py loaddata faq materials
```

Once a week a background job (`beat`, `guides.check_materials`) opens every published link. A
link that does not open (an HTTP error, the site cannot be reached) is marked **broken link** with
the error text and disappears from the public page. In the admin such links are found with the
"broken link" filter, next to the date of the last check. Fix or replace the link and the mark is
cleared at the next check. While such links exist, the administrator's dashboard says how many
published materials are hidden and links to the filtered list. The check only visits links from the list and records nothing about
visitors.

## Suggestions from users

A signed-in user opens **Guides → Suggest an article** (`/guides/suggest/`) and sends either a
link to an article or their own text in Markdown, with its language, topic and a comment for the
staff. A box sets whether their name is shown when the text is published. Below the form the user
sees their earlier suggestions, what became of them and the reason of a refusal.

- Guests cannot suggest: the page sends them to sign in.
- A user sends at most `LAMNEZIA_SUGGESTION_RATE_LIMIT` suggestions a day (3 by default, `0`
  removes the limit).
- Only `http://` and `https://` links are taken. Markdown goes through the same cleaning as FAQ
  articles: HTML, scripts and `javascript:` links are removed.
- **Settings → Users and keys → Users suggest articles** turns the form off; the page then says
  that suggestions are not accepted.

Administrators and moderators review suggestions under **Article suggestions** in the admin menu
(`/guides/moderation/`). The suggestion page shows it as the author sent it (the text rendered as
it will look) and a form to edit it before publishing:

- a link becomes a **useful material** (fill in our own description and the source);
- a text becomes an **FAQ article** in its language.

**Publish as** switches the form to the other kind: a link can become an FAQ article (it starts
with the link in its text), a text a useful material (the text becomes its description; give the
link). Switch before editing: the form starts again from the suggestion.

The **Suggested by** field holds the author's name if they asked to be named and is empty
otherwise; it is shown under the material or article and can be edited. **Decline** needs a
reason. The author is notified of either decision where they get notifications
(`suggestion_accepted` with a link, `suggestion_rejected` with the reason; see
[Notifications](Notifications.md)). A new suggestion is the `suggestion_created` event for the
staff channels. Both decisions go to the audit log.

## Amnezia app downloads

Download links come from the latest release of `amnezia-vpn/amnezia-client` on GitHub. A
background job (`beat`) asks for it every 6 hours and saves it to the database; guide pages never
call GitHub themselves and read the saved release. While none is saved, the first page opened
queues a refresh. If GitHub cannot be reached, the last release received stays.
Without a token GitHub allows 60 requests an hour per IP address, which is enough for one panel.

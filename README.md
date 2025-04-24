matrix-archiver
===============

If you want to add a room to be archived, please ensure it is public and the history is available to "Anyone", then open an issue with the internal ID (starts with !).

What it does
------------

*   Scoops the **entire** history of any *public, un-encrypted* Matrix room.
*   De-duplicates edits – only the **latest** version of a message is kept.
*   Renders:

    *   links (`http…` or `[label](http…)`)
    *   inline code `` `like this` ``
    *   fenced blocks

*   Colour-codes each user, shows basic threading, tags `[edited]`.
*   Emits

    ```
    archive/<slug>/index.html    ← pretty view
    archive/<slug>/room_log.txt  ← plain text
    index.html                   ← directory of rooms
    ```

Made for GitHub Actions + GitHub Pages but works anywhere there's python3.

---

Set-up (GitHub Pages)
---------------------

| repo secret | what to put in it                                                |
|-------------|------------------------------------------------------------------|
| `MATRIX_HS` | homeserver URL, e.g. `https://matrix.example.org`                |
| `MATRIX_USER` | full bot ID, e.g. `@archiver:example.org`                      |
| `MATRIX_TOKEN` | long-lived access-token for that user                         |
| `MATRIX_ROOMS` | **space-separated internal room-IDs**, e.g.<br>`!abc:example.org !def:example.org` |

Commit the supplied workflow from `.github/workflows/` and you’re done:

* nightly cron → pulls fresh history  
* commits the new artefacts  
* deploys to `gh-pages`

---

Local run (one-off)
-------------------

```bash
pip install matrix-commander==8.*
export MATRIX_HS="https://matrix.org"
export MATRIX_USER="@me:matrix.org"
export MATRIX_TOKEN="…"
export MATRIX_ROOMS="!roomId:matrix.org"
python scripts/update.py
open index.html

